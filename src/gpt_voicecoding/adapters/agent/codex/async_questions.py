"""Codex's asynchronous questions, as this lane holds them for one thread (#379).

A model calls `request_user_input_async` and keeps working. The question reaches
every subscribed client as a completed `agentMessage` carrying `questions`
(`[{title, options}]`, no id of its own; the item id is the call id), and the
answer is nothing but a user message framed the way the TUI frames it:
`> {title}\n\n{answer}` (`tui/src/bottom_pane/async_questions/state.rs`,
measured on codex 0.154.0). The app-server has no verb to answer, withdraw or
settle one, and no notification that one was handled.

So what this module holds is a reading of the thread, not a handle on the
TUI's panel. A question stays held until one of these is seen, which is codex
0.155's own rule for the panel:

1. a user message whose leading quote block names it (`> {title}` lines, then a
   blank line) — that ends the questions it names and no others;
2. a user message that is not an answer — that ends every held question;
3. the Session ending (the caller clears this).

A turn ending ends nothing. Titles are the only key, so two questions with the
same title cannot be told apart; that is accepted (#379).

**Where the words are spelt.** The question's own fields and the answer's
frame are written here and nowhere else; the item and turn fields they sit in
are the ones the rest of this lane already reads.

**No legacy behaviour to port (ADR 0010).** Legacy learned Codex turn ends from
hooks and never read a thread's items, and `request_user_input_async` did not
exist; answering a Codex question remotely is **new**.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from gpt_voicecoding.adapters.agent.codex import thread_tail
from gpt_voicecoding.seams.agent import Option, WaitingFor, WaitingKind

#: The `agentMessage` field holding the questions, and each question's fields
#: (`AsyncUserInputQuestion` in codex 0.154.0's generated schema).
QUESTIONS: Final = "questions"
TITLE: Final = "title"
OPTIONS: Final = "options"

#: How the TUI marks the question an answer is for: one quoted line per title,
#: then a blank line, then the answer (`AnsweredQuestion::render`).
QUOTE: Final = "> "
FRAME_END: Final = "\n\n"


@dataclass(frozen=True, slots=True)
class AsyncQuestion:
    """One question a thread asked and has not had answered."""

    item_id: str
    title: str
    options: tuple[str, ...] = ()


@dataclass(slots=True)
class HeldQuestions:
    """The questions one thread is waiting on, oldest first."""

    held: list[AsyncQuestion] = field(default_factory=list)
    #: Every question item already read, answered or not, so one delivered
    #: twice cannot come back once it is answered — the TUI's `seen_ids` rule.
    seen: set[str] = field(default_factory=set)
    #: The questions a Stop has already carried. One question, one Stop: a
    #: turn ending under questions the user was already told about is the same
    #: decision asked twice.
    announced: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.held)

    def heard(self, item: Any) -> bool:
        """Read one completed thread item. True when it raised a new question."""
        if not isinstance(item, Mapping):
            return False
        kind = item.get("type")
        if kind == thread_tail.USER_ITEM:
            self._answered_by(thread_tail.user_text(item.get("content")))
            return False
        if kind != thread_tail.AGENT_ITEM:
            return False
        item_id = item.get("id")
        if not isinstance(item_id, str) or item_id in self.seen:
            return False
        asked = _questions_in(item_id, item.get(QUESTIONS))
        if not asked:
            return False
        self.seen.add(item_id)
        self.held.extend(asked)
        return True

    def replay(self, thread: Any) -> None:
        """Work the held questions out again from a thread's whole history.

        What an engine restart or a resubscription has instead of the live
        notifications it missed. The history is the authority, so what was
        seen before is read again. A question a Stop in this process already
        carried stays announced; one the user was never told about — every
        question after a restart — is told at the next turn end.
        """
        self.held.clear()
        self.seen.clear()
        turns = thread.get("turns") if isinstance(thread, Mapping) else None
        for turn in turns if isinstance(turns, list) else ():
            items = turn.get("items") if isinstance(turn, Mapping) else None
            for item in items if isinstance(items, list) else ():
                self.heard(item)

    def clear(self) -> None:
        """Drop every held question — the Session ended. What was seen stays seen."""
        self.held.clear()

    def waiting(self) -> WaitingFor | None:
        """Every held question as one decision, the way a Claude call with several is.

        Titles joined in order, options flattened in order (`stop_analysis.question_in`).
        Codex marks no option as recommended, so there is no recommendation.
        """
        if not self.held:
            return None
        return WaitingFor(
            kind=WaitingKind.QUESTION,
            prompt="\n".join(question.title for question in self.held),
            options=tuple(
                Option(text=option) for question in self.held for option in question.options
            ),
        )

    def to_announce(self) -> WaitingFor | None:
        """What a Stop should carry now, or `None` when the user has been told it all.

        Every held question once any of them is new, and everything held is
        then counted as told. `None` both when nothing is held and when all of
        it was carried already.
        """
        if all(question.item_id in self.announced for question in self.held):
            return None
        self.announced.update(question.item_id for question in self.held)
        return self.waiting()

    def answer(self, words: str) -> tuple[str, tuple[str, ...]]:
        """The user message that answers what is held, and the questions it answers.

        One question: words naming one of its options (ignoring case and
        spacing) become that option as codex wrote it — what the TUI sends for
        a choice; anything else goes as written, trimmed at the ends as the
        TUI trims its free-text box. Several: the words go whole under every title, and the Session
        splits them (measured twice on 2026-09-17, #379).
        """
        spoken = words.strip()
        if len(self.held) == 1:
            (question,) = self.held
            spoken = _as_offered(question.options, spoken)
        frame = "".join(f"{QUOTE}{question.title}\n" for question in self.held)
        return f"{frame}\n{spoken}", tuple(question.item_id for question in self.held)

    def settle(self, item_ids: tuple[str, ...]) -> None:
        """Drop the questions an answer this lane delivered was for."""
        self.held[:] = [question for question in self.held if question.item_id not in item_ids]

    def _answered_by(self, text: str) -> None:
        header, framed, _ = text.partition(FRAME_END)
        if not framed or not header.startswith(QUOTE):
            # Not an answer: codex 0.155 drops every pending question when the
            # user sends something else, and so does this reading.
            self.held.clear()
            return
        # An answer that names nothing held — a question already answered from
        # here and answered again in the TUI, whose panel still shows it — is
        # still an answer, so it overtakes nothing.
        lines = f"\n{header}\n"
        self.held[:] = [
            question for question in self.held if f"\n{QUOTE}{question.title}\n" not in lines
        ]


def _questions_in(item_id: str, listed: Any) -> list[AsyncQuestion]:
    if not isinstance(listed, list):
        return []
    asked: list[AsyncQuestion] = []
    for question in listed:
        if not isinstance(question, Mapping):
            continue
        title = question.get(TITLE)
        if not isinstance(title, str) or not title.strip():
            continue
        options = question.get(OPTIONS)
        asked.append(
            AsyncQuestion(
                item_id=item_id,
                title=title,
                options=tuple(
                    option
                    for option in (options if isinstance(options, list) else ())
                    if isinstance(option, str) and option.strip()
                ),
            )
        )
    return asked


def _as_offered(options: tuple[str, ...], words: str) -> str:
    spoken = _comparable(words)
    for option in options:
        if _comparable(option) == spoken:
            return option
    return words


def _comparable(text: str) -> str:
    return " ".join(text.split()).casefold()
