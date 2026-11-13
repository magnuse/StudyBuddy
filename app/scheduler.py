"""Decides what the student gets in each time slot. Pure logic over the database, so it is easy to test."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import holidays as holiday_calendar

from .config import Config, weekday_index
from .db import Database
from .vocab import opposite_gender

SLOTS = ("warmup", "afternoon", "evening", "saturday")
PRACTICE_TEST_MAX = 15
REVIEW_EXTRA = 1  # old-topic questions added to the end of a topic batch
SHORT_PERIOD_DAYS = 5
SHORT_PERIOD_EXTRA = 10


@dataclass
class Plan:
    kind: str  # 'topic', 'topic_test', 'review', 'vocab', 'warmup'
    title: str
    card_ids: list[int] = field(default_factory=list)
    mode: str = ""  # vocabulary: 'intro', 'typed', 'missed', 'mixed', 'test'
    round_size: int = 0  # vocabulary: words per round (0 = one round)
    max_questions: int = 0  # vocabulary: questions per session including repeats (0 = no limit)


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


def _same_test(row, other) -> bool:
    return other["subject_id"] == row["subject_id"] and other["test_date"] == row["test_date"]


def _cards(db: Database, row, direction: str | None = None) -> list:
    """Cards of a list plus any other list for the same test (a list sent as several photos)."""
    cards = []
    for other in db.vocab_lists():
        if other["id"] == row["id"] or (row["test_date"] and _same_test(row, other)):
            cards += db.list_cards(other["id"], direction)
    return cards


def effective_start(db: Database, row) -> date:
    """A new list starts after the previous list's test, even if it arrived earlier."""
    start, test = _list_dates(row)
    for other in db.vocab_lists():
        if other["id"] == row["id"] or other["subject_id"] != row["subject_id"]:
            continue
        other_start, other_test = _list_dates(other)
        if other_test and (test is None or other_test < test) and other_start <= start <= other_test:
            start = max(start, other_test)
    return start


def _is_short_period(vocab_row, ref_date: date) -> bool:
    """True when there are fewer than 5 days from ref_date until the test."""
    if vocab_row is None or not vocab_row["test_date"]:
        return False
    return (date.fromisoformat(vocab_row["test_date"]) - ref_date).days < SHORT_PERIOD_DAYS


def _session_limits(config: Config, short_period: bool = False) -> tuple[int, int]:
    """Returns (target_initial_cards, max_questions) for a vocabulary session."""
    limit = config.vocabulary.get("max_questions") or 25
    if short_period:
        limit += SHORT_PERIOD_EXTRA
    return max(1, limit - REPEAT_ROOM), limit


def _failed_card_ids(db: Database, vocab_row, seen: set[int]) -> list[int]:
    subject_id = vocab_row["subject_id"]
    current_list_id = vocab_row["id"]
    rows = db.q(
        """
        SELECT c.id,
               (w.list_id = ?) AS is_current,
               c.box,
               MAX(a.created_at) AS last_answered,
               (SELECT score FROM attempts WHERE card_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_score,
               (SELECT COUNT(*) FROM attempts WHERE card_id = c.id AND score < 2) AS fail_count
        FROM cards c
        JOIN vocab_words w ON w.id = c.item_id
        JOIN vocab_lists v ON v.id = w.list_id
        JOIN attempts a ON a.card_id = c.id
        WHERE c.item_type = 'w' AND v.subject_id = ?
        GROUP BY c.id
        HAVING last_score < 2 OR fail_count > 0 OR c.box <= 1
        ORDER BY is_current DESC, (last_score < 2) DESC, fail_count DESC, c.box ASC, last_answered DESC
        """,
        (current_list_id, subject_id),
    )
    return [r["id"] for r in rows if r["id"] not in seen]


def _fill_cards(
    db: Database,
    initial_card_ids: list[int],
    vocab_row,
    now: datetime,
    rng: random.Random,
    target: int = 20,
) -> list[int]:
    if vocab_row is None:
        return initial_card_ids[:target]

    chosen: list[int] = list(initial_card_ids)
    seen: set[int] = set(chosen)
    if len(chosen) >= target:
        return chosen[:target]

    # 1. Opposite gender cards for words in this list (or sibling lists for the same test)
    language = vocab_row["language"] if "language" in vocab_row.keys() else "es"
    words = []
    for other in db.vocab_lists():
        if other["id"] == vocab_row["id"] or (vocab_row["test_date"] and _same_test(vocab_row, other)):
            words.extend(db.words(other["id"]))
    rng.shuffle(words)

    if (language or "es") == "es":
        for word in words:
            opp = opposite_gender(word["term"], word["translation"], json.loads(word["term_alts"]))
            if opp:
                cid = db.ensure_card("w", word["id"], "from_sv_opp")
                if cid not in seen:
                    seen.add(cid)
                    chosen.append(cid)
                    if len(chosen) >= target:
                        return chosen[:target]

    # 2. Retest failed cards (from this list and earlier lists of the subject)
    failed = _failed_card_ids(db, vocab_row, seen)
    for cid in failed:
        seen.add(cid)
        chosen.append(cid)
        if len(chosen) >= target:
            return chosen[:target]

    # 3. Other cards from the current list (and sibling lists)
    list_cards = list(_cards(db, vocab_row))
    rng.shuffle(list_cards)
    for card in list_cards:
        cid = card["id"]
        if cid not in seen:
            seen.add(cid)
            chosen.append(cid)
            if len(chosen) >= target:
                return chosen[:target]

    # 4. Cards from earlier lists of the same subject
    earlier_cards = db.q(
        "SELECT c.id FROM cards c JOIN vocab_words w ON w.id = c.item_id JOIN vocab_lists v ON v.id = w.list_id "
        "WHERE c.item_type = 'w' AND v.subject_id = ? AND v.id != ?",
        (vocab_row["subject_id"], vocab_row["id"]),
    )
    earlier_ids = [r["id"] for r in earlier_cards if r["id"] not in seen]
    rng.shuffle(earlier_ids)
    for cid in earlier_ids:
        seen.add(cid)
        chosen.append(cid)
        if len(chosen) >= target:
            return chosen[:target]

    return chosen[:target]


def plan_evening(db: Database, config: Config, now: datetime, rng: random.Random) -> list[Plan]:
    today = now.date()
    per_round = config.vocabulary["words_per_round"]
    testing, learning = vocab_lists_for(db, today)

    if testing is not None and testing["test_date"] == (today + timedelta(days=1)).isoformat():
        short = _is_short_period(testing, effective_start(db, testing))
        target, max_q = _session_limits(config, short)
        cards = _cards(db, testing)
        by_word: dict[int, list] = {}
        for card in cards:
            by_word.setdefault(card["item_id"], []).append(card)
        chosen = []
        for word_cards in by_word.values():
            from_sv = [c for c in word_cards if c["direction"] == "from_sv"]
            chosen.append((from_sv or word_cards)[0])  # tests usually ask Swedish -> foreign
        rng.shuffle(chosen)
        card_ids = _fill_cards(db, [c["id"] for c in chosen], testing, now, rng, target=target)
        return [Plan("vocab", f"{testing['subject']}: övningsprov", card_ids, "test", per_round, max_q)]

    if learning is None:
        return []
    start = effective_start(db, learning)
    if start > today:
        return []
    day = (today - start).days
    label = f"{learning['subject']}: {learning['title']}"
    all_cards = list(_cards(db, learning))
    first_session_short = bool(all_cards) and all(c["seen"] == 0 for c in all_cards) and _is_short_period(learning, today)
    short = _is_short_period(learning, start) or first_session_short
    target, max_q = _session_limits(config, short)

    if day == 0:
        if short:
            cards = list(all_cards)
            rng.shuffle(cards)
            card_ids = _fill_cards(db, [c["id"] for c in cards], learning, now, rng, target=target)
            return [Plan("vocab", label, card_ids, "typed", per_round, max_q)]
        cards = _cards(db, learning, "to_sv")
        return [Plan("vocab", label, [c["id"] for c in cards], "intro", per_round, max_q)]
    if day == 1:
        cards = list(all_cards)
        rng.shuffle(cards)
        card_ids = _fill_cards(db, [c["id"] for c in cards], learning, now, rng, target=target)
        return [Plan("vocab", label, card_ids, "typed", per_round, max_q)]
    if today.weekday() == 5:
        cards = list(all_cards)
        rng.shuffle(cards)
        card_ids = _fill_cards(db, [c["id"] for c in cards], learning, now, rng, target=target)
        return [Plan("vocab", label, card_ids, "mixed", per_round, max_q)]
    cards = _due_first(all_cards, now, rng)
    weak = [c for c in cards if c["box"] <= 1] or cards
    card_ids = _fill_cards(db, [c["id"] for c in weak], learning, now, rng, target=target)
    return [Plan("vocab", label, card_ids, "missed", per_round, max_q)] if card_ids else []


def plan_vocab_now(db: Database, config: Config, now: datetime, rng: random.Random) -> Plan | None:
    """A round the student starts himself: the list with the nearest test, weakest words first."""
    today = now.date()
    testing, learning = vocab_lists_for(db, today)
    row = testing or learning
    if row is None:
        return None
    per_round = config.vocabulary["words_per_round"]
    label = f"{row['subject']}: {row['title']}"
    cards = list(_cards(db, row))
    first_session = bool(cards) and all(c["seen"] == 0 for c in cards)
    short = _is_short_period(row, effective_start(db, row)) or (first_session and _is_short_period(row, today))
    target, max_q = _session_limits(config, short)

    if first_session and not short:
        intro = [c for c in cards if c["direction"] == "to_sv"]
        rng.shuffle(intro)
        return cap_vocab(Plan("vocab", label, [c["id"] for c in intro], "intro", per_round, max_q), config)
    rng.shuffle(cards)
    cards.sort(key=lambda c: (c["box"], c["seen"] == 0, c["correct"] / max(c["seen"], 1)))  # missed words first
    card_ids = _fill_cards(db, [c["id"] for c in cards], row, now, rng, target=target)
    return cap_vocab(Plan("vocab", label, card_ids, "typed", per_round, max_q), config) if card_ids else None


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
        hardest = _hardest(_cards(db, testing, "from_sv"), 5)
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
    return [cap_vocab(p, config) for p in PLANNERS[slot](db, config, now, rng) if p.card_ids]


REPEAT_ROOM = 5  # questions kept free for missed words to come back


def cap_vocab(plan: Plan, config: Config) -> Plan:
    """Keeps a vocabulary session short: new cards up to the limit minus room for repeats."""
    limit = plan.max_questions or config.vocabulary.get("max_questions") or 0
    if plan.kind == "vocab" and limit:
        plan.max_questions = limit
        plan.card_ids = plan.card_ids[: max(1, limit - REPEAT_ROOM)]
    return plan


def next_weekday(today: date, name: str) -> date:
    """The next date (after today) that falls on the given weekday."""
    days = (weekday_index(name) - today.weekday()) % 7 or 7
    return today + timedelta(days=days)
