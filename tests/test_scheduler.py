import json
import random
from datetime import date, datetime, timedelta

from app.scheduler import is_day_off, plan_slot
from app.session import build_session

MON, TUE, WED, THU, FRI, SAT = (date(2026, 9, 28) + timedelta(days=i) for i in range(6))


def at(day, hour=16, minute=30):
    return datetime(day.year, day.month, day.day, hour, minute)


def make_topic(db, title="Industriella revolutionen", test=FRI, questions=8):
    subject = db.ensure_subject("SO")
    topic = db.create_topic(subject["id"], title, test)
    for i in range(questions):
        db.add_question(topic, "mc" if i % 2 else "short", f"Fråga {i}?", f"svar {i}",
                        [f"svar {i}", "x", "y"] if i % 2 else None)
    db.approve_topic(topic)
    return topic


def make_words(db, start, test, count=12, title="v40"):
    subject = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(subject["id"], title, start, test)
    for i in range(count):
        db.add_word(list_id, f"palabra{i}", f"ord{i}", [], [])
    db.approve_vocab_list(list_id)
    return list_id


def test_afternoon_batch_during_topic_week(db, config):
    make_topic(db)
    plans = plan_slot("afternoon", db, config, at(MON), random.Random(1))
    assert len(plans) == 1 and plans[0].kind == "topic"
    assert len(plans[0].card_ids) == config.schedule["questions_per_batch"]


def test_thursday_is_practice_test_and_friday_afternoon_is_free(db, config):
    make_topic(db)
    thursday = plan_slot("afternoon", db, config, at(THU), random.Random(1))
    assert thursday[0].kind == "topic_test" and len(thursday[0].card_ids) == 8
    assert plan_slot("afternoon", db, config, at(FRI), random.Random(1)) == []


def test_warmup_on_test_morning_uses_hardest_questions(db, config):
    topic = make_topic(db)
    for card in db.topic_cards(topic)[:4]:
        db.record_attempt(card["id"], "x", 0, "", at(WED))
    plans = plan_slot("warmup", db, config, at(FRI, 7, 15), random.Random(1))
    assert plans and plans[0].kind == "warmup" and len(plans[0].card_ids) == 3


def test_no_afternoon_quiz_on_weekends(db, config):
    make_topic(db, test=date(2026, 10, 9))
    assert plan_slot("afternoon", db, config, at(SAT), random.Random(1)) == []


def test_spanish_cycle_monday_practice_test_then_new_list_on_tuesday(db, config):
    old = make_words(db, date(2026, 9, 21), TUE, title="v39")
    new = make_words(db, MON, date(2026, 10, 6), title="v40")

    monday = plan_slot("evening", db, config, at(MON, 19), random.Random(1))
    assert monday[0].mode == "test"
    assert {db.card(c)["item_id"] for c in monday[0].card_ids} == {w["id"] for w in db.words(old)}

    tuesday = plan_slot("evening", db, config, at(TUE, 19), random.Random(1))
    assert tuesday[0].mode == "intro"
    assert {db.card(c)["item_id"] for c in tuesday[0].card_ids} == {w["id"] for w in db.words(new)}

    wednesday = plan_slot("evening", db, config, at(WED, 19), random.Random(1))
    assert wednesday[0].mode == "typed" and len(wednesday[0].card_ids) == 24


def test_tuesday_morning_warmup_before_word_test(db, config):
    list_id = make_words(db, date(2026, 9, 21), TUE)
    for card in db.list_cards(list_id, "from_sv")[:6]:
        db.record_attempt(card["id"], "x", 0, "", at(MON))
    plans = plan_slot("warmup", db, config, at(TUE, 7, 15), random.Random(1))
    assert plans[0].kind == "vocab" and len(plans[0].card_ids) == 5


def test_holidays_and_pause_stop_everything(db, config):
    make_topic(db)
    db.set_setting("holidays", json.dumps([{"name": "Höstlov", "from": "2026-09-28", "to": "2026-10-02"}]))
    assert is_day_off(MON, config, db) == "Höstlov"
    assert plan_slot("afternoon", db, config, at(MON), random.Random(1)) == []
    db.set_setting("holidays", "[]")
    db.set_setting("paused_until", TUE.isoformat())
    assert plan_slot("afternoon", db, config, at(TUE), random.Random(1)) == []
    assert plan_slot("afternoon", db, config, at(WED), random.Random(1))


def test_public_holiday_but_not_ordinary_sunday(db, config):
    assert is_day_off(date(2026, 12, 25), config, db) == "helgdag"
    assert is_day_off(date(2026, 10, 4), config, db) is None


def test_saturday_reviews_finished_topics(db, config):
    make_topic(db, title="Gammalt", test=date(2026, 9, 25))
    plans = plan_slot("saturday", db, config, at(SAT, 11), random.Random(1))
    assert plans and plans[0].kind == "review"


def test_session_for_vocab_intro_uses_multiple_choice(db, config):
    make_words(db, MON, date(2026, 10, 6))
    plan = plan_slot("evening", db, config, at(MON, 19), random.Random(1))[0]
    session = build_session(db, plan, random.Random(1))
    item = session.next_item()
    assert item.kind == "mc" and item.expected[0] in item.options and len(item.options) == 3
    assert item.language == "sv" and "<b>palabra" in item.prompt


def test_list_sent_as_two_parts_starts_the_same_evening(db, config):
    # Magnus's case: 2 + 30 words sent on Wednesday for the same test.
    make_words(db, date(2026, 9, 30), date(2026, 10, 6), count=2)
    make_words(db, date(2026, 9, 30), date(2026, 10, 6), count=30)
    plans = plan_slot("evening", db, config, datetime(2026, 9, 30, 19, 0), random.Random(1))
    assert len(plans) == 1 and plans[0].mode == "intro"
    assert len(plans[0].card_ids) == 32  # one to_sv card per word, both parts
