"""The student's side: receiving batches, answering, snoozing and progress."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from .context import AppContext, app_of
from .grade import Grade, check_synonym, grade_free_text, grade_multiple_choice
from .llm import LLMError
from .scheduler import Plan, plan_saturday, plan_slot
from .session import QuizItem, Result, Session, build_session
from .vocab import Verdict, check_answer

log = logging.getLogger(__name__)

SNOOZE_MINUTES = 30
REASON_TEXT = {
    "accent": "Tänk på accenten: <b>{expected}</b>",
    "article": "Tänk på artikeln: <b>{expected}</b>",
    "typo": "Nästan rätt stavat: <b>{expected}</b>",
}


def students(app: AppContext) -> list[int]:
    return [m.telegram_id for m in app.db.members("student")]


def parents(app: AppContext) -> list[int]:
    return [m.telegram_id for m in app.db.members("parent")]


async def notify_parents(context: ContextTypes.DEFAULT_TYPE, text: str, exclude: int | None = None, **kwargs) -> None:
    for chat_id in parents(app_of(context)):
        if chat_id != exclude:
            try:
                await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **kwargs)
            except Exception as exc:  # a blocked bot must not stop the others
                log.warning("Could not message parent %s: %s", chat_id, exc)


# ---- offering a batch -----------------------------------------------------------

def _intro_text(plan: Plan, count: int) -> str:
    minutes = max(2, round(count * 0.75))
    if plan.kind == "vocab" and plan.mode == "test":
        return f"📝 <b>{html.escape(plan.title)}</b>\n{count} ord, ca {minutes} min."
    if plan.kind == "vocab":
        rounds = f" i omgångar om {plan.round_size}" if plan.round_size and count > plan.round_size else ""
        return f"🇪🇸 <b>{html.escape(plan.title)}</b>\n{count} ord{rounds}, ca {minutes} min."
    if plan.kind == "topic_test":
        return f"📝 <b>Övningsprov: {html.escape(plan.title)}</b>\n{count} frågor om hela veckan, ca {minutes} min."
    return f"<b>{html.escape(plan.title)}</b>\n{count} snabba frågor, ca {minutes} min."


async def offer_plans(context: ContextTypes.DEFAULT_TYPE, chat_id: int, plans: list[Plan]) -> None:
    app = app_of(context)
    if chat_id in app.sessions and app.sessions[chat_id].started:
        return  # never interrupt a batch in progress
    queue = app.waiting.setdefault(chat_id, [])
    queue.extend(plans)
    await _offer_next(context, chat_id)


async def _offer_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    app = app_of(context)
    queue = app.waiting.get(chat_id) or []
    while queue:
        plan = queue.pop(0)
        session = build_session(app.db, plan, app.rng)
        if not session.queue:
            continue
        app.sessions[chat_id] = session
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Kör!", callback_data="s:go"),
            InlineKeyboardButton(f"Skjut upp {SNOOZE_MINUTES} min", callback_data="s:snooze"),
        ]])
        await context.bot.send_message(chat_id, _intro_text(plan, len(session.queue)),
                                       parse_mode=ParseMode.HTML, reply_markup=buttons)
        _schedule_reminder(context, chat_id, session)
        return


def _schedule_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session) -> None:
    minutes = app_of(context).config.schedule["reminder_after_minutes"]
    if context.job_queue and minutes:
        context.job_queue.run_once(_remind, timedelta(minutes=minutes), chat_id=chat_id,
                                   data={"session": id(session), "step": 1}, name=f"remind-{chat_id}")


async def _remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    chat_id = context.job.chat_id
    session = app.sessions.get(chat_id)
    if session is None or id(session) != context.job.data["session"] or session.started:
        return
    if context.job.data["step"] == 1:
        await context.bot.send_message(chat_id, "Påminnelse: några snabba frågor väntar på dig 🙂",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kör!", callback_data="s:go")]]))
        minutes = app.config.schedule["reminder_after_minutes"]
        context.job_queue.run_once(_remind, timedelta(minutes=minutes), chat_id=chat_id,
                                   data={"session": id(session), "step": 2}, name=f"remind-{chat_id}")
    else:
        await notify_parents(context, f"ℹ️ Dagens pass (<i>{html.escape(session.plan.title)}</i>) är inte påbörjat än.")


async def run_slot(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback for a scheduled time slot."""
    app = app_of(context)
    slot = context.job.data["slot"]
    plans = plan_slot(slot, app.db, app.config, datetime.now(), app.rng)
    if not plans:
        return
    for chat_id in students(app):
        await offer_plans(context, chat_id, plans)


# ---- asking and answering ----------------------------------------------------------

async def _ask(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session) -> None:
    if session.round_finished():
        session.answered_in_round = 0
        right, total = session.score_summary()
        await context.bot.send_message(
            chat_id, f"Runda klar! {right} av {total} rätt hittills. {len(session.queue)} ord kvar.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Fortsätt", callback_data="s:go"),
                                                InlineKeyboardButton("Pausa", callback_data="s:pause")]]))
        session.current = None
        return
    item = session.next_item()
    if item is None:
        await _finish(context, chat_id, session)
        return
    done = len({r.item.card_id for r in session.results})
    header = f"{done + 1}/{session.total}. "
    markup = None
    if item.kind == "mc":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(opt, callback_data=f"a:{index}")]
                                       for index, opt in enumerate(item.options)])
    await context.bot.send_message(chat_id, header + item.prompt,
                                   parse_mode=ParseMode.HTML, reply_markup=markup)


async def _finish(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session) -> None:
    app = app_of(context)
    for _ in range(120):  # wait for background grading, at most about two minutes
        if session.pending_grades == 0:
            break
        await asyncio.sleep(1)
    right, total = session.score_summary()
    points = sum(r.score or 0 for r in session.results)
    text = f"Klart! ✅ {right} av {total} rätt på första försöket. +{points} poäng."
    if total and right == total:
        text += " Perfekt! 🎉"
    await context.bot.send_message(chat_id, text)
    app.sessions.pop(chat_id, None)
    if app.waiting.get(chat_id):
        await _offer_next(context, chat_id)


def _feedback_markup(attempt_id: int | None) -> InlineKeyboardMarkup | None:
    if attempt_id is None:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("Jag tycker jag hade rätt", callback_data=f"f:{attempt_id}")]])


async def _grade_typed(app: AppContext, item: QuizItem, answer: str, prompt_word: str) -> Grade:
    result = check_answer(answer, item.expected, item.language, app.config.vocabulary["strict_accents"])
    if result.verdict == Verdict.CORRECT:
        return Grade(2, "Rätt!")
    if result.verdict == Verdict.ALMOST:
        return Grade(2, "Nästan! " + REASON_TEXT[result.reason].format(expected=html.escape(result.expected)))
    try:
        return await check_synonym(app.router, prompt_word, item.expected[0], answer, item.language or "sv")
    except LLMError as exc:
        log.warning("Synonym check failed: %s", exc)
        return Grade(0, f"Inte riktigt. Rätt svar: <b>{html.escape(item.expected[0])}</b>")


async def handle_answer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, answer: str) -> None:
    app = app_of(context)
    session = app.sessions.get(chat_id)
    if session is None or not session.started or session.current is None:
        await context.bot.send_message(chat_id, "Inga frågor just nu. Skriv /quiz om du vill köra ett pass.")
        return
    item = session.current
    session.current = None
    session.answered_in_round += 1
    result = Result(item, answer)
    session.results.append(result)

    if item.kind == "mc":
        grade = grade_multiple_choice(answer, item.expected[0])
        await _record_and_reply(context, chat_id, session, result, grade, show_flag=False)
    elif item.kind == "typed":
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        prompt_word = html.unescape(item.prompt.split("<b>")[-1].replace("</b>", ""))
        grade = await _grade_typed(app, item, answer, prompt_word)
        await _record_and_reply(context, chat_id, session, result, grade, show_flag=grade.score < 2)
    else:
        # Free text is graded in the background so the student can go on to the next question.
        session.pending_grades += 1
        context.application.create_task(_grade_in_background(context, chat_id, session, result))
        if session.queue:
            await context.bot.send_message(chat_id, "Tack! Rättar ditt svar medan du tar nästa fråga.")
    await _ask(context, chat_id, session)


async def _grade_in_background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session, result: Result) -> None:
    app = app_of(context)
    try:
        grade = await grade_free_text(app.router, result.item.question, result.answer, app.config.student, result.item.subject)
    except LLMError as exc:
        log.warning("Grading failed: %s", exc)
        grade = Grade(1, f"Jag kunde inte rätta just nu. Jämför själv med: {html.escape(result.item.expected[0])}")
    try:
        await _record_and_reply(context, chat_id, session, result, grade, show_flag=grade.score < 2, quote=True)
    finally:
        session.pending_grades -= 1


async def _record_and_reply(context, chat_id: int, session: Session, result: Result, grade: Grade,
                            show_flag: bool, quote: bool = False) -> None:
    app = app_of(context)
    result.score, result.feedback = grade.score, grade.feedback
    result.attempt_id = app.db.record_attempt(result.item.card_id, result.answer, grade.score, grade.feedback)
    if grade.score < 2:
        session.requeue_missed(result.item)
    prefix = ""
    if quote:
        prefix = f"<i>{result.item.plain_prompt[:80]}</i>\n"
    icon = "✅" if grade.score >= 2 else ("🟡" if grade.score == 1 else "❌")
    await context.bot.send_message(chat_id, f"{prefix}{icon} {grade.feedback}", parse_mode=ParseMode.HTML,
                                   reply_markup=_feedback_markup(result.attempt_id) if show_flag else None)


# ---- Telegram entry points ---------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    app = app_of(context)
    chat_id = query.message.chat_id
    member = app.db.member(query.from_user.id)
    if member is None or member.role != "student":
        await query.answer()
        return
    data = query.data
    session = app.sessions.get(chat_id)
    await query.answer()
    if data == "s:go":
        if session is None:
            await query.edit_message_reply_markup(None)
            return
        session.started = True
        await query.edit_message_reply_markup(None)
        if session.current is None:
            await _ask(context, chat_id, session)
    elif data == "s:snooze":
        await query.edit_message_reply_markup(None)
        await snooze(context, chat_id)
    elif data == "s:pause":
        await query.edit_message_reply_markup(None)
        if session:
            session.started = False
        await query.message.reply_text("Pausat. Skriv /quiz när du vill fortsätta.")
    elif data.startswith("a:") and session and session.current and session.current.kind == "mc":
        option = session.current.options[int(data[2:])]
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id, f"➜ {option}")
        await handle_answer(context, chat_id, option)
    elif data.startswith("f:"):
        app.db.flag_attempt(int(data[2:]))
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Noterat! En förälder tittar på det.")


async def snooze(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    app = app_of(context)
    session = app.sessions.pop(chat_id, None)
    if session is None:
        await context.bot.send_message(chat_id, "Det finns inget pass att skjuta upp.")
        return
    session.started = False
    session.current = None

    async def resume(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        app.waiting.setdefault(chat_id, []).insert(0, session.plan)
        await _offer_next(ctx, chat_id)

    context.job_queue.run_once(resume, timedelta(minutes=SNOOZE_MINUTES), chat_id=chat_id)
    await context.bot.send_message(chat_id, f"Okej, jag hör av mig om {SNOOZE_MINUTES} minuter.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_answer(context, update.effective_chat.id, update.message.text.strip())


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    chat_id = update.effective_chat.id
    session = app.sessions.get(chat_id)
    if session is not None:
        session.started = True
        if session.current is None:
            await _ask(context, chat_id, session)
        return
    now = datetime.now()
    plans = []
    for slot in ("afternoon", "evening"):
        plans += plan_slot(slot, app.db, app.config, now, app.rng)
    if not plans:
        plans = [p for p in plan_saturday(app.db, app.config, now, app.rng) if p.card_ids]
    if not plans:
        await update.message.reply_text("Inget att öva på just nu. Bra jobbat! 🎉")
        return
    await offer_plans(context, chat_id, plans)


async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await snooze(context, update.effective_chat.id)


def streak_days(app: AppContext, today: date) -> int:
    rows = app.db.q("SELECT DISTINCT substr(created_at, 1, 10) AS day FROM attempts ORDER BY day DESC LIMIT 60")
    days = {date.fromisoformat(r["day"]) for r in rows}
    streak, day = 0, today
    if day not in days:
        day -= timedelta(days=1)
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    today = date.today()
    week_start = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())
    attempts = app.db.attempts_since(week_start)
    points = sum(a["score"] for a in attempts)
    learned = app.db.one("SELECT COUNT(DISTINCT item_id) AS n FROM cards WHERE item_type = 'w' AND box >= 2")["n"]
    await update.message.reply_text(
        f"🔥 Svit: {streak_days(app, today)} dagar i rad\n"
        f"⭐ Poäng den här veckan: {points}\n"
        f"📚 Glosor du kan: {learned}"
    )
