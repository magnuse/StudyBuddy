"""A quiz session: the items of one batch, the student's progress through them, and the results."""

from __future__ import annotations

import html
import json
import random
from dataclasses import dataclass, field
from datetime import datetime

from .db import Database
from .scheduler import Plan

MAX_REASKS = 2  # a missed word comes back at most this many times in one session


@dataclass
class QuizItem:
    card_id: int
    kind: str  # 'mc', 'typed' (vocabulary), 'free' (graded by a model)
    prompt: str  # Telegram HTML
    expected: list[str]
    options: list[str] = field(default_factory=list)
    language: str | None = None  # language of the expected answer, for vocabulary
    subject: str = ""
    question: dict | None = None  # the question row, for free-text grading
    asked: int = 0

    @property
    def plain_prompt(self) -> str:
        return html.escape(html.unescape(self.prompt.replace("<b>", "").replace("</b>", "")))


@dataclass
class Result:
    item: QuizItem
    answer: str
    score: int | None = None  # None while grading is still running
    feedback: str = ""
    attempt_id: int | None = None


@dataclass
class Session:
    plan: Plan
    queue: list[QuizItem]
    results: list[Result] = field(default_factory=list)
    current: QuizItem | None = None
    started: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    answered_in_round: int = 0
    pending_grades: int = 0

    @property
    def is_vocab(self) -> bool:
        return self.plan.kind == "vocab"

    @property
    def total(self) -> int:
        return len({r.item.card_id for r in self.results} | {i.card_id for i in self.queue}
                   | ({self.current.card_id} if self.current else set()))

    def next_item(self) -> QuizItem | None:
        self.current = self.queue.pop(0) if self.queue else None
        if self.current:
            self.current.asked += 1
        return self.current

    def round_finished(self) -> bool:
        size = self.plan.round_size
        return bool(size) and self.answered_in_round >= size and bool(self.queue)

    def requeue_missed(self, item: QuizItem) -> None:
        """Missed vocabulary comes back a few questions later in the same session."""
        if self.is_vocab and item.asked <= MAX_REASKS:
            position = min(len(self.queue), 3)
            self.queue.insert(position, item)

    def score_summary(self) -> tuple[int, int]:
        """(right on first try, number of distinct items)."""
        first: dict[int, Result] = {}
        for result in self.results:
            first.setdefault(result.item.card_id, result)
        right = sum(1 for r in first.values() if (r.score or 0) >= 2)
        return right, len(first)


def _vocab_item(db: Database, card, word, vocab_list, mode: str, rng: random.Random) -> QuizItem:
    term_forms = [word["term"]] + json.loads(word["term_alts"])
    translation_forms = [word["translation"]] + json.loads(word["translation_alts"])
    language = vocab_list["language"] or "es"
    if card["direction"] == "to_sv":
        prompt = f"Översätt till svenska: <b>{html.escape(word['term'])}</b>"
        expected, answer_language = translation_forms, "sv"
    else:
        prompt = f"Översätt till {LANGUAGE_NAMES_SV.get(language, language)}: <b>{html.escape(word['translation'])}</b>"
        expected, answer_language = term_forms, language
    if mode == "intro":
        others = [w for w in db.words(vocab_list["id"]) if w["id"] != word["id"]]
        rng.shuffle(others)
        key = "translation" if card["direction"] == "to_sv" else "term"
        options = [expected[0]] + [w[key] for w in others[:2]]
        rng.shuffle(options)
        if len(options) >= 2:
            return QuizItem(card["id"], "mc", prompt, expected, options, answer_language, vocab_list["subject"])
    return QuizItem(card["id"], "typed", prompt, expected, [], answer_language, vocab_list["subject"])


LANGUAGE_NAMES_SV = {"es": "spanska", "en": "engelska", "de": "tyska", "fr": "franska", "sv": "svenska"}


def build_session(db: Database, plan: Plan, rng: random.Random | None = None) -> Session:
    rng = rng or random.Random()
    items: list[QuizItem] = []
    for card_id in plan.card_ids:
        card = db.card(card_id)
        if card is None:
            continue
        if card["item_type"] == "q":
            question = db.one("SELECT * FROM questions WHERE id = ? AND removed = 0", (card["item_id"],))
            if question is None:
                continue
            topic = db.topic(question["topic_id"])
            if question["kind"] == "mc":
                options = json.loads(question["options"] or "[]")
                rng.shuffle(options)
                items.append(QuizItem(card_id, "mc", html.escape(question["prompt"]), [question["answer"]], options,
                                      subject=topic["subject"], question=dict(question)))
            else:
                items.append(QuizItem(card_id, "free", html.escape(question["prompt"]), [question["answer"]],
                                      subject=topic["subject"], question=dict(question)))
        else:
            word = db.one("SELECT * FROM vocab_words WHERE id = ?", (card["item_id"],))
            if word is None:
                continue
            vocab_list = db.vocab_list(word["list_id"])
            items.append(_vocab_item(db, card, word, vocab_list, plan.mode, rng))
    return Session(plan, items)
