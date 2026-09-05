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

**One button per option label** (ADR 0021 §6). Whatever kind the notice is, its
`options` become an inline keyboard — `keyboard` — with the 1-based position as
`callback_data`, so a press comes back as the numeral typed in a reply and the
label's words are never read. A label wider than the configured width is cut
on the button and ended with a mark; **the numbered line in the text carries it
whole**, which is what lets the user tell two buttons apart when the cut left
them reading alike. A menu screen is the heading in bold and the labels
numbered. The reply bar is not this module's: `send` opens one when Core asked
for it, on the last part of the message, which is a thing only `send` can see.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    DEFAULT_BUTTON_LABEL_WIDTH,
    MESSAGE_LIMIT_UTF16_UNITS,
)
from gpt_voicecoding.seams.companion_channel import (
    BriefState,
    MenuNotice,
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

#: What ends a label cut for a button. A symbol, not a word: the whole label is
#: on the notice line, and the button only has to be recognisable as it.
CUT_MARK: Final = "…"


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
    #: The `reply_markup` — an inline keyboard drawn from the labels — or None
    #: when the message offers nothing to press.
    reply_markup: dict[str, object] | None = None

    def payload(self) -> dict[str, object]:
        """The fields of the API call this layout decides."""
        body: dict[str, object] = {"text": self.text}
        if self.entities:
            body["entities"] = list(self.entities)
        if self.reply_markup is not None:
            body["reply_markup"] = self.reply_markup
        return body


def lay_out(
    notice: Notice,
    *,
    limit: int = MESSAGE_LIMIT_UTF16_UNITS,
    label_width: int = DEFAULT_BUTTON_LABEL_WIDTH,
) -> LaidOut:
    """Arrange one brief in this surface's shape, inside one message.

    **One button per option label, in order** (ADR 0021 §6): every kind of
    notice that carries labels gets the same keyboard, and one that carries
    none gets no markup. A reply bar is not decided here — it is `send`'s
    `reply_bar`, which belongs to the last part of a message this never sees.
    """
    if isinstance(notice, RosterNotice):
        # **The keyboard follows the rows that survived the cap** (#264 review).
        # A roster is one message and its rows go from the back (#262), so a
        # long one shows the first N; a button for a row the user cannot see is
        # a button they cannot match to anything, and 60 Sessions drew 60
        # buttons under 30 lines. Positions are unaffected: the rows that stay
        # are the leading ones, so label N is still row N.
        laid_out, kept = _roster(notice, limit=limit)
        return replace(
            laid_out, reply_markup=keyboard(notice.options[:kept], label_width=label_width)
        )
    if isinstance(notice, MenuNotice):
        laid_out = _menu(notice)
    else:
        laid_out = _session(notice, limit=limit)
    return replace(laid_out, reply_markup=keyboard(notice.options, label_width=label_width))


def keyboard(
    labels: Sequence[str], *, label_width: int = DEFAULT_BUTTON_LABEL_WIDTH
) -> dict[str, object] | None:
    """One inline button per label, `callback_data` its 1-based position.

    The position, never the label: a press comes back as the numeral typed
    in a reply (ADR 0021 §4), and the label's words are never read. Within
    the API's 64-byte bound by construction — a position is a few digits.
    A label wider than `label_width` is cut on the button and ended with the
    cut mark; **the notice line carries it whole, and that is what tells two
    buttons apart** — the cut takes the end of a label, which is exactly where
    a roster puts the address that distinguishes two Sessions sharing a name
    (#264 review). Rows wrap: a row holds as many buttons as fit inside
    `label_width` counted together, so short labels share a row and a long one
    sits alone. Order is preserved, and the order is the numbering.
    """
    if not labels:
        return None
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    width = 0
    for position, label in enumerate(labels, 1):
        shown = _fitted_label(label, label_width)
        cost = utf16_length(shown)
        if row and width + cost > label_width:
            rows.append(row)
            row, width = [], 0
        row.append({"text": shown, "callback_data": str(position)})
        width += cost
    rows.append(row)
    return {"inline_keyboard": rows}


def _fitted_label(label: str, width: int) -> str:
    """The label whole when it fits the button, else cut and ended with the mark."""
    if utf16_length(label) <= width:
        return label
    room = max(0, width - utf16_length(CUT_MARK))
    return label[: prefix_within(label, room)].rstrip() + CUT_MARK


def _menu(notice: MenuNotice) -> LaidOut:
    """The heading in bold, then the labels numbered — the shape a question block has."""
    lines = [notice.heading, *(f"{n}. {label}" for n, label in enumerate(notice.options, 1))]
    return LaidOut(
        text="\n".join(lines),
        entities=(_entity(BOLD, before="", covers=notice.heading),),
    )


def _roster(notice: RosterNotice, *, limit: int) -> tuple[LaidOut, int]:
    """One numbered line per Session — light, name, agent, state word — and the counts under it.

    **The counts line never goes; rows go from the back.** The same rule the
    hand-over applies at the Call seam's ceiling (`briefing.handover`): the
    counts are Core's summary of every Session, so a roster that could carry
    thirty of two hundred rows still says two hundred are there, and what it
    could not carry is named by number rather than silently absent. Rows are
    in Briefing's order, the Focus Session first, so the back is the least
    asked-about end. **A roster is one message, never split**: it is one Anchor
    whose labels resolve by position (ADR 0021 §6), and a second message would
    be a second Anchor with its own numbering (Advisor ruling on #262).
    """
    rows = [
        f"{_position(index, notice.options)}"
        f"{STATE_LIGHT[row.state]} {row.name}{SEPARATOR}{row.agent}{SEPARATOR}{row.state_word}"
        for index, row in enumerate(notice.rows, 1)
    ]
    while rows and utf16_length("\n".join([*rows, notice.counts])) > limit:
        rows.pop()
    return LaidOut(text="\n".join([*rows, notice.counts])), len(rows)


def _position(index: int, options: Sequence[str]) -> str:
    """`<n>. ` on a roster that offers its rows to be picked, nothing on one that does not.

    A roster with labels is an Anchor whose rows resolve by position, so the
    number is on the line the user reads — it is what a typed numeral answers
    and what tells them which of two buttons reading alike is which. A roster
    with no labels offers nothing to pick, and numbering it would invite a
    numeral that gets refused.
    """
    return f"{index}. " if options else ""


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
    the fold is the marker alone; when not even the whole marker fits, there
    is no fold at all — the marker is Core's fixed words, and a partial one is
    words nobody wrote (Advisor ruling on #262). The headline is never
    the thing that gives way, and the message never leaves here over the cap:
    a notice is one message (ADR 0021 §5), and a message Telegram refuses is a
    notice nobody receives.
    """
    if utf16_length(original) <= budget:
        return original
    room = budget - utf16_length(marker) - 1
    if room <= 0:
        return marker if utf16_length(marker) <= budget else ""
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
