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


async def test_run_slot_logs_why_nothing_was_sent(db, config, caplog):
    import logging

    context, app = make_context(db, config)
    context.job = SimpleNamespace(data={"slot": "evening"})
    with caplog.at_level(logging.INFO):
        await student.run_slot(context)
    assert "Slot evening: nothing sent" in caplog.text
    assert not context.bot.sent


async def test_parent_removes_a_word_list_from_words(db, config):
    db.add_member(1, "Magnus", "parent")
    db.add_member(100, "Elev", "student")
    subject = db.ensure_subject("Spanska", "es")
    keep = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 30), date(2026, 10, 6))
    db.add_word(keep, "el coche", "bilen", [], [])
    db.approve_vocab_list(keep)
    broken = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 30), date(2026, 10, 6))
    db.add_word(broken, "la casa", "huset", [], [])
    db.approve_vocab_list(broken)
    card = db.list_cards(broken)[0]
    db.record_attempt(card["id"], "huset", 2, None)
    context, app = make_context(db, config)

    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    async def edit_markup(markup):
        pass

    async def answer():
        pass

    from app import handlers_parent as parent

    message = SimpleNamespace(reply_text=reply_text)
    await parent.cmd_words(SimpleNamespace(message=message, effective_message=message), context)
    remove_buttons = [row[0].callback_data for row in replies[-1][1].inline_keyboard]
    assert remove_buttons == [f"p:wd:ask:{keep}", f"p:wd:ask:{broken}"]

    user = SimpleNamespace(id=1, first_name="Magnus")
    for data in (f"p:wd:ask:{broken}", f"p:wd:yes:{broken}"):
        query = SimpleNamespace(data=data, from_user=user, message=message, answer=answer,
                                edit_message_reply_markup=edit_markup)
        await parent.on_callback(SimpleNamespace(callback_query=query), context)
    assert "Ta bort" in replies[-1][0]
    assert [v["id"] for v in db.vocab_lists()] == [keep]
    assert not db.list_cards(broken) and len(db.list_cards(keep)) == 2
    assert not db.q("SELECT * FROM attempts")
    assert any("tog bort" in t for _, t, _ in context.bot.sent)


async def test_student_starts_a_vocab_round_himself(db, config):
    db.add_member(100, "Elev", "student")
    subject = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 30), date(2026, 10, 6))
    for i in range(12):
        db.add_word(list_id, f"palabra{i}", f"ord{i}", [], [])
    db.approve_vocab_list(list_id)
    context, app = make_context(db, config)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(text)

    update = SimpleNamespace(effective_chat=SimpleNamespace(id=100), message=SimpleNamespace(reply_text=reply_text), effective_message=SimpleNamespace(reply_text=reply_text))
    await student.cmd_vocab(update, context)
    session = app.sessions[100]
    assert session.started and session.plan.mode == "intro" and len(session.queue) + 1 == 12
    assert "12 ord" in replies[-1] and session.current.kind == "mc"

    await student.cmd_vocab(update, context)  # a round is already running
    assert "pågående" in replies[-1]

    app.sessions.pop(100)
    card = db.list_cards(list_id)[0]
    db.record_attempt(card["id"], "fel", 0, None)
    await student.cmd_vocab(update, context)
    session = app.sessions[100]
    assert session.plan.mode == "typed" and session.plan.card_ids[0] == card["id"]
    assert len(session.plan.card_ids) == 2 * config.vocabulary["words_per_round"]


async def test_vocab_without_lists_says_so(db, config):
    db.add_member(100, "Elev", "student")
    context, app = make_context(db, config)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(text)

    update = SimpleNamespace(effective_chat=SimpleNamespace(id=100), message=SimpleNamespace(reply_text=reply_text), effective_message=SimpleNamespace(reply_text=reply_text))
    await student.cmd_vocab(update, context)
    assert replies == ["Det finns inga glosor att öva på just nu."]


def test_activity_log_names_the_user_and_command(db):
    from app.main import describe_activity

    db.add_member(100, "Elev", "student")

    def update(user_id, text=None, data=None, document=None):
        message = SimpleNamespace(text=text, document=document, photo=None)
        query = SimpleNamespace(data=data) if data else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=query, effective_message=message)

    assert describe_activity(update(100, "/vocab"), db) == "Elev (student) ran /vocab"
    assert describe_activity(update(7, "/start ABCD1234"), db) == "unknown user 7 ran /start"
    assert describe_activity(update(100, data="s:go"), db) == "Elev (student) pressed button s:go"
    assert describe_activity(update(100, "la casa"), db) is None  # answers stay out of the log


def test_edited_messages_do_not_run_commands_or_answers(tmp_path):
    from telegram import Update, User

    from app.config import load_config
    from app.main import build_application

    config = load_config(tmp_path / "none.yaml", env={"TELEGRAM_BOT_TOKEN": "123:ABC", "DATA_DIR": str(tmp_path)})
    application = build_application(config)
    application.bot_data["app"].db.add_member(1, "Magnus", "parent")
    with application.bot._unfrozen():  # command matching needs the bot's username, normally fetched at startup
        application.bot._bot_user = User(123, "StudyBuddy", True, username="studybuddy_bot")

    def make(kind, text):
        message = {"message_id": 5, "date": 0, "chat": {"id": 1, "type": "private"},
                   "from": {"id": 1, "is_bot": False, "first_name": "Magnus"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return Update.de_json({"update_id": 1, kind: message}, application.bot)

    def matches(update):
        return [h for h in application.handlers[0] if h.check_update(update)]

    assert matches(make("message", "/update"))
    assert not matches(make("edited_message", "/update"))
    assert not matches(make("edited_message", "hej"))


async def test_words_shows_a_list_waiting_for_approval(db, config):
    from app import handlers_parent as parent

    db.add_member(1, "Magnus", "parent")
    subject = db.ensure_subject("Spanska", "es")
    pending = db.create_vocab_list(subject["id"], "v41", date(2026, 9, 30), date(2099, 10, 13))
    db.add_word(pending, "el perro", "hunden", [], [])
    context, app = make_context(db, config)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    async def noop(*args):
        pass

    message = SimpleNamespace(reply_text=reply_text)
    await parent.cmd_words(SimpleNamespace(message=message, effective_message=message), context)
    text, markup = replies[-1]
    assert "väntar på godkännande" in text
    approve = markup.inline_keyboard[0][0]
    assert approve.callback_data == f"p:wl:ok:{pending}"

    query = SimpleNamespace(data=approve.callback_data, from_user=SimpleNamespace(id=1, first_name="Magnus"),
                            message=message, answer=noop, edit_message_reply_markup=noop)
    await parent.on_callback(SimpleNamespace(callback_query=query), context)
    assert [v["id"] for v in db.vocab_lists()] == [pending]


async def test_polling_network_errors_are_one_warning_line(caplog):
    import logging

    from telegram.error import BadRequest, NetworkError

    from app.main import on_error

    with caplog.at_level(logging.INFO):
        await on_error(None, SimpleNamespace(error=NetworkError("Bad Gateway")))
    assert [r.levelname for r in caplog.records] == ["WARNING"] and "Bad Gateway" in caplog.text
    assert caplog.records[0].exc_info is None

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await on_error(SimpleNamespace(), SimpleNamespace(error=BadRequest("broken")))
    assert [r.levelname for r in caplog.records] == ["ERROR"]


async def test_parents_get_a_report_and_answers_are_logged(db, config, caplog):
    import logging

    db.add_member(1, "Magnus", "parent")
    db.add_member(100, "Elev", "student")
    subject = db.ensure_subject("Spanska", "es")
    list_id = db.create_vocab_list(subject["id"], "v40", date(2026, 9, 28), date(2026, 10, 6))
    db.add_word(list_id, "la canción", "sången", [], [])
    db.add_word(list_id, "el perro", "hunden", [], [])
    db.approve_vocab_list(list_id)
    context, app = make_context(db, config)

    plans = plan_slot("evening", db, config, datetime(2026, 9, 30, 19, 0), random.Random(1))  # day 1: typed
    await student.offer_plans(context, 100, plans)
    session = app.sessions[100]
    session.started = True
    await student._ask(context, 100, session)
    answers = {"sången": "la canción", "la canción": "sången", "hunden": "el gato", "el perro": "katten"}
    with caplog.at_level(logging.INFO):
        while 100 in app.sessions and app.sessions[100].current:
            word = session.current.prompt.split("<b>")[1].split("</b>")[0]
            await student.handle_answer(context, 100, answers[word])  # missed words come back, wrong again
    assert "Answer from Elev: Översätt till svenska: el perro | answered 'katten' | expected 'hunden' | wrong" in caplog.text
    report = [t for chat, t, _ in context.bot.sent if chat == 1]
    assert len(report) == 1 and report[0].startswith("📚 Elev är klar med")
    assert "2 av 4 rätt" in report[0] and "svarade <i>katten</i>" in report[0]
