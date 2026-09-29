"""The parents' side: members, uploads, approvals, plans and reports."""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import date, datetime, timedelta

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from . import updater
from .captions import parse_caption, parse_date
from .context import AppContext, PendingUpload, app_of
from .generate import classify, generate_questions
from .grade import grade_free_text
from .handlers_student import notify_parents
from .ingest import UnsupportedFile, extract_text
from .llm import LLMError, LLMRouter
from .scheduler import is_day_off, next_weekday
from .vocab import parse_word_list

log = logging.getLogger(__name__)

LANGUAGE_CODES = {"spanska": "es", "engelska": "en", "tyska": "de", "franska": "fr"}
MAX_LIST_LINES = 40


def _is_parent(app: AppContext, user_id: int) -> bool:
    member = app.db.member(user_id)
    return member is not None and member.role == "parent"


def _name(update: Update) -> str:
    user = update.effective_user
    return user.first_name or user.username or str(user.id)


def _language_for(subject: str) -> str | None:
    return LANGUAGE_CODES.get(subject.strip().lower())


# ---- members ---------------------------------------------------------------------

async def _set_commands(context: ContextTypes.DEFAULT_TYPE, user_id: int, role: str) -> None:
    """Shows the right command menu in Telegram for this member."""
    from .main import PARENT_COMMANDS, STUDENT_COMMANDS

    commands = PARENT_COMMANDS if role == "parent" else STUDENT_COMMANDS
    try:
        await context.bot.set_my_commands([BotCommand(c, d) for c, d in commands], scope=BotCommandScopeChat(user_id))
    except Exception as exc:
        log.warning("Could not set commands for %s: %s", user_id, exc)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    user_id = update.effective_user.id
    member = app.db.member(user_id)
    if member:
        text = ("Hej igen! Skicka material, glosor eller skriv /status." if member.role == "parent"
                else "Hej igen! Skriv /quiz för att öva nu, eller vänta på nästa pass.")
        await update.message.reply_text(text)
        return
    if not app.db.members("parent") and app.config.first_parent_id == user_id:
        app.db.add_member(user_id, _name(update), "parent")
        await _set_commands(context, user_id, "parent")
        await update.message.reply_text(
            "Välkommen! Du är tillagd som förälder.\n"
            "Lägg till fler med /invite parent eller /invite student.\n"
            "Börja med att skicka hans ämnen, till exempel: /subjects add Spanska")
        return
    code = context.args[0] if context.args else ""
    role = app.db.use_invite(code, user_id) if code else None
    if role is None:
        await update.message.reply_text("Den här boten är privat. Be en förälder om en inbjudningskod.")
        return
    app.db.add_member(user_id, _name(update), role)
    await _set_commands(context, user_id, role)
    if role == "student":
        await update.message.reply_text(
            "Hej! Jag är din studybuddy. Efter skolan skickar jag några snabba frågor om det ni läser just nu. "
            "Svara genom att trycka eller skriva. Skriv /quiz om du vill öva direkt, och /progress för din svit och dina poäng.")
    else:
        await update.message.reply_text("Välkommen! Du är tillagd som förälder. Skriv /status för att se läget.")
    await notify_parents(context, f"👋 {html.escape(_name(update))} gick med som {'elev' if role == 'student' else 'förälder'}.",
                         exclude=user_id)


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    role_arg = (context.args[0].lower() if context.args else "")
    role = {"parent": "parent", "student": "student"}.get(role_arg)
    if role is None:
        await update.message.reply_text("Skriv /invite parent eller /invite student.")
        return
    code = app.db.create_invite(role, update.effective_user.id)
    bot = await context.bot.get_me()
    await update.message.reply_text(
        f"Inbjudningskod: <code>{code}</code> (gäller i 24 timmar, en gång).\n"
        f"Be {'eleven' if role == 'student' else 'den andra föräldern'} öppna @{bot.username} i Telegram "
        f"och skicka:\n<code>/start {code}</code>", parse_mode=ParseMode.HTML)


async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    members = app.db.members()
    lines = [f"{'👤' if m.role == 'parent' else '🎒'} {html.escape(m.name)} ({'förälder' if m.role == 'parent' else 'elev'})"
             for m in members]
    buttons = [[InlineKeyboardButton(f"Ta bort {m.name}", callback_data=f"p:rm:{m.id}")]
               for m in members if m.telegram_id != update.effective_user.id]
    await update.message.reply_text("\n".join(lines) or "Inga medlemmar.", parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


# ---- subjects ----------------------------------------------------------------------

async def cmd_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    args = context.args or []
    if len(args) >= 2 and args[0].lower() == "add":
        name = " ".join(args[1:])
        app.db.ensure_subject(name, _language_for(name))
        kind = " (glosläge)" if _language_for(name) else ""
        await update.message.reply_text(f"Ämnet {name} är tillagt{kind}.")
        return
    if len(args) >= 3 and args[0].lower() == "rename":
        old, new = args[1], " ".join(args[2:])
        row = app.db.subject_by_name(old)
        if row is None:
            await update.message.reply_text(f"Hittar inget ämne som heter {old}.")
            return
        app.db.run("UPDATE subjects SET name = ? WHERE id = ?", (new, row["id"]))
        await update.message.reply_text(f"{old} heter nu {new}.")
        return
    if len(args) >= 3 and args[0].lower() == "weight":
        row = app.db.subject_by_name(args[1])
        if row is None:
            await update.message.reply_text(f"Hittar inget ämne som heter {args[1]}.")
            return
        app.db.run("UPDATE subjects SET weight = ? WHERE id = ?", (float(args[2]), row["id"]))
        await update.message.reply_text(f"{row['name']} har nu vikt {args[2]}.")
        return
    subjects = app.db.subjects()
    if not subjects:
        await update.message.reply_text("Inga ämnen än. Lägg till med till exempel /subjects add SO")
        return
    lines, buttons = [], []
    for subject in subjects:
        topics = app.db.q("SELECT title, test_date, status FROM topics WHERE subject_id = ? ORDER BY id DESC LIMIT 3",
                          (subject["id"],))
        topic_text = "; ".join(f"{t['title']}{' (prov ' + t['test_date'] + ')' if t['test_date'] else ''}" for t in topics)
        state = "" if subject["active"] else " (avstängt)"
        lines.append(f"<b>{html.escape(subject['name'])}</b>{state}"
                     f"{' – glosor' if subject['language'] else ''}\n  {html.escape(topic_text) or 'inga områden än'}")
        label = "Stäng av" if subject["active"] else "Slå på"
        buttons.append([InlineKeyboardButton(f"{label} {subject['name']}", callback_data=f"p:subj:{subject['id']}")])
    lines.append("\nÄndra: /subjects add Namn, /subjects rename Gammalt Nytt")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


# ---- uploads -----------------------------------------------------------------------

async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A parent sent a document, photo or plain text that is not a command."""
    app = app_of(context)
    message = update.message
    user_id = update.effective_user.id
    upload = app.uploads.get(user_id)

    # A plain text reply while an upload waits for a new subject name.
    if message.text and upload and upload.subject == "?":
        upload.subject = message.text.strip()
        await _confirm_upload(update, context, upload)
        return

    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    try:
        if message.document:
            file = await message.document.get_file()
            data = bytes(await file.download_as_bytearray())
            text, filename = extract_text(data, message.document.file_name or "fil"), message.document.file_name
        elif message.photo:
            file = await message.photo[-1].get_file()
            data = bytes(await file.download_as_bytearray())
            text, filename = extract_text(data, "bild.jpg"), None
        else:
            text, filename = message.text, None
    except UnsupportedFile as exc:
        await message.reply_text(str(exc))
        return
    except Exception as exc:  # corrupt files, Telegram's 20 MB limit
        log.exception("Upload failed")
        await message.reply_text(f"Kunde inte läsa filen: {exc}")
        return

    caption_text = message.caption if (message.document or message.photo) else None
    if message.text and not caption_text:
        # For pasted text the first line may be the caption, e.g. "Spanska glosor".
        first, _, rest = message.text.partition("\n")
        if rest and len(first) < 60:
            caption_text, text = first, rest
    subjects = [s["name"] for s in app.db.subjects()]
    today = date.today()
    caption = parse_caption(caption_text, subjects, today)
    upload = PendingUpload(text=text, filename=filename, subject=caption.subject, title=caption.title,
                           test_date=caption.test_date, is_word_list=caption.is_word_list)
    if upload.subject and app.db.subject_by_name(upload.subject) and app.db.subject_by_name(upload.subject)["language"]:
        upload.is_word_list = True
    if not upload.is_word_list and len(parse_word_list(text)) >= max(5, len(text.splitlines()) // 2):
        upload.is_word_list = True

    if upload.subject is None or (not upload.is_word_list and upload.title is None):
        await message.reply_text("Läser materialet…")
        try:
            guess = await classify(app.router, text, subjects, today)
            upload.subject = upload.subject or guess.subject
            upload.title = upload.title or guess.topic
            upload.test_date = upload.test_date or guess.test_date
            upload.is_word_list = upload.is_word_list or guess.is_word_list
        except LLMError as exc:
            log.warning("Classify failed: %s", exc)
            upload.subject = upload.subject or "?"
    app.uploads[user_id] = upload
    if upload.subject == "?":
        await _ask_subject(message, app)
        return
    await _confirm_upload(update, context, upload)


async def _ask_subject(message, app: AppContext) -> None:
    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"p:subjpick:{s['id']}")] for s in app.db.subjects(True)]
    await message.reply_text("Vilket ämne gäller det? Välj nedan, eller skriv namnet på ett nytt ämne.",
                             reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


def _default_test_date(app: AppContext, upload: PendingUpload, today: date) -> date:
    if upload.is_word_list:
        # Words arrive on Monday; the test is the list test day at least six days later.
        return next_weekday(today + timedelta(days=5), app.config.vocabulary["test_day"])
    test_day = app.config.schedule["test_day"]
    return today if next_weekday(today - timedelta(days=1), test_day) == today else next_weekday(today, test_day)


async def _confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, upload: PendingUpload) -> None:
    app = app_of(context)
    today = date.today()
    upload.test_date = upload.test_date or _default_test_date(app, upload, today)
    message = update.effective_message
    when = upload.test_date.strftime("%-d/%-m")
    if upload.is_word_list:
        text = f"Glosor i <b>{html.escape(upload.subject)}</b>, glosförhör {when}. Stämmer det?"
    else:
        text = (f"<b>{html.escape(upload.subject)}: {html.escape(upload.title or 'Nytt område')}</b>, prov {when}. "
                "Stämmer det?\nRätta datum genom att svara till exempel <code>prov 9/10</code>.")
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Ja", callback_data="p:up:yes"),
        InlineKeyboardButton("Annat ämne", callback_data="p:up:subject"),
        InlineKeyboardButton("Avbryt", callback_data="p:up:cancel"),
    ]])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=buttons)


async def on_parent_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Short text from a parent: a date correction, a new subject name, or new material."""
    app = app_of(context)
    upload = app.uploads.get(update.effective_user.id)
    text = update.message.text.strip()
    if upload and upload.subject != "?" and text.lower().startswith(("prov", "glosförhör")):
        new_date = parse_date(text, date.today())
        if new_date:
            upload.test_date = new_date
            await _confirm_upload(update, context, upload)
            return
    await on_upload(update, context)


async def _accept_upload(query, context: ContextTypes.DEFAULT_TYPE, upload: PendingUpload) -> None:
    app = app_of(context)
    language = _language_for(upload.subject)
    subject = app.db.ensure_subject(upload.subject, language if upload.is_word_list else None)
    if upload.is_word_list:
        if subject["language"] is None:
            app.db.run("UPDATE subjects SET language = ? WHERE id = ?", (language or "es", subject["id"]))
        pairs = parse_word_list(upload.text)
        if len(pairs) < 2:
            await query.message.reply_text("Jag hittade inga ordpar. Skriv ett par per rad, till exempel: la casa - huset")
            return
        start = date.today()
        title = f"Glosor vecka {start.isocalendar().week}"
        list_id = app.db.create_vocab_list(subject["id"], title, start, upload.test_date)
        for pair in pairs:
            app.db.add_word(list_id, pair.term, pair.translation, pair.term_alts, pair.translation_alts)
        upload.list_id = list_id
        await _show_word_list(query.message, app, list_id)
        return

    topic_id = app.db.create_topic(subject["id"], upload.title or "Nytt område", upload.test_date)
    app.db.add_material(topic_id, upload.filename, upload.text)
    app.uploads.pop(query.from_user.id, None)
    await query.message.reply_text("Skriver frågor… Det kan ta några minuter. Jag hör av mig när de är klara.")
    context.application.create_task(_generate(context, topic_id, subject["name"], upload))


async def _show_word_list(message, app: AppContext, list_id: int) -> None:
    words = app.db.words(list_id)
    lines = [f"{html.escape(w['term'])} – {html.escape(w['translation'])}" for w in words[:MAX_LIST_LINES]]
    more = f"\n… och {len(words) - MAX_LIST_LINES} till" if len(words) > MAX_LIST_LINES else ""
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Godkänn", callback_data=f"p:wl:ok:{list_id}"),
        InlineKeyboardButton("Byt kolumner", callback_data=f"p:wl:swap:{list_id}"),
        InlineKeyboardButton("Avbryt", callback_data=f"p:wl:del:{list_id}"),
    ]])
    await message.reply_text(f"{len(words)} ord (vänster = främmande språk, höger = svenska):\n\n" + "\n".join(lines) + more,
                             parse_mode=ParseMode.HTML, reply_markup=buttons)


async def _generate(context: ContextTypes.DEFAULT_TYPE, topic_id: int, subject: str, upload: PendingUpload) -> None:
    app = app_of(context)
    try:
        summary, questions = await generate_questions(app.router, subject, upload.title or "", upload.text, app.config.student)
    except LLMError as exc:
        log.exception("Question generation failed")
        await notify_parents(context, f"⚠️ Kunde inte skriva frågor för {html.escape(subject)}: {html.escape(str(exc))[:300]}")
        return
    app.db.run("UPDATE topics SET summary = ? WHERE id = ?", (summary, topic_id))
    for q in questions:
        app.db.add_question(topic_id, q.kind, q.prompt, q.answer, q.options, q.rubric, q.source_ref)
    await send_topic_review(context, topic_id)


def _question_lines(app: AppContext, topic_id: int) -> list[str]:
    lines = []
    marked = app.removal_marks.get(topic_id, set())
    for number, q in enumerate(app.db.questions(topic_id, approved=False), 1):
        kind = {"mc": "flerval", "short": "kort svar", "explain": "förklara"}[q["kind"]]
        strike = ("<s>", "</s>") if q["id"] in marked else ("", "")
        lines.append(f"{number}. {strike[0]}{html.escape(q['prompt'])}{strike[1]} <i>({kind}; svar: {html.escape(q['answer'][:60])})</i>")
    return lines


async def send_topic_review(context: ContextTypes.DEFAULT_TYPE, topic_id: int, chat_ids: list[int] | None = None) -> None:
    app = app_of(context)
    topic = app.db.topic(topic_id)
    lines = _question_lines(app, topic_id)
    summary = html.escape((topic["summary"] or "")[:900])
    text = (f"📚 <b>{html.escape(topic['subject'])}: {html.escape(topic['title'])}</b>\n\n{summary}\n\n"
            f"<b>{len(lines)} frågor:</b>\n" + "\n".join(lines))
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Godkänn alla", callback_data=f"p:tq:ok:{topic_id}"),
        InlineKeyboardButton("Ta bort frågor…", callback_data=f"p:tq:rm:{topic_id}"),
    ]])
    for chunk_start in range(0, len(text), 3900):
        chunk = text[chunk_start:chunk_start + 3900]
        last = chunk_start + 3900 >= len(text)
        targets = chat_ids or [m.telegram_id for m in app.db.members("parent")]
        for chat_id in targets:
            await context.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML, reply_markup=buttons if last else None)


def _removal_keyboard(app: AppContext, topic_id: int) -> InlineKeyboardMarkup:
    questions = app.db.questions(topic_id, approved=False)
    marked = app.removal_marks.get(topic_id, set())
    rows, row = [], []
    for number, q in enumerate(questions, 1):
        row.append(InlineKeyboardButton(f"{'✗' if q['id'] in marked else ''}{number}", callback_data=f"p:tq:x:{topic_id}:{q['id']}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Klar: godkänn resten", callback_data=f"p:tq:ok:{topic_id}")])
    return InlineKeyboardMarkup(rows)


# ---- callbacks ---------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    app = app_of(context)
    user_id = query.from_user.id
    if not _is_parent(app, user_id):
        await query.answer()
        return
    parts = query.data.split(":")
    await query.answer()
    action = parts[1]
    who = html.escape(query.from_user.first_name or "En förälder")

    if action == "rm":
        member_id = int(parts[2])
        row = app.db.one("SELECT * FROM members WHERE id = ?", (member_id,))
        if row and row["telegram_id"] != user_id:
            app.db.remove_member(member_id)
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(f"{row['name']} är borttagen.")
    elif action == "subj":
        subject_id = int(parts[2])
        app.db.run("UPDATE subjects SET active = 1 - active WHERE id = ?", (subject_id,))
        row = app.db.one("SELECT * FROM subjects WHERE id = ?", (subject_id,))
        await query.message.reply_text(f"{row['name']} är {'påslaget' if row['active'] else 'avstängt'}.")
    elif action == "subjpick":
        upload = app.uploads.get(user_id)
        row = app.db.one("SELECT * FROM subjects WHERE id = ?", (int(parts[2]),))
        if upload and row:
            upload.subject = row["name"]
            upload.is_word_list = upload.is_word_list or bool(row["language"])
            await query.edit_message_reply_markup(None)
            await _confirm_upload(update, context, upload)
    elif action == "up":
        upload = app.uploads.get(user_id)
        await query.edit_message_reply_markup(None)
        if upload is None:
            return
        if parts[2] == "yes":
            await _accept_upload(query, context, upload)
        elif parts[2] == "subject":
            upload.subject = "?"
            await _ask_subject(query.message, app)
        else:
            app.uploads.pop(user_id, None)
            await query.message.reply_text("Avbrutet.")
    elif action == "wl":
        list_id = int(parts[3])
        if parts[2] == "swap":
            app.db.swap_words(list_id)
            await query.edit_message_reply_markup(None)
            await _show_word_list(query.message, app, list_id)
        elif parts[2] == "ok":
            count = app.db.approve_vocab_list(list_id)
            app.uploads.pop(user_id, None)
            await query.edit_message_reply_markup(None)
            vocab = app.db.vocab_list(list_id)
            await notify_parents(context, f"✅ {who} godkände {count} glosor i {html.escape(vocab['subject'])} "
                                          f"(glosförhör {vocab['test_date']}).")
        else:
            app.db.run("DELETE FROM vocab_words WHERE list_id = ?", (list_id,))
            app.db.run("DELETE FROM vocab_lists WHERE id = ?", (list_id,))
            app.uploads.pop(user_id, None)
            await query.edit_message_reply_markup(None)
            await query.message.reply_text("Glosorna togs bort.")
    elif action == "tq":
        topic_id = int(parts[3])
        if parts[2] == "ok":
            for question_id in app.removal_marks.pop(topic_id, set()):
                app.db.remove_question(question_id)
            count = app.db.approve_topic(topic_id)
            topic = app.db.topic(topic_id)
            await query.edit_message_reply_markup(None)
            if count:
                await notify_parents(context, f"✅ {who} godkände {count} frågor i {html.escape(topic['subject'])}: "
                                              f"{html.escape(topic['title'])}.")
        elif parts[2] == "rm":
            await query.message.reply_text("Tryck på numren du vill ta bort, och sedan Klar.",
                                           reply_markup=_removal_keyboard(app, topic_id))
        elif parts[2] == "x":
            question_id = int(parts[4])
            marks = app.removal_marks.setdefault(topic_id, set())
            marks.symmetric_difference_update({question_id})
            await query.edit_message_reply_markup(_removal_keyboard(app, topic_id))


# ---- plans, pauses and holidays ----------------------------------------------------

async def cmd_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    pending = app.db.pending_topics()
    for topic in pending:
        await send_topic_review(context, topic["id"], [update.effective_chat.id])
    lines = []
    for topic in app.db.topics()[-10:]:
        approved = len(app.db.questions(topic["id"], approved=True))
        lines.append(f"{html.escape(topic['subject'])}: {html.escape(topic['title'])} – {approved} frågor"
                     f"{', prov ' + topic['test_date'] if topic['test_date'] else ''}"
                     f"{' (repetition)' if topic['status'] == 'review' else ''}")
    if not lines and not pending:
        await update.message.reply_text("Inget material än. Skicka en PDF, en bild eller text med ämnet i bildtexten.")
    elif lines:
        await update.message.reply_text("<b>Områden</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    today = date.today().isoformat()
    lists = [v for v in app.db.vocab_lists() if not v["test_date"] or v["test_date"] >= today]
    if not lists:
        await update.message.reply_text("Inga aktuella glosor. Skicka veckans lista, till exempel som foto med bildtexten 'Spanska glosor'.")
        return
    parts = []
    for vocab in lists:
        cards = app.db.list_cards(vocab["id"])
        by_word: dict[int, list] = {}
        for card in cards:
            by_word.setdefault(card["item_id"], []).append(card)
        known = sum(1 for cs in by_word.values() if all(c["box"] >= 2 for c in cs))
        weak = [cs[0] for cs in by_word.values() if any(c["seen"] > c["correct"] for c in cs)]
        weak_text = ", ".join(f"{html.escape(c['term'])}" for c in weak[:15]) or "inga än"
        parts.append(f"<b>{html.escape(vocab['subject'])}: {html.escape(vocab['title'])}</b> "
                     f"(glosförhör {vocab['test_date']})\nKan: {known} av {len(by_word)}\nMissar: {weak_text}")
    await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    s, v = app.config.schedule, app.config.vocabulary
    today = date.today()
    off = is_day_off(today, app.config, app.db)
    await update.message.reply_text(
        f"<b>Tider</b>\nEfter skolan (temavecka): {s['afternoon']} mån–fre\nKväll (glosor): {s['evening']} varje dag\n"
        f"Repetition: lördag {s['saturday']}\nUppvärmning provdagar: {s['warmup_before_test'] or 'av'}\n"
        f"Prov temavecka: {s['test_day']}, glosförhör: {v['test_day']}\n"
        f"Idag: {'inga frågor (' + off + ')' if off else 'som vanligt'}\n\n"
        "Tiderna ändras i config.yaml på servern.", parse_mode=ParseMode.HTML)


async def cmd_holiday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    args = context.args or []
    holidays = json.loads(app.db.get_setting("holidays", "[]"))
    if len(args) >= 2:
        start, end = parse_date(args[0], date.today()), parse_date(args[1], date.today())
        if not start or not end or end < start:
            await update.message.reply_text("Skriv till exempel: /holiday 2026-10-26 2026-10-30 Höstlov")
            return
        name = " ".join(args[2:]) or "Lov"
        holidays.append({"name": name, "from": start.isoformat(), "to": end.isoformat()})
        app.db.set_setting("holidays", json.dumps(holidays, ensure_ascii=False))
        await update.message.reply_text(f"{name}: inga frågor {start} till {end}.")
        return
    listed = [f"{h['name']}: {h['from']} – {h['to']}" for h in holidays + [
        {"name": h.name, "from": h.start.isoformat(), "to": h.end.isoformat()} for h in app.config.holidays]]
    await update.message.reply_text("\n".join(listed) or "Inga lov inlagda.\nLägg till: /holiday 2026-10-26 2026-10-30 Höstlov")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    days = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    until = date.today() + timedelta(days=days - 1)
    app.db.set_setting("paused_until", until.isoformat())
    await notify_parents(context, f"⏸ Frågorna är pausade till och med {until}. /resume startar dem igen.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    app.db.set_setting("paused_until", None)
    await notify_parents(context, "▶️ Frågorna är igång igen.")


# ---- status and reports ------------------------------------------------------------

def weekly_summary(app: AppContext, since: datetime) -> str:
    rows = app.db.q(
        "SELECT a.score, a.flagged, a.answer, a.feedback, c.item_type, "
        "COALESCE(s1.name, s2.name) AS subject, COALESCE(q.prompt, w.term) AS prompt "
        "FROM attempts a JOIN cards c ON c.id = a.card_id "
        "LEFT JOIN questions q ON c.item_type = 'q' AND q.id = c.item_id "
        "LEFT JOIN topics t ON t.id = q.topic_id LEFT JOIN subjects s1 ON s1.id = t.subject_id "
        "LEFT JOIN vocab_words w ON c.item_type = 'w' AND w.id = c.item_id "
        "LEFT JOIN vocab_lists v ON v.id = w.list_id LEFT JOIN subjects s2 ON s2.id = v.subject_id "
        "WHERE a.created_at >= ?", (since.isoformat(timespec="seconds"),))
    if not rows:
        return "Inga svar den här perioden."
    per_subject: dict[str, list[int]] = {}
    for row in rows:
        per_subject.setdefault(row["subject"] or "Övrigt", []).append(row["score"])
    days = len(app.db.q("SELECT DISTINCT substr(created_at, 1, 10) AS d FROM attempts WHERE created_at >= ?",
                        (since.isoformat(timespec="seconds"),)))
    lines = [f"Aktiv {days} dagar, {len(rows)} svar."]
    for subject, scores in sorted(per_subject.items()):
        right = sum(1 for s in scores if s >= 2)
        lines.append(f"• {html.escape(subject)}: {right} av {len(scores)} rätt ({round(100 * right / len(scores))} %)")
    flagged = [r for r in rows if r["flagged"]]
    if flagged:
        lines.append("\n<b>Han tycker att de här rättades fel:</b>")
        lines += [f"• {html.escape((r['prompt'] or '')[:60])} → <i>{html.escape(r['answer'][:60])}</i>" for r in flagged[:10]]
    return "\n".join(lines)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    today = date.today()
    since = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())
    await update.message.reply_text("<b>Den här veckan</b>\n" + weekly_summary(app, since), parse_mode=ParseMode.HTML)


async def job_weekly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    since = datetime.now() - timedelta(days=7)
    await notify_parents(context, "📊 <b>Veckorapport</b>\n" + weekly_summary(app, since))


async def job_sunday_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    if is_day_off(date.today() + timedelta(days=1), app.config, app.db):
        return
    await notify_parents(context, "📅 Ny vecka i morgon. Vilket ämne är det temavecka i? "
                                  "Skicka veckoplaneringen hit, med ämnet i bildtexten (till exempel <i>SO, prov fredag</i>).")


async def job_vocab_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    today = date.today().isoformat()
    if any(v["start_date"] == today for v in app.db.vocab_lists(approved_only=False)):
        return
    if is_day_off(date.today(), app.config, app.db):
        return
    await notify_parents(context, "🇪🇸 Har veckans glosor kommit? Skicka dem hit som foto eller text, "
                                  "med bildtexten <i>Spanska glosor</i>.")


# ---- versions, updates and model tests ---------------------------------------------

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    info = updater.current_version(app.config.code_dir)
    await update.message.reply_text(f"Version {info.version} ({info.commit})\nSenaste ändring: {info.subject or '-'}")


async def check_for_update(context: ContextTypes.DEFAULT_TYPE, forced_by: Update | None = None) -> None:
    app = app_of(context)
    branch = app.config.raw.get("updates", {}).get("branch") or "stable"
    try:
        new_commits = updater.update_available(app.config.code_dir, branch, app.state_dir)
    except Exception as exc:
        log.warning("Update check failed: %s", exc)
        if forced_by:
            await forced_by.message.reply_text(f"Kunde inte söka efter uppdateringar: {exc}")
        return
    if not new_commits:
        if forced_by:
            await forced_by.message.reply_text("Boten är redan uppdaterad.")
        return
    if any(s.started for s in app.sessions.values()):
        if forced_by:
            await forced_by.message.reply_text("Ett pass pågår. Jag uppdaterar när det är klart, eller i natt.")
        return
    if forced_by:
        await forced_by.message.reply_text(f"Uppdaterar ({len(new_commits)} ändringar) och startar om…")
    context.application.create_task(updater.restart_soon())


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_for_update(context, forced_by=update)


async def job_update_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_for_update(context)


SAMPLE_QUESTION = {
    "prompt": "Varför var ångmaskinen viktig under den industriella revolutionen?",
    "answer": "Den gav kraft till fabriker oberoende av vattendrag och drev tåg och båtar, så varor kunde fraktas snabbare.",
    "rubric": "Nämner fabriker som inte behövde ligga vid vatten, och snabbare transporter.",
    "source_ref": "Sida 42",
}
SAMPLE_ANSWERS = ["den gjorde att fabriker inte behövde ligga vid vatten", "för att den var stor"]


async def cmd_test_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = app_of(context)
    await update.message.reply_text("Testar rättning med varje modell. Det kan ta några minuter på processorn…")
    lines = []
    for name, provider in app.router.providers.items():
        if provider.is_cloud and not app.router.allow_cloud:
            lines.append(f"<b>{name}</b>: molnet är avstängt")
            continue
        single = LLMRouter({name: provider}, {"grade": [name]})
        start = time.monotonic()
        try:
            grades = [await grade_free_text(single, SAMPLE_QUESTION, a, app.config.student, "SO") for a in SAMPLE_ANSWERS]
            seconds = (time.monotonic() - start) / len(SAMPLE_ANSWERS)
            lines.append(f"<b>{name}</b> ({html.escape(provider.model)}): {seconds:.0f} s per svar, poäng "
                         f"{grades[0].score} och {grades[1].score} (bör vara 1–2 och 0)\n<i>{grades[0].feedback}</i>")
        except LLMError as exc:
            lines.append(f"<b>{name}</b>: fel – {html.escape(str(exc))[:200]}")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
