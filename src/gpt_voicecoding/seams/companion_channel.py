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

**A conversation's messages carry no brief, only a reply bar** (ADR 0021 §7).
An Assistant Conversation's opening line and every answer of it are prose — one
fixed, the rest the coding model's own — and a notice is a laid-out brief that a
surface *cuts* to its limit. An answer must **split** instead, every part of it
an Anchor, so it crosses as text with `reply_bar` set and no `notice` at all.

**Options are labels, and a surface that can draw buttons draws one per label**
(ADR 0021 §6). Every notice that offers choices carries them as `options`, in
order — a question's labels, a permission's `allow` / `deny`, a roster's
Session names, a menu screen's words — and a numeral picks one by position.
`send` grows no `options` argument and this seam has no word for a button: an
adapter that draws them draws the labels as it draws everything else, and a
press comes back as the numeral it stood for. `MenuNotice` is the third kind,
for the screens the command menu opens.

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


#: The longest reply-bar placeholder a surface will carry. The Bot API's own
#: bound on `ForceReply.input_field_placeholder`, stated at the seam so Core
#: stays inside it and no adapter silently cuts the user's own vocabulary.
PLACEHOLDER_LIMIT = 64


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
    #: One label per row, in row order, for a surface that draws choices (ADR
    #: 0021 §6): the roster is an Anchor whose choices are the live Sessions,
    #: and a numeral picks one by position. Core words the labels — the name,
    #: with an address after it where two rows share one — and the adapter
    #: draws them verbatim. Empty for a roster that offers nothing to pick.
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.options and len(self.options) != len(self.rows):
            raise ValueError(
                f"a roster's labels are its rows: {len(self.options)} label(s) cannot "
                f"stand for {len(self.rows)} row(s)"
            )


@dataclass(frozen=True, slots=True)
class MenuNotice:
    """A menu screen as this seam carries it: a heading, and the labels a numeral picks from.

    The screens the command menu opens (ADR 0021 §6, #264) — a greeting for one
    Session, the config screen, the switch screen, the `Say to <name>:` prompt
    — are one shape: a heading in Core's words and the option labels in order,
    each of which a numeral picks by position. Every string is Core's; the
    adapter numbers, draws, and chooses none.

    **A screen that asks for words says so through `send(reply_bar=...)`, not
    here.** `expects_words` used to be a second signal for the same thing — it
    existed only to make a surface open its reply bar — so one message could
    want a bar for two unrelated reasons and an adapter had two fields to read.
    There is one signal now: the words in the bar, given to the verb that sends
    the message, which is also the verb that knows which part of a split send
    the bar belongs under (ADR 0021 §7, #265).
    """

    heading: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.heading.strip():
            raise ValueError("a menu screen says what it is for")


#: What `send` may be handed beside its text. Three kinds, and an adapter lays
#: each out in one visual language (ADR 0021 §5, §6).
Notice = SessionNotice | RosterNotice | MenuNotice


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
        reply_bar: str = "",
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

        `reply_bar` opens the surface's reply bar with those words written into
        it as a placeholder — a courtesy on the `Say to <name>:` prompt and on
        an Assistant Conversation's messages (ADR 0021 §6, §7), never the
        routing, which is the Anchor rule alone. On a send that splits it
        belongs to the **last** part, because that is the message the bar sits
        under. An adapter with no reply bar ignores it. The words are Core's,
        and `PLACEHOLDER_LIMIT` is the bound they stay inside.
        """
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...
