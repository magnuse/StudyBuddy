"""The parents' read-only web page."""

import base64
import urllib.error
import urllib.request
from datetime import date

import pytest

from app.db import Database
from app.web import render_answers, render_topics, render_words, start_web


@pytest.fixture
def filled(tmp_path):
    db = Database(tmp_path / "studybuddy.db")
    db.migrate()
    spanish = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(spanish["id"], "Glosor vecka 41", date(2026, 10, 6), date(2026, 10, 13))
    db.add_word(list_id, "el perro", "hunden", [], ["vovven"])
    db.approve_vocab_list(list_id)
    so = db.ensure_subject("NO")
    topic = db.create_topic(so["id"], "Fotosyntes", date(2026, 10, 16))
    db.add_question(topic, "explain", "Vad behöver växten för <fotosyntes>?", "Ljus, vatten och koldioxid")
    db.approve_topic(topic)
    word_card = db.list_cards(list_id, "to_sv")[0]
    db.record_attempt(word_card["id"], "katten <script>", 0, "Inte riktigt. Rätt svar: <b>hunden</b>")
    return db, tmp_path / "studybuddy.db"


def test_pages_show_lists_questions_and_answers_escaped(filled):
    db, _ = filled
    words = render_words(db.conn)
    assert "el perro" in words and "hunden" in words and "även: vovven" in words and "0 av 1" in words
    topics = render_topics(db.conn)
    assert "Fotosyntes" in topics and "&lt;fotosyntes&gt;" in topics and "Ljus, vatten och koldioxid" in topics
    answers = render_answers(db.conn)
    assert "katten &lt;script&gt;" in answers and "<script>" not in answers  # the student's text is escaped
    assert "→ svenska" in answers and "❌ fel" in answers


def test_server_serves_pages_read_only_and_asks_for_the_password(filled):
    db, path = filled
    server = start_web(path, 0)
    assert server is None  # port 0 turns the page off
    server = start_web(path, _free_port(), password="hemligt")
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url + "/words")
        assert denied.value.code == 401
        request = urllib.request.Request(url + "/answers", headers={
            "Authorization": "Basic " + base64.b64encode(b"magnus:hemligt").decode()})
        with urllib.request.urlopen(request) as response:
            assert response.status == 200 and "katten" in response.read().decode()
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(urllib.request.Request(url + "/nope", headers=request.headers))
        assert missing.value.code == 404
    finally:
        server.shutdown()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
