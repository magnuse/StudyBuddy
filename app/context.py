"""Shared state for the Telegram handlers."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from telegram.ext import ContextTypes

from .config import Config
from .db import Database
from .llm import LLMRouter
from .session import Session


@dataclass
class PendingUpload:
    text: str
    filename: str | None
    subject: str | None = None
    title: str | None = None
    test_date: object = None
    is_word_list: bool = False
    list_id: int | None = None


@dataclass
class AppContext:
    config: Config
    db: Database
    router: LLMRouter
    state_dir: Path | None = None
    sessions: dict[int, Session] = field(default_factory=dict)  # student telegram id -> session
    waiting: dict[int, list] = field(default_factory=dict)  # student telegram id -> plans not yet started
    uploads: dict[int, PendingUpload] = field(default_factory=dict)  # parent telegram id -> upload
    removal_marks: dict[int, set[int]] = field(default_factory=dict)  # topic id -> question ids marked
    rng: random.Random = field(default_factory=random.Random)


def app_of(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    return context.application.bot_data["app"]
