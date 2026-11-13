import json
import random
from datetime import date, datetime, timedelta

from app.scheduler import is_day_off, plan_slot, plan_vocab_now
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
    assert wednesday[0].mode == "typed" and len(wednesday[0].card_ids) == 20  # capped, room for repeats


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
    cards = [db.card(c) for c in plans[0].card_ids]
    assert len(cards) == 20 and all(c["direction"] == "to_sv" for c in cards)  # capped at 25 minus repeats
    assert {db.one("SELECT list_id FROM vocab_words WHERE id = ?", (c["item_id"],))["list_id"] for c in cards} == {1, 2}


def test_vocab_tests_target_twenty_questions_even_with_short_list(db, config):
    # A list of only 10 words (>= 6 days total) should still produce 20 questions on review and practice test days
    make_words(db, date(2026, 9, 21), TUE, count=10)
    # Practice test Monday evening before Tuesday test:
    plans = plan_slot("evening", db, config, at(MON, 19), random.Random(1))
    assert len(plans) == 1
    assert len(plans[0].card_ids) == 20
    assert plans[0].max_questions == 25  # room for up to 5 repeats (total 20-25 questions)


def test_short_period_skips_intro_and_increases_questions(db, config):
    # 4 days left (< 5 days): skip intro / easy choices and ask more questions per session
    make_words(db, THU, date(2026, 10, 5), count=12)
    plans = plan_slot("evening", db, config, at(THU, 19), random.Random(1))
    assert len(plans) == 1
    assert plans[0].mode == "typed"
    assert len(plans[0].card_ids) == 30
    assert plans[0].max_questions == 35

    session = build_session(db, plans[0], random.Random(1))
    assert all(item.kind == "typed" for item in session.queue)

    now_plan = plan_vocab_now(db, config, at(THU, 19), random.Random(1))
    assert now_plan is not None and now_plan.mode == "typed"
    assert len(now_plan.card_ids) == 30 and now_plan.max_questions == 35


def test_five_or_more_days_uses_slow_start(db, config):
    # Exactly 5 days left (>= 5 days): use slow start (intro with mc) and normal question count
    make_words(db, WED, date(2026, 10, 5), count=12)
    plans = plan_slot("evening", db, config, at(WED, 19), random.Random(1))
    assert len(plans) == 1
    assert plans[0].mode == "intro"
    assert len(plans[0].card_ids) == 20
    assert plans[0].max_questions == 25


def test_delayed_first_session_with_short_time_left_skips_intro(db, config):
    # List started 8 days before test, but student starts first session only 4 days before test
    make_words(db, MON, date(2026, 10, 6), count=12)
    now_plan = plan_vocab_now(db, config, at(FRI, 19), random.Random(1))
    assert now_plan is not None and now_plan.mode == "typed"
    assert len(now_plan.card_ids) == 30 and now_plan.max_questions == 35


def test_opposite_gender_cards_tested_with_hints(db, config):
    subject = db.ensure_subject("Spanska", "es")
    l1 = db.create_vocab_list(subject["id"], "v40", MON, date(2026, 10, 6))
    db.add_word(l1, "el chico", "pojke", [], [])
    db.add_word(l1, "cansado", "trött", ["cansada"], [])
    db.add_word(l1, "la mesa", "bordet", [], [])
    db.approve_vocab_list(l1)

    plans = plan_slot("evening", db, config, at(MON + timedelta(days=2), 19), random.Random(1))
    assert len(plans) == 1
    session = build_session(db, plans[0], random.Random(1))
    prompts = [item.prompt for item in session.queue]
    # Check that opposite gender and hint parentheticals are used
    assert any("femininum" in p for p in prompts)
    assert any("maskulinum" in p for p in prompts)


def test_failed_cards_retested_in_review(db, config):
    subject = db.ensure_subject("Spanska", "es")
    l1 = db.create_vocab_list(subject["id"], "v40", MON, date(2026, 10, 6))
    w1 = db.add_word(l1, "el perro", "hunden", [], [])
    for i in range(9):
        db.add_word(l1, f"palabra{i}", f"ord{i}", [], [])
    db.approve_vocab_list(l1)

    # Fail the card for w1
    card = db.one("SELECT id FROM cards WHERE item_id = ? AND direction = 'from_sv'", (w1,))
    db.record_attempt(card["id"], "fel", 0, "Fel", at(TUE, 19))

    # Day 2 (Wednesday) evening review plan
    plans = plan_slot("evening", db, config, at(WED, 19), random.Random(1))
    assert len(plans) == 1
    assert len(plans[0].card_ids) == 20
    assert card["id"] in plans[0].card_ids

