"""Choose a descriptive task from agent records, climbing but never falling.

ADR 0024 and #280 replace self-report and frozen names with existing records.
The legacy validation is adapted: refuse a candidate as a value and try the
next rung. Core logs the reasons and holds the choice; this module has no I/O.
Project resolution stays with the adapters and retains its existing rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from gpt_voicecoding.seams.identity import NAME_SEPARATOR, AgentKind, SessionName

# codex 0.150.0 THREAD_TITLE_MAX_CHARS, tui/src/app/thread_title.rs:22.
# This is the daemon's format, not the configurable first-words length.
PROVISIONAL_TITLE_CHARACTERS = 36
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_IMAGE_MARKER = re.compile(r"\[Image #\d+\]")


class NameRung(IntEnum):
    """Relative authority of the record that supplied a task."""

    DERIVED = 0
    FIRST_PROMPT = 1
    AI_TITLE = 2
    USER_NAME = 3


@dataclass(frozen=True, slots=True)
class NameChoice:
    task: str
    rung: NameRung


@dataclass(frozen=True, slots=True)
class NamingResult:
    choice: NameChoice | None
    refusals: tuple[str, ...] = ()


def choose_task(
    agent: AgentKind,
    *,
    first_prompt_characters: int,
    previous: NameChoice | None = None,
    user_name: str | None = None,
    ai_title: str | None = None,
    first_prompt: str | None = None,
    derived_name: str | None = None,
    thread_name: str | None = None,
    preview: str | None = None,
    short_thread_id: str | None = None,
) -> NamingResult:
    """The best available task, or the previous task if all sources fell away."""
    if agent is AgentKind.CODEX:
        prompt = " ".join((preview or "").split())
        provisional = bool(prompt) and " ".join((thread_name or "").split()) == " ".join(
            prompt[:PROVISIONAL_TITLE_CHARACTERS].split()
        )
        candidates = (
            (NameRung.AI_TITLE, None if provisional else thread_name),
            (NameRung.FIRST_PROMPT, preview),
            (NameRung.DERIVED, short_thread_id),
        )
    else:
        candidates = (
            (NameRung.USER_NAME, user_name),
            (NameRung.AI_TITLE, ai_title),
            (NameRung.FIRST_PROMPT, first_prompt),
            (NameRung.DERIVED, derived_name),
        )
    refusals: list[str] = []
    for rung, raw in candidates:
        if raw is None:
            continue
        task = (
            _first_words(raw, first_prompt_characters)
            if rung is NameRung.FIRST_PROMPT
            else raw.strip()
        )
        reason = _refusal(task)
        if reason is None and rung is NameRung.FIRST_PROMPT:
            reason = _absolute_path_refusal(task)
        if reason:
            refusals.append(f"{agent} {rung.name}: {reason}")
            continue
        choice = NameChoice(task, rung)
        if previous is not None and previous.rung > rung:
            choice = previous
        return NamingResult(choice, tuple(refusals))
    return NamingResult(previous, tuple(refusals))


def _first_words(raw: str, limit: int) -> str:
    args = _COMMAND_ARGS.search(raw)
    command = _COMMAND_NAME.search(raw)
    if command is not None:
        raw = args.group(1) if args is not None and args.group(1).strip() else command.group(1)
    raw = _IMAGE_MARKER.sub("", raw)
    return " ".join(raw.split())[:limit].strip()


def _absolute_path_refusal(task: str) -> str | None:
    """The first-words rung's own refusal (ADR 0024, amendment 2026-09-09).

    A first prompt that opens with a path names a place on this machine, not a
    task; forty characters of a home directory outranked a derived name all of
    2026-09-09. A relative path is kept: `crewtask/21` is what the user handed
    the driver and says so.
    """
    if task[:1] in ("/", "~"):
        return "a task that is an absolute path"
    return None


def _refusal(task: str) -> str | None:
    if not task:
        return "a task with no words in it"
    if len(task.splitlines()) > 1:
        return "a task spanning more than one line"
    if NAME_SEPARATOR.strip() in task:
        return f"a task carrying {NAME_SEPARATOR.strip()!r}"
    return None


def compose(project_name: str, task: str) -> SessionName | None:
    """Compose the same two halves as before, without logging or raising."""
    project, wanted = project_name.strip(), task.strip()
    if not project or NAME_SEPARATOR.strip() in project or _refusal(wanted):
        return None
    return SessionName(project=project, task=wanted)
