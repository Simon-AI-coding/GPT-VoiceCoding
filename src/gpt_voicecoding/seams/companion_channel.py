"""The Companion Channel seam — reaching the user when no Live Call is up.

Verbs Bridge Core calls: `send(message)` and `verify` (liveness — ADR 0003).

Events raised upward: inbound user text, **unclassified**. Deciding whether
inbound text is a control-plane command, an Answer Relay, or a delegation is
Bridge Core's job and never the channel's — which is why the event is named for
what it is (text that arrived) and not for what it might mean, and why it has no
field an adapter could use to volunteer an opinion.

**The adapter reports facts about a message, never an opinion about meaning**
(ADR 0021 §4). Which message the user's text answered (`in_reply_to`), where it
came from (`origin`) and which provider ids a push landed under
(`ChannelReceipt.message_ids`) are things the adapter can *see*; what any of them
means — which Session a reply was for, whether a numeral picks an option — is
read by Bridge Core against its own tables. Every one of them is an opaque
string Core matches by equality and never parses; a channel with no reply
concept emits empty and gets Core's default rule.

The null implementation is a real implementation. It reports the empty module
string from `verify` and returns a positive non-delivery from `send` — a
`ChannelReceipt` with no ids — never something Bridge Core could mistake for
delivery. It never emits.

**A notice crosses as a structured brief, beside its text** (ADR 0021 §5). Core
fills `SessionNotice` / `RosterNotice` from `core/briefing.py`'s wording tables
— the same words `briefing.text` prints — and hands one to `send` as the
optional `notice` keyword; the `text` beside it is that rendering, so a channel
that draws no layout (the null one, a log) prints it and loses nothing, and one
that does (Telegram) lays the brief out and chooses no words. The brief is an
optional argument rather than a second verb, and its types live here rather
than in a shared module, on the Call seam's precedent: `SpokenBrief` is that
seam's own carrier in `seams/call.py`, filled by `briefing.spoken`. `BriefState`
is the one non-string field, so an adapter can key a state light on a closed
enum instead of on English words — the words stay Core's; the light is layout.

Adapters: Telegram is the generic public one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyResult


@dataclass(frozen=True, slots=True)
class InboundText(Event):
    """Text the user sent. What it *means* is Bridge Core's to decide.

    `origin` is the adapter's own opaque reference to where it came from — a
    chat id, a socket path. Bridge Core does not parse it; it exists so a reply
    can go back the way the text came, echoed into `send(origin=...)`.

    `in_reply_to` is the adapter's opaque id of the message this text answered,
    and empty when it answered nothing. Of the same nature as `origin`: a fact
    the adapter saw, matched by Core by string equality against the ids earlier
    `ChannelReceipt`s reported. An adapter with no reply concept leaves it empty.
    """

    text: str
    origin: str = ""
    in_reply_to: str = ""


@dataclass(frozen=True, slots=True)
class ChannelReceipt(DeliveryReceipt):
    """One push's classification, plus the provider ids of every part that landed.

    A `DeliveryReceipt` first: every existing `send` site keeps reading
    `is_delivered`. `message_ids` is in sending order, and an UNKNOWN receipt from
    a split send that failed after a part landed still lists the parts that did —
    those messages exist and the user can reply to them. Empty when nothing
    landed, and when what was sent is not a message (the null channel, a toast).
    """

    message_ids: tuple[str, ...] = ()


class BriefState(StrEnum):
    """What one Session is doing, in the five words the user is ever told.

    Deliberately not `SessionState`: that is the agent's own vocabulary for a
    lifecycle (`running`, `idle`, `waiting`), and these five are what the user
    is owed — three of them actionable, one of them the honest admission that
    something could not be read.

    **Defined at the seam, read by Core.** The words for each state are Core's
    (`core/briefing.py::STATE_WORDING`, ADR 0021 §5), but the closed set of
    states is a fact an adapter needs apart from the words: the Telegram layout
    lights one symbol per state, and keying that on English would make the
    adapter read words it is not allowed to choose. One definition, here, so the
    channel carries the same enum Briefing derives — Core imports the seams and
    the seams import nothing of Core's, so this is the one direction it can go.
    """

    #: A question is waiting for the user, or a Codex turn ended (#166 B2).
    DECISION = "decision"
    #: A permission dialog is open.
    PERMISSION = "permission"
    #: This turn is done and the Session is idle for a new instruction (Q7).
    FINISHED = "finished"
    #: Mid-turn. Nothing is being asked of the user.
    RUNNING = "running"
    #: It stopped, and what it stopped on or what it said could not be read.
    #: **Never counted as a decision** (#166 B7): the brief carries whatever was
    #: read, and says plainly what it could not.
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SessionNotice:
    """One Session Brief as this seam carries it — Briefing's words, as data.

    Every string is one Briefing worded; the adapter assembles and never
    phrases. `options` are the labels in order, because a numeral picks one by
    position (ADR 0021 §3) and a surface that can draw buttons draws one per
    label (§6); empty labels draw no buttons. `question` is empty for a notice
    that asks nothing — a turn that `finished` — and the layout is the same with
    the slot empty. `newest` is the newest message **whole**, or the omission
    words: cutting it to a surface's limit is the adapter's layout act (§5), and
    this carrier never holds a cut one — what it holds is `cut_marker`, the words
    Core chose for the place a surface had to cut, so the cut is marked in the
    user's vocabulary and the adapter still chooses none of it.
    """

    state: BriefState
    state_word: str
    agent: str
    #: The Session Name where there is one, else the address (`_headline`'s rule).
    name: str
    question: str = ""
    options: tuple[str, ...] = ()
    #: Briefing's whole line for the recommendation, empty when there is none.
    recommendation: str = ""
    newest: str = ""
    #: What ends a folded original a surface had to cut — Core's words.
    cut_marker: str = ""
    #: Whether the user can answer from this surface, and Briefing's line for it.
    answerable_here: bool = True
    answer_wording: str = ""
    #: Why the user's last reply never arrived, in Briefing's words — empty when
    #: nothing is undelivered, which is the ordinary case and draws no line.
    undelivered: str = ""

    def __post_init__(self) -> None:
        if not self.state_word.strip():
            raise ValueError("a notice says what the Session is doing")


@dataclass(frozen=True, slots=True)
class RosterRowNotice:
    """One roster line's facts: the state to light, and the three words to print."""

    state: BriefState
    state_word: str
    agent: str
    name: str


@dataclass(frozen=True, slots=True)
class RosterNotice:
    """The Roster Brief as this seam carries it — rows in Briefing's order, counts whole.

    `counts` is the whole counts line, heading included, for the reason
    `SpokenRosterBrief` gives: with a Focus Session the counts are *the others*,
    and that is a fact Briefing states, not a label an adapter writes.
    """

    rows: tuple[RosterRowNotice, ...]
    counts: str


#: What `send` may be handed beside its text. Two kinds, and an adapter lays
#: each out in one visual language (ADR 0021 §5).
Notice = SessionNotice | RosterNotice


#: The closed set of events this seam raises. Nothing else may appear.
CompanionChannelEvent = InboundText


@runtime_checkable
class CompanionChannel(Protocol):
    """Text reach when there is no call. Mechanism only — routing policy is Core's."""

    async def send(
        self,
        text: str,
        *,
        request_id: RequestId,
        origin: str = "",
        revises: tuple[str, ...] = (),
        notice: Notice | None = None,
    ) -> ChannelReceipt:
        """Push one message to the user, and say which ids it landed under.

        `origin` is the inbound event's `origin`, echoed when this is a reply to
        it; empty for an unbidden push. `revises` is the ids from an earlier
        receipt: non-empty means "replace those messages' content with this
        text" rather than send a new one. An adapter that cannot edit ignores
        it and reports as it always did. `notice` is the structured brief the
        `text` renders, when the message is one: an adapter that lays notices
        out lays this one out and sends that instead of `text`; one that does
        not sends `text` and ignores it. The words are the same either way.
        """
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...
