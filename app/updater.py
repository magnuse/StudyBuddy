"""Self-update support. The container's entrypoint pulls the code; the bot only checks and restarts."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import VERSION

log = logging.getLogger(__name__)


@dataclass
class VersionInfo:
    version: str
    commit: str
    subject: str


def _git(code_dir: Path, *args: str, timeout: int = 60) -> str:
    result = subprocess.run(["git", "-C", str(code_dir), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def current_version(code_dir: Path | None) -> VersionInfo:
    if code_dir and (code_dir / ".git").exists():
        try:
            commit = _git(code_dir, "rev-parse", "--short", "HEAD")
            subject = _git(code_dir, "log", "-1", "--format=%s")
            return VersionInfo(VERSION, commit, subject)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            log.warning("Could not read git version: %s", exc)
    return VersionInfo(VERSION, "okänd", "")


def update_available(code_dir: Path | None, branch: str, state_dir: Path | None = None) -> list[str]:
    """Fetches the branch and returns the subjects of new commits, newest first. Empty if up to date."""
    if not code_dir or not (code_dir / ".git").exists():
        return []
    _git(code_dir, "fetch", "--quiet", "origin", branch, timeout=120)
    bad = state_dir / "bad_commit" if state_dir else None
    if bad and bad.exists() and bad.read_text().strip() == _git(code_dir, "rev-parse", f"origin/{branch}"):
        return []  # the newest commit is the one that failed to start; wait for a fix
    log_output = _git(code_dir, "log", "--format=%s", f"HEAD..origin/{branch}")
    return [line for line in log_output.splitlines() if line.strip()]


def mark_healthy(state_dir: Path | None) -> None:
    """Called once the bot is connected to Telegram: this commit is good, reset the crash counter."""
    if not state_dir or not state_dir.exists():
        return
    (state_dir / "crash_count").write_text("0")
    candidate = state_dir / "candidate_commit"
    if candidate.exists():
        (state_dir / "last_good_commit").write_text(candidate.read_text())


def read_rollback_note(state_dir: Path | None) -> str | None:
    if not state_dir:
        return None
    note = state_dir / "rollback_note"
    if note.exists():
        text = note.read_text().strip()
        note.unlink()
        return text or None
    return None


async def restart_soon(delay: float = 2.0) -> None:
    """Exits the process. Docker's restart policy starts it again, and the entrypoint pulls new code."""
    await asyncio.sleep(delay)
    log.info("Restarting to update")
    os._exit(0)
