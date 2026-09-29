"""Runs the student's quiz flow end to end against a fake Telegram bot."""

import asyncio
import random
from datetime import date, datetime
from types import SimpleNamespace

from app import handlers_student as student
from app.context import AppContext
from app.llm import LLMProvider, LLMRouter
from app.scheduler import plan_slot


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))

    async def send_chat_action(self, chat_id, action):
        pass


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, **kwargs):
        self.jobs.append((callback, when, kwargs))


class FakeGrader(LLMProvider):
    async def complete_json(self, system, prompt, schema, max_tokens=4000):
        if "accepted" in schema["required"]:
            return {"accepted": "carro" in prompt.split("The student wrote:")[1], "feedback": "Synonym!"}
        return {"score": 2, "feedback": "Bra förklarat!", "missing": ""}


def make_context(db, config):
    router = LLMRouter({"fake": FakeGrader("fake", "m")}, {"grade": ["fake"], "vocab_synonym": ["fake"]})
    app = AppContext(config=config, db=db, router=router, rng=random.Random(3))
    application = SimpleNamespace(bot_data={"app": app}, create_task=asyncio.ensure_future)
    return SimpleNamespace(application=application, bot=FakeBot(), job_queue=FakeJobQueue()), app


async def test_topic_batch_from_offer_to_summary(db, config):
    db.add_member(100, "Elev", "student")
    subject = db.ensure_subject("SO")
    topic = db.create_topic(subject["id"], "Industrialismen", date(2026, 10, 2))
    db.add_question(topic, "mc", "Var började den?", "Storbritannien", ["Storbritannien", "Frankrike", "Tyskland"])
    db.add_question(topic, "explain", "Varför var ångmaskinen viktig?", "Fabriker och transporter")
    db.approve_topic(topic)
    context, app = make_context(db, config)

    plans = plan_slot("afternoon", db, config, datetime(2026, 9, 28, 16, 30), random.Random(1))
    await student.offer_plans(context, 100, plans)
    assert "2 snabba frågor" in context.bot.sent[-1][1]
    assert context.job_queue.jobs  # reminder scheduled

    session = app.sessions[100]
    session.started = True
    await student._ask(context, 100, session)
    for _ in range(2):
        item = session.current
        answer = "Storbritannien" if item.kind == "mc" else "fabriker behövde inte ligga vid vatten"
        await student.handle_answer(context, 100, answer)
    await asyncio.sleep(0.05)
    texts = [t for _, t, _ in context.bot.sent]
    assert any("Bra förklarat" in t for t in texts)
    assert any(t.startswith("Klart!") and "2 av 2" in t for t in texts)
    assert 100 not in app.sessions
    assert len(db.q("SELECT * FROM attempts")) == 2


async def test_vocab_typed_answers_with_accent_and_synonym(db, config):
    db.add_member(100, "Elev", "student")
    subject = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 28), date(2026, 10, 6))
    db.add_word(list_id, "la canción", "sången", [], [])
    db.add_word(list_id, "el coche", "bilen", [], [])
    db.approve_vocab_list(list_id)
    context, app = make_context(db, config)

    plans = plan_slot("evening", db, config, datetime(2026, 9, 30, 19, 0), random.Random(1))  # day 1: typed
    await student.offer_plans(context, 100, plans)
    session = app.sessions[100]
    session.started = True
    await student._ask(context, 100, session)
    answers = {"sången": "la cancion", "bilen": "el carro", "la canción": "sangen", "el coche": "bilen"}
    for _ in range(4):
        word = session.current.prompt.split("<b>")[1].split("</b>")[0]
        await student.handle_answer(context, 100, answers[word])
    texts = [t for _, t, _ in context.bot.sent]
    assert any("Tänk på accenten" in t for t in texts)
    assert any("Synonym!" in t for t in texts)
    assert any(t.startswith("Klart!") for t in texts)


def test_application_builds_with_all_handlers_and_jobs(tmp_path):
    from app.config import load_config
    from app.main import build_application

    config = load_config(tmp_path / "none.yaml", env={"TELEGRAM_BOT_TOKEN": "123:ABC", "DATA_DIR": str(tmp_path)})
    application = build_application(config)
    names = {job.name for job in application.job_queue.jobs()}
    assert {"afternoon", "evening", "warmup", "saturday", "sunday_prompt", "vocab_reminder", "weekly_report",
            "update_check"} <= names
    assert sum(len(h) for h in application.handlers.values()) >= 20
