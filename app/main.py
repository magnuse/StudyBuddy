"""Starts the bot: database, model router, Telegram handlers and the daily schedule."""

from __future__ import annotations

import logging
import os
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)

from . import handlers_parent as parent
from . import handlers_student as student
from . import updater
from .config import Config, load_config, parse_time, weekday_index
from .context import AppContext
from .db import Database
from .llm import LLMRouter

log = logging.getLogger("studybuddy")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Stockholm"))
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)  # python-telegram-bot: 0 = Sunday … 6 = Saturday

PARENT_COMMANDS = [
    ("status", "Veckans läge per ämne"), ("material", "Material och frågor att godkänna"),
    ("words", "Glosor, ord han missar, ta bort en lista"), ("subjects", "Ämnen och områden"),
    ("schedule", "Tider"), ("holiday", "Lägg in lov"), ("pause", "Pausa frågor i N dagar"),
    ("resume", "Starta frågor igen"), ("invite", "Bjud in: /invite parent eller student"),
    ("family", "Medlemmar"), ("update", "Uppdatera boten nu"), ("version", "Version och senaste ändring"),
    ("test_models", "Jämför AI-modellerna"),
]
STUDENT_COMMANDS = [("quiz", "Öva nu"), ("vocab", "Öva glosor nu"), ("snooze", "Skjut upp 30 min"), ("progress", "Svit och poäng")]


class RoleFilter(filters.MessageFilter):
    """Lets a message through only if the sender is a member with the given role."""

    def __init__(self, db: Database, role: str):
        super().__init__(name=f"RoleFilter({role})")
        self.db, self.role = db, role

    def filter(self, message) -> bool:
        member = self.db.member(message.from_user.id) if message.from_user else None
        return member is not None and member.role == self.role


def ptb_day(name: str) -> int:
    """Converts 'monday' to python-telegram-bot's day number (Sunday = 0)."""
    return (weekday_index(name) + 1) % 7


def at(value: str) -> dtime:
    return parse_time(value).replace(tzinfo=TZ)


def schedule_jobs(application: Application, config: Config) -> None:
    jq = application.job_queue
    s, v = config.schedule, config.vocabulary
    weekdays = (1, 2, 3, 4, 5)
    if s.get("warmup_before_test"):
        jq.run_daily(student.run_slot, at(s["warmup_before_test"]), ALL_DAYS, data={"slot": "warmup"}, name="warmup")
    jq.run_daily(student.run_slot, at(s["afternoon"]), weekdays, data={"slot": "afternoon"}, name="afternoon")
    jq.run_daily(student.run_slot, at(s["evening"]), ALL_DAYS, data={"slot": "evening"}, name="evening")
    jq.run_daily(student.run_slot, at(s["saturday"]), (6,), data={"slot": "saturday"}, name="saturday")
    jq.run_daily(parent.job_sunday_prompt, at(s["sunday_prompt"]), (0,), name="sunday_prompt")
    jq.run_daily(parent.job_vocab_reminder, at(v["reminder"]), (ptb_day(v["list_arrives"]),), name="vocab_reminder")
    jq.run_daily(parent.job_weekly_report, at(s["weekly_report"]), (ptb_day(s["test_day"]),), name="weekly_report")
    check_time = config.raw.get("updates", {}).get("check_time")
    if check_time:
        jq.run_daily(parent.job_update_check, at(check_time), ALL_DAYS, name="update_check")


async def post_init(application: Application) -> None:
    app: AppContext = application.bot_data["app"]
    updater.mark_healthy(app.state_dir)
    for member in app.db.members():
        commands = PARENT_COMMANDS if member.role == "parent" else STUDENT_COMMANDS
        try:
            await application.bot.set_my_commands([BotCommand(c, d) for c, d in commands],
                                                  scope=BotCommandScopeChat(member.telegram_id))
        except Exception as exc:
            log.warning("Could not set commands for %s: %s", member.telegram_id, exc)

    info = updater.current_version(app.config.code_dir)
    rollback = updater.read_rollback_note(app.state_dir)
    message = None
    if rollback:
        message = f"⚠️ Uppdateringen misslyckades, kör version {info.commit} igen. ({rollback})"
    elif app.db.get_setting("announced_commit") not in (None, info.commit):
        message = f"🔄 Uppdaterad till {info.version} ({info.commit}): {info.subject}"
    app.db.set_setting("announced_commit", info.commit)
    if message:
        for member in app.db.members("parent"):
            try:
                await application.bot.send_message(member.telegram_id, message, parse_mode=ParseMode.HTML)
            except Exception as exc:
                log.warning("Could not notify %s: %s", member.telegram_id, exc)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling an update", exc_info=context.error)


def describe_activity(update: Update, db: Database) -> str | None:
    """One log line for a command, button press or upload; None for plain text such as answers."""
    user = update.effective_user
    if user is None:
        return None
    member = db.member(user.id)
    who = f"{member.name} ({member.role})" if member else f"unknown user {user.id}"
    if update.callback_query:
        return f"{who} pressed button {update.callback_query.data}"
    message = update.effective_message
    if message is None:
        return None
    if message.text and message.text.startswith("/"):
        command = message.text.split()[0].split("@")[0]  # arguments left out: /start carries invite codes
        return f"{who} ran {command}"
    if message.document or message.photo:
        return f"{who} sent a {'file' if message.document else 'photo'}"
    return None


async def log_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    line = describe_activity(update, context.application.bot_data["app"].db)
    if line:
        log.info(line)


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Den här boten är privat. Be en förälder om en inbjudningskod.")


def build_application(config: Config) -> Application:
    db = Database(config.db_path)
    db.migrate()
    router = LLMRouter.from_config(config.llm)
    state_dir = Path(os.environ["STATE_DIR"]) if os.environ.get("STATE_DIR") else None
    app = AppContext(config=config, db=db, router=router, state_dir=state_dir)

    application = Application.builder().token(config.telegram_token).post_init(post_init).build()
    application.bot_data["app"] = app
    is_parent, is_student = RoleFilter(db, "parent"), RoleFilter(db, "student")
    new = filters.UpdateType.MESSAGE  # an edited message must not run a command or count as an answer again

    application.add_handler(TypeHandler(Update, log_activity), group=-1)  # runs before the real handlers
    application.add_handler(CommandHandler("start", parent.cmd_start, filters=new))
    parent_commands = {
        "invite": parent.cmd_invite, "family": parent.cmd_family, "subjects": parent.cmd_subjects,
        "material": parent.cmd_material, "words": parent.cmd_words, "schedule": parent.cmd_schedule,
        "holiday": parent.cmd_holiday, "pause": parent.cmd_pause, "resume": parent.cmd_resume,
        "status": parent.cmd_status, "update": parent.cmd_update, "version": parent.cmd_version,
        "test_models": parent.cmd_test_models,
    }
    for name, callback in parent_commands.items():
        application.add_handler(CommandHandler(name, callback, filters=new & is_parent))
    for name, callback in {"quiz": student.cmd_quiz, "vocab": student.cmd_vocab, "snooze": student.cmd_snooze, "progress": student.cmd_progress}.items():
        application.add_handler(CommandHandler(name, callback, filters=new & is_student))

    application.add_handler(CallbackQueryHandler(parent.on_callback, pattern=r"^p:"))
    application.add_handler(CallbackQueryHandler(student.on_callback, pattern=r"^(s|a|f):"))
    application.add_handler(MessageHandler(new & is_parent & (filters.Document.ALL | filters.PHOTO), parent.on_upload))
    application.add_handler(MessageHandler(new & is_parent & filters.TEXT & ~filters.COMMAND, parent.on_parent_text))
    application.add_handler(MessageHandler(new & is_student & filters.TEXT & ~filters.COMMAND, student.on_text))
    application.add_handler(MessageHandler(new & ~is_parent & ~is_student, on_stranger))
    application.add_error_handler(on_error)
    schedule_jobs(application, config)
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()
    if not config.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
