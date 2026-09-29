"""Loads config.yaml and environment variables into one Config object."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "schedule": {
        "afternoon": "16:30",  # topic-week slot
        "evening": "19:00",  # language (vocabulary) slot
        "saturday": "11:00",  # review slot
        "questions_per_batch": 4,
        "reminder_after_minutes": 45,
        "test_day": "friday",  # topic-week test day
        "warmup_before_test": "07:15",  # null disables
        "sunday_prompt": "18:00",
        "weekly_report": "15:00",  # sent on test_day
    },
    "holidays": [],  # [{name, from, to}]
    "student": {"age": 14, "grade": 8, "lenient_spelling": True},
    "vocabulary": {
        "languages": ["es"],
        "list_arrives": "monday",
        "test_day": "tuesday",
        "reminder": "15:00",
        "words_per_round": 10,
        "strict_accents": False,
    },
    "llm": {
        "providers": {
            "writer": {"type": "ollama", "base_url": "http://ollama:11434", "model": "gemma3:12b"},
            "grader": {"type": "ollama", "base_url": "http://ollama:11434", "model": "gemma3:4b"},
            "cloud": {"type": "anthropic", "model": "claude-opus-5"},
        },
        "tasks": {
            "generate_questions": ["writer"],
            "classify": ["writer"],
            "grade": ["cloud", "grader"],
            "vocab_synonym": ["cloud", "grader"],
        },
        "allow_cloud": True,
    },
    "updates": {"check_time": "03:30"},
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    hour, minute = str(value).split(":")
    return time(int(hour), int(minute))


def weekday_index(name: str) -> int:
    return WEEKDAYS.index(name.lower())


@dataclass
class Holiday:
    name: str
    start: date
    end: date


@dataclass
class Config:
    raw: dict
    telegram_token: str = ""
    first_parent_id: int | None = None
    data_dir: Path = Path("data")
    code_dir: Path | None = None
    holidays: list[Holiday] = field(default_factory=list)

    @property
    def schedule(self) -> dict:
        return self.raw["schedule"]

    @property
    def vocabulary(self) -> dict:
        return self.raw["vocabulary"]

    @property
    def student(self) -> dict:
        return self.raw["student"]

    @property
    def llm(self) -> dict:
        return self.raw["llm"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studybuddy.db"


def load_config(path: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    path = Path(path or env.get("CONFIG_PATH", "config.yaml"))
    user = {}
    if path.exists():
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _merge(DEFAULTS, user)

    holidays = [
        Holiday(h.get("name", "Lov"), date.fromisoformat(str(h["from"])), date.fromisoformat(str(h["to"])))
        for h in raw.get("holidays") or []
    ]
    first_parent = env.get("FIRST_PARENT_CHAT_ID", "").strip()
    code_dir = env.get("CODE_DIR")
    return Config(
        raw=raw,
        telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        first_parent_id=int(first_parent) if first_parent else None,
        data_dir=Path(env.get("DATA_DIR", "data")),
        code_dir=Path(code_dir) if code_dir else None,
        holidays=holidays,
    )
