"""A small read-only web page for the parents: word lists, topics with questions, and the student's answers.

It uses only the standard library and runs in a background thread next to the bot. Every request opens
its own read-only database connection, so the page can never change anything.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

ANSWER_LIMIT = 300
SCORE_TEXT = {2: ("✅", "rätt"), 1: ("🟡", "nästan"), 0: ("❌", "fel")}

STYLE = """
:root { --bg: #fafaf8; --fg: #1d1d1b; --muted: #6b6b66; --line: #e3e3de; --card: #fff;
        --ok: #1f7a3f; --mid: #9a6a00; --bad: #b3261e; --accent: #2d5bd7; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ececea; --muted: #a3a39d; --line: #33332f; --card: #1f1f1d;
          --ok: #5cc184; --mid: #e0b44c; --bad: #ef7a6f; --accent: #8aa8ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--line); z-index: 1; }
nav { max-width: 1000px; margin: 0 auto; padding: 12px 16px; display: flex; gap: 16px; flex-wrap: wrap;
      align-items: baseline; }
nav b { margin-right: 8px; }
nav a { color: var(--muted); text-decoration: none; }
nav a.on { color: var(--fg); font-weight: 600; }
main { max-width: 1000px; margin: 0 auto; padding: 8px 16px 48px; }
h2 { margin: 28px 0 4px; font-size: 1.15em; }
.meta { color: var(--muted); font-size: 0.9em; margin: 0 0 10px; }
.wrap { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.85em; color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.ok { color: var(--ok); } .mid { color: var(--mid); } .bad { color: var(--bad); }
.dim { color: var(--muted); }
.pill { display: inline-block; font-size: 0.8em; padding: 1px 8px; border-radius: 99px;
        border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
details { margin-top: 8px; } summary { cursor: pointer; color: var(--accent); }
.summary { white-space: pre-wrap; background: var(--card); border: 1px solid var(--line); border-radius: 8px;
           padding: 10px 12px; margin-top: 6px; }
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, active: str, body: str) -> str:
    links = [("/", "Översikt"), ("/words", "Glosor"), ("/topics", "Områden och frågor"), ("/answers", "Svar")]
    nav = "".join(f'<a href="{href}" class="{"on" if href == active else ""}">{label}</a>' for href, label in links)
    return (f'<!doctype html><html lang="sv"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>StudyBuddy – {esc(title)}</title><style>{STYLE}</style></head>"
            f"<body><header><nav><b>StudyBuddy</b>{nav}</nav></header><main>{body}</main></body></html>")


def table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{h}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{c}</td>' for i, c in enumerate(row))
                   + "</tr>" for row in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def score_cell(score: int) -> str:
    icon, text = SCORE_TEXT.get(score, ("", str(score)))
    css = {2: "ok", 1: "mid", 0: "bad"}.get(score, "")
    return f'<span class="{css}">{icon} {text}</span>'


def _alts(raw: str) -> str:
    alts = json.loads(raw or "[]")
    return f'<div class="dim">även: {esc(", ".join(alts))}</div>' if alts else ""


# ---- pages -------------------------------------------------------------------------

def render_overview(conn: sqlite3.Connection) -> str:
    lists = conn.execute("SELECT COUNT(*) FROM vocab_lists WHERE approved = 1").fetchone()[0]
    words = conn.execute("SELECT COUNT(*) FROM vocab_words w JOIN vocab_lists v ON v.id = w.list_id "
                         "WHERE v.approved = 1").fetchone()[0]
    topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    questions = conn.execute("SELECT COUNT(*) FROM questions WHERE approved = 1 AND removed = 0").fetchone()[0]
    week = conn.execute("SELECT COUNT(*), COALESCE(SUM(score = 2), 0) FROM attempts "
                        "WHERE created_at >= datetime('now', 'localtime', '-7 days')").fetchone()
    rows = [["Glosor", f"{words} ord i {lists} listor", '<a href="/words">Visa</a>'],
            ["Områden", f"{questions} godkända frågor i {topics} områden", '<a href="/topics">Visa</a>'],
            ["Senaste 7 dagarna", f"{week[0]} svar, {week[1]} rätt", '<a href="/answers">Visa</a>']]
    return page("Översikt", "/", "<h2>Översikt</h2>" + table(["", "", ""], rows))


def render_words(conn: sqlite3.Connection) -> str:
    lists = conn.execute(
        "SELECT v.*, s.name AS subject FROM vocab_lists v JOIN subjects s ON s.id = v.subject_id "
        "ORDER BY COALESCE(v.test_date, v.start_date) DESC, v.id DESC").fetchall()
    if not lists:
        return page("Glosor", "/words", "<h2>Glosor</h2><p class='dim'>Inga glosor än.</p>")
    parts = []
    for vocab in lists:
        words = conn.execute(
            "SELECT w.*, "
            "  (SELECT seen FROM cards WHERE item_type = 'w' AND item_id = w.id AND direction = 'to_sv') AS to_seen,"
            "  (SELECT correct FROM cards WHERE item_type = 'w' AND item_id = w.id AND direction = 'to_sv') AS to_ok,"
            "  (SELECT seen FROM cards WHERE item_type = 'w' AND item_id = w.id AND direction = 'from_sv') AS from_seen,"
            "  (SELECT correct FROM cards WHERE item_type = 'w' AND item_id = w.id AND direction = 'from_sv') AS from_ok "
            "FROM vocab_words w WHERE w.list_id = ? ORDER BY w.id", (vocab["id"],)).fetchall()
        rows = []
        for word in words:
            def stat(seen, ok):
                if not seen:
                    return '<span class="dim">–</span>'
                css = "ok" if ok == seen else ("bad" if ok == 0 else "mid")
                return f'<span class="{css}">{ok} av {seen}</span>'
            rows.append([esc(word["term"]) + _alts(word["term_alts"]),
                         esc(word["translation"]) + _alts(word["translation_alts"]),
                         stat(word["to_seen"], word["to_ok"]), stat(word["from_seen"], word["from_ok"])])
        status = "" if vocab["approved"] else ' <span class="pill">väntar på godkännande</span>'
        parts.append(f"<h2>{esc(vocab['subject'])}: {esc(vocab['title'])}{status}</h2>"
                     f"<p class='meta'>Glosförhör {esc(vocab['test_date'] or 'inget datum')} · start "
                     f"{esc(vocab['start_date'])} · {len(words)} ord</p>"
                     + table(["Främmande språk", "Svenska", "→ svenska", "→ främmande"], rows, {2, 3}))
    return page("Glosor", "/words", "".join(parts))


def render_topics(conn: sqlite3.Connection) -> str:
    topics = conn.execute(
        "SELECT t.*, s.name AS subject FROM topics t JOIN subjects s ON s.id = t.subject_id "
        "ORDER BY COALESCE(t.test_date, t.created_at) DESC, t.id DESC").fetchall()
    if not topics:
        return page("Områden", "/topics", "<h2>Områden och frågor</h2><p class='dim'>Inga områden än.</p>")
    parts = []
    for topic in topics:
        questions = conn.execute(
            "SELECT q.*, c.seen, c.correct FROM questions q "
            "LEFT JOIN cards c ON c.item_type = 'q' AND c.item_id = q.id "
            "WHERE q.topic_id = ? ORDER BY q.id", (topic["id"],)).fetchall()
        rows = []
        for number, q in enumerate(questions, 1):
            prompt = esc(q["prompt"])
            if q["options"]:
                options = json.loads(q["options"])
                prompt += "<div class='dim'>" + " · ".join(esc(o) for o in options) + "</div>"
            answer = esc(q["answer"])
            if q["rubric"]:
                answer += f"<div class='dim'>Bedömning: {esc(q['rubric'])}</div>"
            state = ("borttagen" if q["removed"] else "godkänd" if q["approved"] else "väntar")
            result = f"{q['correct']} av {q['seen']}" if q["seen"] else "–"
            rows.append([str(number), prompt, answer, f'<span class="pill">{state}</span>', result])
        status = " (repetition)" if topic["status"] == "review" else ""
        summary = (f"<details><summary>Sammanfattning</summary><div class='summary'>{esc(topic['summary'])}</div>"
                   f"</details>" if topic["summary"] else "")
        parts.append(f"<h2>{esc(topic['subject'])}: {esc(topic['title'])}{status}</h2>"
                     f"<p class='meta'>Prov {esc(topic['test_date'] or 'inget datum')} · {len(questions)} frågor</p>"
                     + summary + (table(["#", "Fråga", "Svar", "Status", "Rätt"], rows, {0, 4}) if rows else
                                  "<p class='dim'>Inga frågor skrivna.</p>"))
    return page("Områden", "/topics", "".join(parts))


def answer_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = """
        SELECT a.created_at, a.answer, a.score, a.feedback, a.flagged, c.direction, c.item_type,
               COALESCE(s1.name, s2.name) AS subject,
               q.prompt AS q_prompt, q.answer AS q_answer, w.term, w.translation
        FROM attempts a JOIN cards c ON c.id = a.card_id
        LEFT JOIN questions q ON c.item_type = 'q' AND q.id = c.item_id
        LEFT JOIN topics t ON t.id = q.topic_id LEFT JOIN subjects s1 ON s1.id = t.subject_id
        LEFT JOIN vocab_words w ON c.item_type = 'w' AND w.id = c.item_id
        LEFT JOIN vocab_lists v ON v.id = w.list_id LEFT JOIN subjects s2 ON s2.id = v.subject_id
        ORDER BY a.id DESC"""
    return conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()


def render_answers(conn: sqlite3.Connection, show_all: bool = False) -> str:
    rows = answer_rows(conn, None if show_all else ANSWER_LIMIT)
    if not rows:
        return page("Svar", "/answers", "<h2>Svar</h2><p class='dim'>Inga svar än.</p>")
    cells = []
    for r in rows:
        if r["item_type"] == "w":
            question, expected = ((r["term"], r["translation"]) if r["direction"] == "to_sv"
                                  else (r["translation"], r["term"]))
            arrow = "→ svenska" if r["direction"] == "to_sv" else "→ främmande språk"
            question = f"{esc(question)} <span class='dim'>{arrow}</span>"
        else:
            question, expected = esc(r["q_prompt"]), r["q_answer"]
        feedback = r["feedback"] or ""  # already escaped Telegram HTML from the grader
        flag = " 🚩" if r["flagged"] else ""
        cells.append([esc(r["created_at"].replace("T", " ")[:16]), esc(r["subject"]), question,
                      esc(r["answer"]), esc(expected), score_cell(r["score"]) + flag,
                      f"<span class='dim'>{feedback}</span>"])
    more = ("" if show_all or len(rows) < ANSWER_LIMIT
            else f"<p class='meta'>Visar de senaste {ANSWER_LIMIT}. <a href='/answers?all=1'>Visa alla</a></p>")
    return page("Svar", "/answers", "<h2>Svar</h2>" + more +
                table(["När", "Ämne", "Fråga", "Hans svar", "Rätt svar", "Resultat", "Återkoppling"], cells))


# ---- server ------------------------------------------------------------------------

def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def make_handler(db_path: Path, password: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            if password and not self._authorized():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="StudyBuddy"')
                self.end_headers()
                return
            url = urlparse(self.path)
            routes = {"/": render_overview, "/words": render_words, "/topics": render_topics}
            try:
                conn = connect_readonly(db_path)
                try:
                    if url.path == "/answers":
                        body = render_answers(conn, show_all=bool(parse_qs(url.query).get("all")))
                    elif url.path in routes:
                        body = routes[url.path](conn)
                    else:
                        self.send_error(404)
                        return
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.warning("Web page could not read the database: %s", exc)
                self.send_error(503, "Databasen kunde inte läsas")
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return secrets.compare_digest(given, password)

        def log_message(self, format, *args):  # keep the bot's log readable
            log.debug("web: " + format, *args)

    return Handler


def start_web(db_path: Path, port: int, password: str | None = None) -> ThreadingHTTPServer | None:
    """Starts the page in a background thread. Port 0 turns it off."""
    if not port:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path, password))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Web page on port %d%s", port, " (password protected)" if password else "")
    return server
