"""How a structured brief is laid out for Telegram — Core's words, this surface's shape.

ADR 0021 §5: Core fills the brief and chooses every word in it; this module
arranges those words and chooses none. What it adds is **symbols and markup**
only — one state light per `BriefState`, the numbering of the option labels,
and two entities: the question in bold and the original folded in an
`expandable_blockquote`. Nothing here is a sentence the user reads that Core
did not write.

**Plain text plus `entities`, never MarkdownV2.** MarkdownV2's eighteen-character
escape list applies inside blockquote bodies too, and every escaping bug shows
up as a refused `sendMessage`. Entities are offsets over the plain text, so a
stray `*`, `_`, backtick or backslash in the agent's words cannot break a
notice, and fenced code inside the original shows as literal characters —
Telegram forbids `pre` and `code` inside a blockquote, and that is accepted.
Offsets and lengths are in UTF-16 code units, which is the API's ruler
(`utf16_length`).

**A notice is one message.** The headline — light, state word, agent, name,
question, options, recommendation, and the lines below the fold — is never cut.
The original is cut to what remains of `MESSAGE_LIMIT_UTF16_UNITS`, preferring
the last line break, then the last space (`split_message`'s rule), and the fold
then ends with the marker Core put on the brief (`cut_marker`), so the cut is
marked in the user's vocabulary. An original that fits exactly gets no marker;
a headline that alone fills the cap leaves an original of the marker and
nothing else. **This is the one place a message text is cut** (ADR 0021 §5,
amending ADR 0016 to that extent): the brief still carries the original whole,
and `split_message` is for the messages that are not notices.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    MESSAGE_LIMIT_UTF16_UNITS,
)
from gpt_voicecoding.seams.companion_channel import (
    BriefState,
    Notice,
    RosterNotice,
    SessionNotice,
)

#: One light per state: layout, not vocabulary. The words beside them are Core's.
STATE_LIGHT: Final[Mapping[BriefState, str]] = {
    BriefState.DECISION: "🟡",
    BriefState.PERMISSION: "🔴",
    BriefState.FINISHED: "🟢",
    BriefState.RUNNING: "🔵",
    BriefState.UNREADABLE: "⚪",
}

#: What separates the facts on one line — `agent · project · task`. The same
#: character a Session Name is already spelled with (`seams/identity.py`).
SEPARATOR: Final = " · "

#: The Bot API's entity types this layout uses, and no others.
BOLD: Final = "bold"
EXPANDABLE_BLOCKQUOTE: Final = "expandable_blockquote"


def utf16_length(text: str) -> int:
    """How long Telegram thinks this string is. `len()` is the wrong ruler.

    The API's 4096 cap counts UTF-16 code units, so an emoji costs two and a
    message of 3000 emoji is over the limit while `len()` says it is not.
    """
    return len(text.encode("utf-16-le")) // 2


def prefix_within(text: str, limit: int) -> int:
    """How many characters fit, counted the way the API counts them.

    Walks code points, so a surrogate pair is never split down the middle.
    """
    units = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            return index
        units += width
    return len(text)


@dataclass(frozen=True, slots=True)
class LaidOut:
    """One message as `sendMessage` takes it: the text, and the entities over it."""

    text: str
    #: `MessageEntity` objects, offsets and lengths in UTF-16 code units.
    entities: tuple[dict[str, object], ...] = ()

    def payload(self) -> dict[str, object]:
        """The two fields of the API call this layout decides."""
        body: dict[str, object] = {"text": self.text}
        if self.entities:
            body["entities"] = list(self.entities)
        return body


def lay_out(notice: Notice, *, limit: int = MESSAGE_LIMIT_UTF16_UNITS) -> LaidOut:
    """Arrange one brief in this surface's shape, inside one message."""
    if isinstance(notice, RosterNotice):
        return _roster(notice, limit=limit)
    return _session(notice, limit=limit)


def _roster(notice: RosterNotice, *, limit: int) -> LaidOut:
    """One line per Session — light, name, agent, state word — and the counts under it.

    **The counts line never goes; rows go from the back.** The same rule the
    hand-over applies at the Call seam's ceiling (`briefing.handover`): the
    counts are Core's summary of every Session, so a roster that could carry
    thirty of two hundred rows still says two hundred are there, and what it
    could not carry is named by number rather than silently absent. Rows are
    in Briefing's order, the Focus Session first, so the back is the least
    asked-about end.
    """
    rows = [
        f"{STATE_LIGHT[row.state]} {row.name}{SEPARATOR}{row.agent}{SEPARATOR}{row.state_word}"
        for row in notice.rows
    ]
    while rows and utf16_length("\n".join([*rows, notice.counts])) > limit:
        rows.pop()
    return LaidOut(text="\n".join([*rows, notice.counts]))


def _session(notice: SessionNotice, *, limit: int) -> LaidOut:
    """Headline, question block, fold, footer — and the fold is what gives way."""
    head = [
        f"{STATE_LIGHT[notice.state]} {notice.state_word}",
        f"{notice.agent}{SEPARATOR}{notice.name}",
        "",
    ]
    entities: list[dict[str, object]] = []
    if notice.question:
        head.append(notice.question)
        entities.append(_entity(BOLD, before="\n".join(head[:-1]) + "\n", covers=notice.question))
        head.extend(f"{position}. {label}" for position, label in enumerate(notice.options, 1))
        if notice.recommendation:
            head.append(notice.recommendation)
        head.append("")
    above = "\n".join(head) + "\n"

    below_lines = []
    if not notice.answerable_here and notice.answer_wording:
        below_lines.append(notice.answer_wording)
    if notice.undelivered:
        below_lines.append(notice.undelivered)
    below = "".join(f"\n{line}" for line in below_lines)

    budget = limit - utf16_length(above) - utf16_length(below)
    fold = _fitted(notice.newest, notice.cut_marker, budget)
    if fold:
        entities.append(_entity(EXPANDABLE_BLOCKQUOTE, before=above, covers=fold))
    return LaidOut(text=f"{above}{fold}{below}", entities=tuple(entities))


def _fitted(original: str, marker: str, budget: int) -> str:
    """The original whole when it fits; otherwise cut, and ended with the marker.

    The cut prefers the last line break, then the last space, inside what the
    remaining budget allows once the marker and the line break before it are
    paid for — a line break first because a fold that ends on a whole line
    reads as the message it is cut from, and a space only when the window holds
    no line break at all. When nothing of the original fits beside the marker,
    the fold is the marker alone; when not even the marker fits, it is as much
    of the marker as does, and nothing when nothing does. The headline is never
    the thing that gives way, and the message never leaves here over the cap:
    a notice is one message (ADR 0021 §5), and a message Telegram refuses is a
    notice nobody receives.
    """
    if utf16_length(original) <= budget:
        return original
    room = budget - utf16_length(marker) - 1
    if room <= 0:
        return marker[: prefix_within(marker, max(budget, 0))]
    hard = prefix_within(original, room)
    window = original[:hard]
    boundary = window.rfind("\n")
    if boundary <= 0:
        boundary = window.rfind(" ")
    cut = window[:boundary] if boundary > 0 else window
    kept = cut.rstrip()
    return f"{kept}\n{marker}" if kept else marker


def _entity(kind: str, *, before: str, covers: str) -> dict[str, object]:
    """One `MessageEntity`, its offset and length counted in UTF-16 units."""
    return {"type": kind, "offset": utf16_length(before), "length": utf16_length(covers)}
