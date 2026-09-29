"""Reads the short notes parents write with uploads, like 'SO, prov 10 okt' or 'Spanska glosor'."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
WEEKDAYS_SV = {"måndag": 0, "tisdag": 1, "onsdag": 2, "torsdag": 3, "fredag": 4, "lördag": 5, "söndag": 6}
WORD_LIST_HINTS = ("glosor", "glosa", "ordlista", "vocab", "words")


@dataclass
class Caption:
    subject: str | None = None
    test_date: date | None = None
    is_word_list: bool = False
    title: str | None = None


def parse_date(text: str, today: date) -> date | None:
    text = text.lower()
    match = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text)
    if match:
        return date(int(match[1]), int(match[2]), int(match[3]))
    match = re.search(r"\b(\d{1,2})\s*(?:e\s+)?(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-z]*\b", text)
    if match:
        return _future(int(match[1]), MONTHS[match[2]], today)
    match = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if match:  # Swedish order: day/month
        return _future(int(match[1]), int(match[2]), today)
    for name, index in WEEKDAYS_SV.items():
        if re.search(rf"\b{name}\b", text):
            days = (index - today.weekday()) % 7
            return today + timedelta(days=days)
    return None


def _future(day: int, month: int, today: date) -> date | None:
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate < today - timedelta(days=60):
        candidate = date(today.year + 1, month, day)
    return candidate


def parse_caption(text: str | None, subjects: list[str], today: date) -> Caption:
    caption = Caption()
    if not text:
        return caption
    lowered = text.lower()
    caption.is_word_list = any(hint in lowered for hint in WORD_LIST_HINTS)
    caption.test_date = parse_date(text, today)
    for name in sorted(subjects, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", lowered):
            caption.subject = name
            break
    if caption.subject is None:
        first = re.split(r"[,:\n]", text, maxsplit=1)[0].strip()
        if first and len(first) <= 30 and not any(ch.isdigit() for ch in first):
            skip = set(WORD_LIST_HINTS) | set(WEEKDAYS_SV) | {"prov", "provet", "på", "till", "inför"}
            words = [w for w in first.split() if w.lower() not in skip and w.lower()[:3] not in MONTHS]
            if words:
                caption.subject = " ".join(words)
    rest = re.split(r"[,:\n]", text, maxsplit=1)
    if len(rest) == 2:
        title = re.sub(r"\bprov\b.*", "", rest[1], flags=re.IGNORECASE).strip(" ,.-")
        caption.title = title or None
    return caption
