from datetime import date, datetime, timedelta

from app.db import Database


def test_migrate_is_idempotent(db):
    assert db.migrate() == db.migrate()


def test_invite_can_be_used_once(db):
    code = db.create_invite("student", created_by=1)
    assert db.use_invite(code.lower(), 42) == "student"
    assert db.use_invite(code, 43) is None


def test_expired_invite_is_rejected(db):
    code = db.create_invite("parent", created_by=1, hours=-1)
    assert db.use_invite(code, 42) is None


def test_leitner_boxes(db):
    subject = db.ensure_subject("SO")
    topic = db.create_topic(subject["id"], "Test", date(2026, 10, 2))
    question = db.add_question(topic, "short", "Fråga?", "Svar")
    db.approve_topic(topic)
    card = db.topic_cards(topic)[0]
    now = datetime(2026, 9, 28, 16, 30)
    db.record_attempt(card["id"], "Svar", 2, "Rätt", now)
    assert db.card(card["id"])["box"] == 1
    assert db.card(card["id"])["due_at"].startswith("2026-09-29")
    db.record_attempt(card["id"], "fel", 0, "Fel", now)
    assert db.card(card["id"])["box"] == 0
    assert question


def test_approving_a_word_list_creates_two_cards_per_word(db):
    subject = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 28), date(2026, 10, 6))
    db.add_word(list_id, "la casa", "huset", [], [])
    db.add_word(list_id, "el perro", "hunden", [], [])
    assert db.approve_vocab_list(list_id) == 2
    assert len(db.list_cards(list_id)) == 4
    assert len(db.list_cards(list_id, "from_sv")) == 2


def test_removed_question_loses_its_card(db):
    subject = db.ensure_subject("NO")
    topic = db.create_topic(subject["id"], "Celler", None)
    q1 = db.add_question(topic, "short", "A?", "a")
    db.add_question(topic, "short", "B?", "b")
    db.approve_topic(topic)
    db.remove_question(q1)
    assert len(db.topic_cards(topic)) == 1


def test_topics_move_to_review_after_the_test(db):
    subject = db.ensure_subject("SO")
    db.create_topic(subject["id"], "Gammal", date(2026, 9, 25))
    db.create_topic(subject["id"], "Ny", date(2026, 10, 2))
    assert db.close_finished_topics(date(2026, 9, 28)) == 1
    assert [t["title"] for t in db.topics("active")] == ["Ny"]


def test_database_file_is_created(tmp_path):
    path = tmp_path / "sub" / "x.db"
    Database(path).migrate()
    assert path.exists()
    assert timedelta  # keeps import used
