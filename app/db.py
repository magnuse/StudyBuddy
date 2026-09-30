"""SQLite storage. Schema changes are appended to MIGRATIONS and applied at start."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE members (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('parent', 'student')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE invites (
        code TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        used_by INTEGER
    );
    CREATE TABLE subjects (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL COLLATE NOCASE,
        language TEXT,               -- e.g. 'es' for language subjects, NULL otherwise
        active INTEGER NOT NULL DEFAULT 1,
        weight REAL NOT NULL DEFAULT 1.0
    );
    CREATE TABLE topics (
        id INTEGER PRIMARY KEY,
        subject_id INTEGER NOT NULL REFERENCES subjects(id),
        title TEXT NOT NULL,
        test_date TEXT,
        summary TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'review')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE materials (
        id INTEGER PRIMARY KEY,
        topic_id INTEGER REFERENCES topics(id),
        filename TEXT,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY,
        topic_id INTEGER NOT NULL REFERENCES topics(id),
        kind TEXT NOT NULL CHECK (kind IN ('mc', 'short', 'explain')),
        prompt TEXT NOT NULL,
        answer TEXT NOT NULL,
        options TEXT,                -- JSON list for multiple choice
        rubric TEXT,
        source_ref TEXT,
        approved INTEGER NOT NULL DEFAULT 0,
        removed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE vocab_lists (
        id INTEGER PRIMARY KEY,
        subject_id INTEGER NOT NULL REFERENCES subjects(id),
        title TEXT NOT NULL,
        start_date TEXT NOT NULL,
        test_date TEXT,
        approved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE vocab_words (
        id INTEGER PRIMARY KEY,
        list_id INTEGER NOT NULL REFERENCES vocab_lists(id),
        term TEXT NOT NULL,          -- foreign language side, e.g. 'la casa'
        translation TEXT NOT NULL,   -- Swedish side, e.g. 'huset'
        term_alts TEXT NOT NULL DEFAULT '[]',
        translation_alts TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE cards (
        id INTEGER PRIMARY KEY,
        item_type TEXT NOT NULL CHECK (item_type IN ('q', 'w')),
        item_id INTEGER NOT NULL,
        direction TEXT NOT NULL DEFAULT '',   -- words: 'to_sv' or 'from_sv'
        box INTEGER NOT NULL DEFAULT 0,
        due_at TEXT NOT NULL,
        seen INTEGER NOT NULL DEFAULT 0,
        correct INTEGER NOT NULL DEFAULT 0,
        UNIQUE (item_type, item_id, direction)
    );
    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY,
        card_id INTEGER NOT NULL REFERENCES cards(id),
        answer TEXT NOT NULL,
        score INTEGER NOT NULL,      -- 0 wrong, 1 almost/partly, 2 right
        feedback TEXT,
        flagged INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]

# A new card is due right away.
NEW_CARD_DUE = "2000-01-01T00:00:00"
# Leitner boxes: days until a card is due again after a correct answer.
BOX_INTERVALS = [0, 1, 2, 4, 8, 16, 32]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Member:
    id: int
    telegram_id: int
    name: str
    role: str


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ---- schema -----------------------------------------------------------
    def migrate(self) -> int:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            with self.conn:
                self.conn.executescript(MIGRATIONS[index])
                self.conn.execute(f"PRAGMA user_version = {index + 1}")
        return len(MIGRATIONS)

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.conn:
            cur = self.conn.execute(sql, tuple(params))
        return cur.lastrowid

    # ---- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.run("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.run(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- members and invites ---------------------------------------------
    def member(self, telegram_id: int) -> Member | None:
        row = self.one("SELECT * FROM members WHERE telegram_id = ?", (telegram_id,))
        return Member(row["id"], row["telegram_id"], row["name"], row["role"]) if row else None

    def members(self, role: str | None = None) -> list[Member]:
        sql, params = "SELECT * FROM members", ()
        if role:
            sql, params = sql + " WHERE role = ?", (role,)
        return [Member(r["id"], r["telegram_id"], r["name"], r["role"]) for r in self.q(sql + " ORDER BY id", params)]

    def add_member(self, telegram_id: int, name: str, role: str) -> Member:
        self.run(
            "INSERT INTO members(telegram_id, name, role, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name, role = excluded.role",
            (telegram_id, name, role, now_iso()),
        )
        return self.member(telegram_id)

    def remove_member(self, member_id: int) -> None:
        self.run("DELETE FROM members WHERE id = ?", (member_id,))

    def create_invite(self, role: str, created_by: int, hours: int = 24) -> str:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
        expires = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.run("INSERT INTO invites(code, role, created_by, expires_at) VALUES(?, ?, ?, ?)", (code, role, created_by, expires))
        return code

    def use_invite(self, code: str, telegram_id: int) -> str | None:
        """Returns the invite's role if the code is valid, and marks it used."""
        row = self.one("SELECT * FROM invites WHERE code = ? AND used_by IS NULL", (code.strip().upper(),))
        if not row or datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return None
        self.run("UPDATE invites SET used_by = ? WHERE code = ?", (telegram_id, row["code"]))
        return row["role"]

    # ---- subjects and topics ---------------------------------------------
    def subject_by_name(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM subjects WHERE name = ? COLLATE NOCASE", (name.strip(),))

    def ensure_subject(self, name: str, language: str | None = None) -> sqlite3.Row:
        row = self.subject_by_name(name)
        if row:
            return row
        self.run("INSERT INTO subjects(name, language) VALUES(?, ?)", (name.strip(), language))
        return self.subject_by_name(name)

    def subjects(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM subjects" + (" WHERE active = 1" if active_only else "") + " ORDER BY name"
        return self.q(sql)

    def create_topic(self, subject_id: int, title: str, test_date: date | None, summary: str | None = None) -> int:
        return self.run(
            "INSERT INTO topics(subject_id, title, test_date, summary, created_at) VALUES(?, ?, ?, ?, ?)",
            (subject_id, title, test_date.isoformat() if test_date else None, summary, now_iso()),
        )

    def topic(self, topic_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT t.*, s.name AS subject FROM topics t JOIN subjects s ON s.id = t.subject_id WHERE t.id = ?",
            (topic_id,),
        )

    def topics(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT t.*, s.name AS subject FROM topics t JOIN subjects s ON s.id = t.subject_id WHERE s.active = 1"
        params: tuple = ()
        if status:
            sql += " AND t.status = ?"
            params = (status,)
        return self.q(sql + " ORDER BY t.test_date IS NULL, t.test_date, t.id", params)

    def close_finished_topics(self, today: date) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE topics SET status = 'review' WHERE status = 'active' AND test_date IS NOT NULL AND test_date < ?",
                (today.isoformat(),),
            )
        return cur.rowcount

    # ---- materials and questions -----------------------------------------
    def add_material(self, topic_id: int | None, filename: str | None, text: str) -> int:
        return self.run(
            "INSERT INTO materials(topic_id, filename, text, created_at) VALUES(?, ?, ?, ?)",
            (topic_id, filename, text, now_iso()),
        )

    def add_question(self, topic_id: int, kind: str, prompt: str, answer: str, options: list[str] | None = None,
                     rubric: str | None = None, source_ref: str | None = None) -> int:
        return self.run(
            "INSERT INTO questions(topic_id, kind, prompt, answer, options, rubric, source_ref, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_id, kind, prompt, answer, json.dumps(options, ensure_ascii=False) if options else None,
             rubric, source_ref, now_iso()),
        )

    def questions(self, topic_id: int, approved: bool | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM questions WHERE topic_id = ? AND removed = 0"
        if approved is not None:
            sql += f" AND approved = {1 if approved else 0}"
        return self.q(sql + " ORDER BY id", (topic_id,))

    def pending_topics(self) -> list[sqlite3.Row]:
        return self.q(
            "SELECT DISTINCT t.*, s.name AS subject FROM topics t JOIN subjects s ON s.id = t.subject_id "
            "JOIN questions q ON q.topic_id = t.id WHERE q.approved = 0 AND q.removed = 0 ORDER BY t.id"
        )

    def approve_topic(self, topic_id: int) -> int:
        rows = self.questions(topic_id, approved=False)
        for row in rows:
            self.run("UPDATE questions SET approved = 1 WHERE id = ?", (row["id"],))
            self.ensure_card("q", row["id"])
        return len(rows)

    def remove_question(self, question_id: int) -> None:
        self.run("UPDATE questions SET removed = 1 WHERE id = ?", (question_id,))
        self.run("DELETE FROM attempts WHERE card_id IN (SELECT id FROM cards WHERE item_type = 'q' AND item_id = ?)", (question_id,))
        self.run("DELETE FROM cards WHERE item_type = 'q' AND item_id = ?", (question_id,))

    # ---- vocabulary -------------------------------------------------------
    def create_vocab_list(self, subject_id: int, title: str, start: date, test_date: date | None) -> int:
        return self.run(
            "INSERT INTO vocab_lists(subject_id, title, start_date, test_date, created_at) VALUES(?, ?, ?, ?, ?)",
            (subject_id, title, start.isoformat(), test_date.isoformat() if test_date else None, now_iso()),
        )

    def add_word(self, list_id: int, term: str, translation: str, term_alts: list[str], translation_alts: list[str]) -> int:
        return self.run(
            "INSERT INTO vocab_words(list_id, term, translation, term_alts, translation_alts) VALUES(?, ?, ?, ?, ?)",
            (list_id, term, translation, json.dumps(term_alts, ensure_ascii=False), json.dumps(translation_alts, ensure_ascii=False)),
        )

    def words(self, list_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM vocab_words WHERE list_id = ? ORDER BY id", (list_id,))

    def swap_words(self, list_id: int) -> None:
        self.run(
            "UPDATE vocab_words SET term = translation, translation = term, "
            "term_alts = translation_alts, translation_alts = term_alts WHERE list_id = ?",
            (list_id,),
        )

    def vocab_list(self, list_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT v.*, s.name AS subject, s.language FROM vocab_lists v JOIN subjects s ON s.id = v.subject_id WHERE v.id = ?",
            (list_id,),
        )

    def vocab_lists(self, approved_only: bool = True) -> list[sqlite3.Row]:
        sql = ("SELECT v.*, s.name AS subject, s.language FROM vocab_lists v JOIN subjects s ON s.id = v.subject_id "
               "WHERE s.active = 1")
        if approved_only:
            sql += " AND v.approved = 1"
        return self.q(sql + " ORDER BY v.start_date, v.id")

    def delete_vocab_list(self, list_id: int) -> list[int]:
        """Removes a word list with its words, cards and answers. Returns the removed card ids."""
        card_ids = [r["id"] for r in self.q(
            "SELECT c.id FROM cards c JOIN vocab_words w ON w.id = c.item_id WHERE c.item_type = 'w' AND w.list_id = ?",
            (list_id,))]
        for card_id in card_ids:
            self.run("DELETE FROM attempts WHERE card_id = ?", (card_id,))
            self.run("DELETE FROM cards WHERE id = ?", (card_id,))
        self.run("DELETE FROM vocab_words WHERE list_id = ?", (list_id,))
        self.run("DELETE FROM vocab_lists WHERE id = ?", (list_id,))
        return card_ids

    def approve_vocab_list(self, list_id: int) -> int:
        self.run("UPDATE vocab_lists SET approved = 1 WHERE id = ?", (list_id,))
        words = self.words(list_id)
        for word in words:
            self.ensure_card("w", word["id"], "to_sv")
            self.ensure_card("w", word["id"], "from_sv")
        return len(words)

    # ---- cards and attempts ----------------------------------------------
    def ensure_card(self, item_type: str, item_id: int, direction: str = "") -> int:
        row = self.one("SELECT id FROM cards WHERE item_type = ? AND item_id = ? AND direction = ?", (item_type, item_id, direction))
        if row:
            return row["id"]
        return self.run(
            "INSERT INTO cards(item_type, item_id, direction, due_at) VALUES(?, ?, ?, ?)",
            (item_type, item_id, direction, NEW_CARD_DUE),
        )

    def card(self, card_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM cards WHERE id = ?", (card_id,))

    def record_attempt(self, card_id: int, answer: str, score: int, feedback: str | None, when: datetime | None = None) -> int:
        when = when or datetime.now()
        card = self.card(card_id)
        if score >= 2:
            box = min(card["box"] + 1, len(BOX_INTERVALS) - 1)
        elif score == 1:
            box = card["box"]
        else:
            box = 0
        due = datetime.combine(when.date() + timedelta(days=BOX_INTERVALS[box]), datetime.min.time())
        with self.conn:
            self.conn.execute(
                "UPDATE cards SET box = ?, due_at = ?, seen = seen + 1, correct = correct + ? WHERE id = ?",
                (box, due.isoformat(timespec="seconds"), 1 if score >= 2 else 0, card_id),
            )
            cur = self.conn.execute(
                "INSERT INTO attempts(card_id, answer, score, feedback, created_at) VALUES(?, ?, ?, ?, ?)",
                (card_id, answer, score, feedback, when.isoformat(timespec="seconds")),
            )
        return cur.lastrowid

    def flag_attempt(self, attempt_id: int) -> None:
        self.run("UPDATE attempts SET flagged = 1 WHERE id = ?", (attempt_id,))

    def topic_cards(self, topic_id: int) -> list[sqlite3.Row]:
        return self.q(
            "SELECT c.*, q.prompt, q.kind FROM cards c JOIN questions q ON q.id = c.item_id "
            "WHERE c.item_type = 'q' AND q.topic_id = ? AND q.approved = 1 AND q.removed = 0",
            (topic_id,),
        )

    def list_cards(self, list_id: int, direction: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, w.term, w.translation FROM cards c JOIN vocab_words w ON w.id = c.item_id "
               "WHERE c.item_type = 'w' AND w.list_id = ?")
        params: list = [list_id]
        if direction:
            sql += " AND c.direction = ?"
            params.append(direction)
        return self.q(sql, params)

    def review_cards(self, now: datetime, limit: int) -> list[sqlite3.Row]:
        """Due question cards from topics in review mode (earlier topic weeks)."""
        return self.q(
            "SELECT c.* FROM cards c JOIN questions q ON q.id = c.item_id JOIN topics t ON t.id = q.topic_id "
            "JOIN subjects s ON s.id = t.subject_id "
            "WHERE c.item_type = 'q' AND t.status = 'review' AND s.active = 1 AND q.removed = 0 AND c.due_at <= ? "
            "ORDER BY c.box, c.due_at LIMIT ?",
            (now.isoformat(timespec="seconds"), limit),
        )

    def attempts_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.q(
            "SELECT a.*, c.item_type, c.item_id, c.direction FROM attempts a JOIN cards c ON c.id = a.card_id "
            "WHERE a.created_at >= ? ORDER BY a.id",
            (since.isoformat(timespec="seconds"),),
        )
