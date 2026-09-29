"""Decides what the student gets in each time slot. Pure logic over the database, so it is easy to test."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import holidays as holiday_calendar

from .config import Config, weekday_index
from .db import Database

SLOTS = ("warmup", "afternoon", "evening", "saturday")
PRACTICE_TEST_MAX = 15
REVIEW_EXTRA = 1  # old-topic questions added to the end of a topic batch


@dataclass
class Plan:
    kind: str  # 'topic', 'topic_test', 'review', 'vocab', 'warmup'
    title: str
    card_ids: list[int] = field(default_factory=list)
    mode: str = ""  # vocabulary: 'intro', 'typed', 'missed', 'mixed', 'test'
    round_size: int = 0  # vocabulary: words per round (0 = one round)


_SWEDISH_HOLIDAYS: dict[int, holiday_calendar.HolidayBase] = {}


def is_public_holiday(day: date) -> bool:
    if day.year not in _SWEDISH_HOLIDAYS:
        _SWEDISH_HOLIDAYS[day.year] = holiday_calendar.SE(years=day.year)
    name = _SWEDISH_HOLIDAYS[day.year].get(day)
    return bool(name) and name != "Söndag"  # the calendar lists every Sunday too


def is_day_off(day: date, config: Config, db: Database) -> str | None:
    """Returns why there are no quizzes on this day, or None."""
    paused = db.get_setting("paused_until")
    if paused and day <= date.fromisoformat(paused):
        return "pausad"
    for holiday in config.holidays:
        if holiday.start <= day <= holiday.end:
            return holiday.name
    for holiday in json.loads(db.get_setting("holidays", "[]")):
        if holiday["from"] <= day.isoformat() <= holiday["to"]:
            return holiday["name"]
    if is_public_holiday(day):
        return "helgdag"
    return None


def _due_first(cards, now: datetime, rng: random.Random) -> list:
    """Due cards first (weakest box first), then unseen, keeping the generated easy-to-hard order."""
    now_iso = now.isoformat(timespec="seconds")
    due = [c for c in cards if c["due_at"] <= now_iso]
    rng.shuffle(due)
    due.sort(key=lambda c: (c["box"], c["seen"] > 0))
    return due


def _hardest(cards, count: int) -> list:
    seen = [c for c in cards if c["seen"] > 0]
    seen.sort(key=lambda c: (c["box"], c["correct"] / max(c["seen"], 1)))
    return seen[:count]


# ---- topic weeks -----------------------------------------------------------

def current_topic(db: Database, today: date):
    for topic in db.topics(status="active"):
        if topic["test_date"] and date.fromisoformat(topic["test_date"]) >= today and db.topic_cards(topic["id"]):
            return topic
    # A topic without a test date is used when nothing else is scheduled.
    for topic in db.topics(status="active"):
        if not topic["test_date"] and db.topic_cards(topic["id"]):
            return topic
    return None


def plan_afternoon(db: Database, config: Config, now: datetime, rng: random.Random) -> list[Plan]:
    today = now.date()
    if today.weekday() >= 5:
        return []
    size = config.schedule["questions_per_batch"]
    topic = current_topic(db, today)
    if topic is None:
        review = db.review_cards(now, size)
        return [Plan("review", "Repetition", [c["id"] for c in review])] if review else []

    cards = db.topic_cards(topic["id"])
    label = f"{topic['subject']}: {topic['title']}"
    if topic["test_date"]:
        days_left = (date.fromisoformat(topic["test_date"]) - today).days
        if days_left == 0:
            return []  # test day: only the morning warm-up
        if days_left == 1:
            chosen = list(cards)
            rng.shuffle(chosen)
            return [Plan("topic_test", label, [c["id"] for c in chosen[:PRACTICE_TEST_MAX]])]

    chosen = _due_first(cards, now, rng)[:size]
    if not chosen:
        return []
    card_ids = [c["id"] for c in chosen]
    if rng.random() < 0.5:
        card_ids += [c["id"] for c in db.review_cards(now, REVIEW_EXTRA)]
    return [Plan("topic", label, card_ids)]


def plan_saturday(db: Database, config: Config, now: datetime, rng: random.Random) -> list[Plan]:
    review = db.review_cards(now, config.schedule["questions_per_batch"] + 2)
    return [Plan("review", "Repetition av tidigare temaveckor", [c["id"] for c in review])] if review else []


# ---- vocabulary --------------------------------------------------------------

def _list_dates(row) -> tuple[date, date | None]:
    return date.fromisoformat(row["start_date"]), date.fromisoformat(row["test_date"]) if row["test_date"] else None


def vocab_lists_for(db: Database, today: date) -> tuple[object | None, object | None]:
    """Returns (list with a test tomorrow or today, list being learned now)."""
    testing, learning = None, None
    candidates = []
    for row in db.vocab_lists():
        start, test = _list_dates(row)
        if start > today or (test and test < today):
            continue
        candidates.append((test or date.max, row))
    candidates.sort(key=lambda pair: pair[0])
    for test, row in candidates:
        if test in (today, today + timedelta(days=1)) and testing is None:
            testing = row
        elif learning is None:
            learning = row
    return testing, learning


def effective_start(db: Database, row) -> date:
    """A new list starts after the previous list's test, even if it arrived earlier."""
    start, _ = _list_dates(row)
    for other in db.vocab_lists():
        if other["id"] == row["id"] or other["subject_id"] != row["subject_id"]:
            continue
        other_start, other_test = _list_dates(other)
        if other_test and other_start <= start <= other_test:
            start = max(start, other_test)
    return start


def plan_evening(db: Database, config: Config, now: datetime, rng: random.Random) -> list[Plan]:
    today = now.date()
    per_round = config.vocabulary["words_per_round"]
    testing, learning = vocab_lists_for(db, today)

    if testing is not None and testing["test_date"] == (today + timedelta(days=1)).isoformat():
        cards = db.list_cards(testing["id"])
        by_word: dict[int, list] = {}
        for card in cards:
            by_word.setdefault(card["item_id"], []).append(card)
        chosen = []
        for word_cards in by_word.values():
            from_sv = [c for c in word_cards if c["direction"] == "from_sv"]
            chosen.append((from_sv or word_cards)[0])  # tests usually ask Swedish -> foreign
        rng.shuffle(chosen)
        return [Plan("vocab", f"{testing['subject']}: övningsprov", [c["id"] for c in chosen], "test", per_round)]

    if learning is None:
        return []
    start = effective_start(db, learning)
    if start > today:
        return []
    day = (today - start).days
    label = f"{learning['subject']}: {learning['title']}"
    if day == 0:
        cards = db.list_cards(learning["id"], "to_sv")
        return [Plan("vocab", label, [c["id"] for c in cards], "intro", per_round)]
    if day == 1:
        cards = list(db.list_cards(learning["id"]))
        rng.shuffle(cards)
        return [Plan("vocab", label, [c["id"] for c in cards], "typed", per_round)]
    if today.weekday() == 5:
        cards = list(db.list_cards(learning["id"]))
        rng.shuffle(cards)
        return [Plan("vocab", label, [c["id"] for c in cards[: per_round * 2]], "mixed", per_round)]
    cards = _due_first(db.list_cards(learning["id"]), now, rng)
    weak = [c for c in cards if c["box"] <= 1] or cards
    return [Plan("vocab", label, [c["id"] for c in weak[:per_round]], "missed", per_round)] if weak else []


def plan_warmup(db: Database, config: Config, now: datetime, rng: random.Random) -> list[Plan]:
    today = now.date()
    plans = []
    for topic in db.topics(status="active"):
        if topic["test_date"] == today.isoformat():
            hardest = _hardest(db.topic_cards(topic["id"]), 3)
            if hardest:
                plans.append(Plan("warmup", f"Uppvärmning inför provet i {topic['subject']}", [c["id"] for c in hardest]))
    testing, _ = vocab_lists_for(db, today)
    if testing is not None and testing["test_date"] == today.isoformat():
        hardest = _hardest(db.list_cards(testing["id"], "from_sv"), 5)
        if hardest:
            plans.append(Plan("vocab", f"Uppvärmning inför glosförhöret i {testing['subject']}", [c["id"] for c in hardest], "test"))
    return plans


PLANNERS = {
    "warmup": plan_warmup,
    "afternoon": plan_afternoon,
    "evening": plan_evening,
    "saturday": plan_saturday,
}


def plan_slot(slot: str, db: Database, config: Config, now: datetime, rng: random.Random | None = None) -> list[Plan]:
    rng = rng or random.Random()
    if is_day_off(now.date(), config, db):
        return []
    if slot == "saturday" and now.weekday() != 5:
        return []
    db.close_finished_topics(now.date())
    return [p for p in PLANNERS[slot](db, config, now, rng) if p.card_ids]


def next_weekday(today: date, name: str) -> date:
    """The next date (after today) that falls on the given weekday."""
    days = (weekday_index(name) - today.weekday()) % 7 or 7
    return today + timedelta(days=days)
