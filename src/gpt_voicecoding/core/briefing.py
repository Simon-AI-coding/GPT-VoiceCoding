"""Briefing — the one source of words about what a Session is doing.

Pure, in-process, and fed the roster rows Bridge Core has already folded: no
I/O, no clock, no lane. Eight functions and nothing else —

    briefing.roster(sessions, focus)  -> RosterBrief
    briefing.session(session)         -> SessionBrief
    briefing.omitting_newest(brief)   -> SessionBrief
    briefing.text(brief)              -> str
    briefing.spoken(brief)            -> SpokenBrief
    briefing.for_call(sessions, ...)  -> tuple[HandoverItem, ...]
    briefing.notice(brief)            -> SessionNotice
    briefing.roster_notice(brief)     -> RosterNotice

`spoken` and `for_call` are the Live Call's two: the first hands one brief across
the Call seam as the seam's own carrier (no Core type crosses a seam, ADR 0001),
the second selects the dial-time background, opening or mid-call answer. `notice` and
`roster_notice` are the Companion Channel's pair, on the same precedent (ADR
0021 §5): the seam's own carrier, filled from the tables below, laid out by the
adapter. All four are still only words about Sessions — the carriers are filled
with the wording below, and the adapter that puts them on the wire chooses none
of it.

`text` is the **only** text renderer for Session state. The engine log and
``bridgectl brief`` print what it returns, and the Companion Channel is handed
it beside the structured brief, so there is one place the wording lives and no
two surfaces can describe one Session two ways. That was the defect #166 named:
six renderers, each with its own half of the vocabulary, and a Session that read
as *waiting* on one surface and *idle* on another. What ADR 0021 §5 adds is that
**the words must not fork; a layout per surface may**: the Telegram adapter
arranges these same strings in its own shape, as the realtime adapter already
does with `spoken`.

**The engine never condenses.** `newest` is the newest assistant message whole,
under ADR 0016's omission rules, and the decision carries the prompt, every
option and any recommendation. The one-line conclusion the user hears and the
detail they may ask for are the *same field*: the Voice condenses it for a
brief and reads it whole for detail, and the channel shows it whole. An engine
that summarised would be deciding what the user is told, in a place where the
decision cannot be reviewed.

**A brief is derived from one row, never remembered.** Every value here comes
off the `Session` it was handed, so a brief taken now says what is true now —
legacy's "exactly one fetch, read at the moment you speak"
(`legacy@1d32845:skill/announcing.md` step 1, `bridge/host.py:399-405`),
**ported**. The aggregate Roster Brief and the Focus Session are **new**:
legacy ran one job at a time and had neither (`bridge/coordinator.py:836-838`).
FINISHED is legacy's "finished its turn and is waiting for the user"
(`bridge/host.py:226-234`), **ported**; `decision.recommendation` is its single
recommendation (`bridge/transcript.py:1736-1741`), **ported**.

Module-level functions rather than a class, because there is no state to hold:
the caller writes `briefing.roster(...)`, which reads the same as `#166`'s
`Briefing.roster(...)` and cannot grow a constructor.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from gpt_voicecoding.core.call_keeper import Occasion
from gpt_voicecoding.core.sessions import UNNAMED_SESSION, Session, spoken_name
from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    ProgressObservation,
    ProgressOmission,
    ProgressPhase,
    ProgressRole,
    ReplyWindow,
    SessionState,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.call import (
    HANDOVER_BUDGET_BYTES,
    MAX_HANDOVER_ITEMS,
    HandoverItem,
    SpokenBrief,
    SpokenRosterBrief,
)
from gpt_voicecoding.seams.companion_channel import (
    BriefState,
    RosterNotice,
    RosterRowNotice,
    SessionNotice,
)
from gpt_voicecoding.seams.control_plane import Action
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget


class NewestState(StrEnum):
    """Whether the newest assistant message is here, and if not, why not."""

    #: It is here, whole.
    SAID = "said"
    #: The source answered and the Session has said nothing yet.
    NOTHING_SAID = "nothing_said"
    #: Nobody looked. Not the same fact as having looked and failed.
    NOT_READ = "not_read"
    #: Somebody looked and the source could not be read.
    UNREADABLE = "unreadable"
    #: It exists and is too large to carry whole — and text is never sliced.
    OVERSIZE = "oversize"


#: The omission wording, in one table. Every surface that says why a message is
#: absent says it in these words: the same sentence in two renderers is the same
#: sentence only until one of them is edited.
NEWEST_WORDING: Mapping[NewestState, str] = {
    NewestState.NOTHING_SAID: "nothing said yet",
    NewestState.NOT_READ: "not read",
    NewestState.UNREADABLE: "could not be read",
    NewestState.OVERSIZE: "the newest entry is too large to carry",
}

#: How each state is said. The three spoken states are #165 Q1's own words, and
#: `waiting on {name}` is #320's — the one entry that is a **template**, because
#: it is the one state whose word names somebody. Core fills it (`state_word`);
#: no surface ever sees the braces.
STATE_WORDING: Mapping[BriefState, str] = {
    BriefState.DECISION: "waiting for your decision",
    BriefState.PERMISSION: "requesting permission",
    BriefState.WAITING_ON: "waiting on {name}",
    BriefState.FINISHED: "finished",
    BriefState.RUNNING: "running",
}

#: The two things `{name}` is filled with when no one party can be named, side
#: by side because they are one decision: every user-facing word lives in this
#: module (ADR 0021 §5), and a filler invented at a call site would be a second
#: vocabulary.
#:
#: `BACKGROUND_COMMAND` is the child that has no name to give — the lane carries
#: `awaiting=None` for it, and naming it from its command line is refused (#320:
#: a command line is not a name, and it is not the user's word for anything).
#: `SOMEBODY_ELSE` is the counts line, which groups Sessions by state and so has
#: no single party to name at all.
BACKGROUND_COMMAND: Final = "a background command"
SOMEBODY_ELSE: Final = "somebody else"


def state_word(state: BriefState, awaited: str | None = None) -> str:
    """One state, in the words the user is told — with `{name}` filled.

    **One filler, one place.** Five of the six readers of `STATE_WORDING` in
    this module have a party to name and one does not, and every one of them
    goes through here: a template rendered at a call site is a template one call
    site will forget to render, and `waiting on {name}` printed literally is the
    engine showing the user its own plumbing.
    """
    if state is not BriefState.WAITING_ON:
        return STATE_WORDING[state]
    return STATE_WORDING[state].format(name=awaited if awaited else BACKGROUND_COMMAND)


class NoticeWord(StrEnum):
    """The fixed words a notice needs that are not a state: keys into `NOTICE_WORDING`."""

    #: The state line of a notice whose decision has closed (ADR 0021 §8).
    HANDLED = "handled"
    #: The one line a Session's end is announced with (ADR 0021 §9).
    ENDED = "ended"
    #: The two labels a permission notice offers (ADR 0021 §6).
    ALLOW = "allow"
    DENY = "deny"
    #: What ends a folded original that a surface had to cut (ADR 0021 §5).
    TRUNCATED = "truncated"


#: The words themselves. **All English** (Simon, 2026-09-06): one table, chosen
#: here, laid out by every adapter as it stands — no per-surface language and no
#: adapter-held translation, which would be the second vocabulary ADR 0021 §5
#: exists to prevent. The Voice re-renders them in speech; Telegram prints them.
NOTICE_WORDING: Mapping[NoticeWord, str] = {
    NoticeWord.HANDLED: "handled",
    NoticeWord.ENDED: "ended",
    NoticeWord.ALLOW: "allow",
    NoticeWord.DENY: "deny",
    NoticeWord.TRUNCATED: "… cut here; the rest is on the terminal",
}

#: The prompt a surface shows when the user chose to say something to one
#: Session (ADR 0021 §6, the `send message` screen). A template, because the
#: name goes inside the sentence; `say_to` fills it.
SAY_TO_TEMPLATE = "Say to {name}:"


def say_to(name: str) -> str:
    """The `Say to <name>:` prompt, in this module's words."""
    return SAY_TO_TEMPLATE.format(name=name)


#: The one line a Session's end is announced with (ADR 0021 §9, #266). A
#: template rather than a `BriefState`: `ended` is a wording entry beside the
#: state words, because a brief describes a Session on the roster and this one
#: has left it. The symbol is in the words here rather than lit by a surface,
#: for the same reason: this line is text and never a notice, so no adapter has
#: a layout to light it from.
ENDED_LINE_TEMPLATE = "⚫ {ended} · {agent} · {name}"


def ended_line(session: Session) -> str:
    """`⚫ ended · <agent> · <name>` — what the user is told when a Session is gone.

    The name is `spoken_name`'s, so the Session is called here what every other
    surface calls it, and the agent stands before it because away from the Mac
    "which one of them" is the question this line answers.
    """
    return ENDED_LINE_TEMPLATE.format(
        ended=NOTICE_WORDING[NoticeWord.ENDED],
        agent=session.target.agent,
        name=spoken_name(session),
    )


class MenuWord(StrEnum):
    """The fixed words the menu screens need (ADR 0021 §6, #264): keys into `MENU_WORDING`."""

    #: The three choices a greeting offers about one Session.
    BRIEF = "brief"
    HISTORY = "history"
    SEND_MESSAGE = "send_message"
    #: The three choices the config screen offers.
    SWITCH = "switch"
    VERIFY = "verify"
    LIVE = "live"
    #: What the config screen and the switch screen are headed with.
    CONFIG = "config"
    SWITCHES = "switches"
    #: The two states a switch label carries.
    ON = "on"
    OFF = "off"


#: The words themselves, held here for the reason `NOTICE_WORDING` is: a menu
#: label is a sentence the user reads and presses, so it is Core's to choose,
#: and every surface prints the same one.
#:
#: **The six that are also control-plane verbs are read off `Action`**, not
#: typed again here. A label the user presses and the command they could have
#: typed instead are one word (ADR 0021 §6), and a literal would be a second
#: spelling free to drift: rename the action and the parser, `USAGE` and every
#: `/` follow it, while a hand-written label would keep offering a word the
#: command line no longer accepts. The seam is where the hub may read them
#: from (`tests/test_architecture.py`). The rest are this module's own words.
MENU_WORDING: Mapping[MenuWord, str] = {
    MenuWord.BRIEF: str(Action.BRIEF),
    MenuWord.HISTORY: str(Action.HISTORY),
    MenuWord.SEND_MESSAGE: "send message",
    MenuWord.SWITCH: str(Action.SWITCH),
    MenuWord.VERIFY: str(Action.VERIFY),
    MenuWord.LIVE: str(Action.LIVE),
    MenuWord.CONFIG: str(Action.CONFIG),
    MenuWord.SWITCHES: "switches — press one to flip it",
    MenuWord.ON: "on",
    MenuWord.OFF: "off",
}

#: One switch's label: its name and the state it holds now. A press means
#: "flip", whatever the label still says (ADR 0021 §6).
SWITCH_LABEL_TEMPLATE = "{name}: {state}"


def switch_label(name: str, on: bool) -> str:
    """`<name>: on|off`, in this module's words."""
    return SWITCH_LABEL_TEMPLATE.format(
        name=name, state=MENU_WORDING[MenuWord.ON if on else MenuWord.OFF]
    )


#: How two roster labels that would read alike are told apart: the address,
#: after the name (ADR 0021 §6, #264). The label is still resolved by position;
#: the suffix is for the user's eyes, not for Core's.
DISAMBIGUATED_LABEL_TEMPLATE = "{name} ({address})"

#: What the `sessions` screen says when there is no live Session to list — text
#: only, no Anchor, because a screen with nothing to pick is not a screen.
NOTHING_RUNNING_HINT = "nothing is running — start a Session at the terminal and it appears here"


#: What the user hears back when their message carried no text at all — a voice
#: note, a photo, a file. The Companion Channel is text only this iteration (ADR
#: 0021 §4): the adapter raises such a message as empty text, and the router's
#: cannot-classify path answers with these words. One hint, worded here and
#: nowhere else, so every surface that cannot read a message says so the same way.
NON_TEXT_HINT = "I can only read text here — say it in words"

#: The three fixed refusals of the reply grammar (ADR 0021 §2, §3; #263). Each
#: is one hint, worded here and nowhere else, and never re-targets: a numeral
#: that cannot be resolved is refused, not guessed onto another message.
#:
#: A numeral on a message Core no longer holds a row for — sent before a
#: restart, fallen out of the Anchor Table, or the user's own message — and a
#: numeral typed with no reply at all. Core does not know what that message's
#: options were, and reading the number against the newest notice would pick a
#: different message's option (review finding, 2026-09-06).
NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT = (
    "I no longer know which message that number answers — reply to the notice you mean, "
    "or say it in words"
)
#: A numeral on a message that offers nothing to pick by number: a receipt, a
#: prompt, an answer, or a number past the last option.
NUMERAL_PICKS_NOTHING_HINT = "that number picks none of that message's options — say it in words"
#: A numeral on a question that is no longer answerable from here: it was
#: answered at the terminal, or the dialog closed. Nothing is relayed.
QUESTION_ALREADY_ANSWERED_HINT = (
    "that question was already answered on screen — if you want to add something, say it in words"
)
#: A verdict on a permission whose dialog no live row carries any more: it was
#: answered at the terminal, or its hook ended. A dialog is not answered in
#: words, so the hint sends the user to the screen rather than asking for any.
PERMISSION_ALREADY_SETTLED_HINT = (
    "that permission was already settled on screen — if it is still showing, answer it there"
)


#: The Assistant Conversation's own fixed words (ADR 0021 §7, #265). Core's,
#: like every other sentence the user reads: the conversation's *answers* are
#: the coding model's words, and these three are not — they are what the surface
#: says around them, and a model never writes them.
#:
#: The line one conversation opens with. Sent as the first message of the
#: conversation and registered as its Anchor, so a reply to it is the first
#: turn. It says how the conversation is continued, because replying is the
#: whole mechanism and nothing else on this surface teaches it.
ASSISTANT_OPENING_LINE = (
    "the assistant is listening — reply to this message, or to any answer, and it remembers "
    "the conversation"
)
#: What the reply bar shows while it waits, on a surface that has one. A
#: courtesy and never the routing (ADR 0021 §7), and inside the seam's own
#: `PLACEHOLDER_LIMIT`.
ASSISTANT_REPLY_PLACEHOLDER = "your words for the assistant"

#: Binding confirms reach; saving configuration and restarting belong to the surface.
TELEGRAM_BINDING_CONFIRMATION = "Telegram is ready to connect to GPT-VoiceCoding."

#: The same, for the `Say to <name>:` prompt (#264). The heading already names
#: the Session, so the bar says only what it is waiting for.
SAY_TO_PLACEHOLDER = "your words for that session"
#: A reply to a conversation the coding model no longer has the thread for.
#: There is nothing to continue, so the hint sends the user back to the one
#: place a conversation is opened.
#:
#: **Not the expired-Anchor case**, which is the other way a conversation can
#: go out of reach and is deliberately answered differently (#265, #263): a
#: reply to an Anchor that fell out past the cap is words with an *unknown*
#: Anchor, and words with an unknown Anchor go to the newest Anchor, whatever
#: it is. Core no longer holds the row, so it cannot know the message was ever
#: a conversation's to say this about it.
ASSISTANT_CONVERSATION_GONE_HINT = (
    "that conversation has ended — open a new one from the menu and it starts fresh"
)
#: Answers a menu press when this engine has no assistant to open a conversation
#: with: the Call seam behind it could not start a thread. Its own words rather
#: than the wire's, because what the user needs is the fact, not the failure.
ASSISTANT_UNAVAILABLE_HINT = "I could not open a conversation with the assistant just now"


@dataclass(frozen=True, slots=True)
class Newest:
    """The newest assistant message, whole — or the named reason it is not here."""

    state: NewestState
    text: str | None = None
    #: What the source said when it could not be read, when it said anything.
    #: Carried so an UNREADABLE brief can name *which* failure it hit — "no
    #: registry record for its pid", "its transcript could not be found by
    #: session id" — rather than the bare "could not be read" that sends a user
    #: to the engine log to find out what happened (#278). Absent wherever the
    #: source gave no sentence, which leaves the wording exactly as it was.
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.state is NewestState.SAID) != (self.text is not None):
            raise ValueError("a newest message is either carried whole or named as absent")
        if self.reason is not None and self.state is not NewestState.UNREADABLE:
            raise ValueError("only a message nobody could read carries the reason it could not")

    @property
    def words(self) -> str:
        """What a renderer prints for it — the message, or why it is missing.

        The one rendering point, so a reason reaches every surface that prints a
        newest message at once. `NEWEST_WORDING` still supplies the clause; the
        reason extends it rather than replacing it, because "could not be read"
        is what the state means and the sentence after it is which time.
        """
        if self.text is not None:
            return self.text
        words = NEWEST_WORDING[self.state]
        return f"{words}: {self.reason}" if self.reason is not None else words


@dataclass(frozen=True, slots=True)
class BriefOption:
    """One answer a Session offered, as the user will hear it."""

    text: str
    description: str | None = None
    recommended: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """What the Session is waiting on, whole.

    Two shapes in one type, because a brief carries exactly one of them and a
    consumer branches on the `BriefState` it came with: a question carries
    `prompt`, `options` and `recommendation`; a permission carries `tool` and a
    one-line `summary`.
    """

    prompt: str | None = None
    options: tuple[BriefOption, ...] = ()
    recommendation: str | None = None
    tool: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class SessionBrief:
    """What the system knows about one Session, structured for telling the user."""

    target: SessionTarget
    name: SessionName | None
    agent: AgentKind
    state: BriefState
    #: Who this Session is waiting on, as the user calls them — the awaited
    #: Session's own Session Name, or the awaited child's name. `None` on every
    #: state but `WAITING_ON`, and on a `WAITING_ON` whose party has no name of
    #: its own, which `state_word` says in the wording table's words (#320).
    awaited: str | None
    newest: Newest
    #: `None` when nothing is being asked — a running or finished Session.
    decision: Decision | None
    #: Whether the user can answer this from here, rather than at the terminal.
    answerable_here: bool
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class RosterRow:
    """One header row: name, agent, state — and the address to ask about it by."""

    target: SessionTarget
    name: SessionName | None
    agent: AgentKind
    state: BriefState
    #: Who this row is waiting on, as `SessionBrief.awaited` carries it. The
    #: header row says the same word as the whole brief, so it is resolved once
    #: and carried, never re-derived by a renderer.
    awaited: str | None = None
    #: Whether this is the Focus Session. Exactly one row may carry it.
    focus: bool = False
    newest: str | None = None
    last_activity_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RosterBrief:
    """How many Sessions are in each state, and one header row for each.

    Focus is call-side metadata; neither the counts nor row order depend on it.
    """

    counts: Mapping[BriefState, int]
    rows: tuple[RosterRow, ...]
    focus: SessionTarget | None = None


# ----------------------------------------------------------------------
# The three verbs.
# ----------------------------------------------------------------------


def roster(sessions: Sequence[Session], focus: SessionTarget | None) -> RosterBrief:
    """Counts per state and one header row per live Session, newest activity first.

    **Exited Sessions appear nowhere** (#165 Q7), and neither does a Child
    Process. Two reasons, and they are the same one: every row here is one the
    model may ask `brief <address>` about, and a child is refused as a target;
    and `CONTEXT.md`'s *Child Process* is a row that gets "no Relay, no Stop
    Notice, no name" — a Roster Brief is what the user is *told*, and a child is
    seen rather than spoken about (#68). The registry still lists it and
    `status` still carries it, which is where "appears in the roster" is true.
    The menu-bar panel already counts the user-facing roster this way
    (`shell/Sources/ShellCore/ControlPanel.swift:44`).

    **And neither does a Headless Run** (#319, ADR 0021 §9 as amended). The same
    gate and the same sentence: a row here is one the user may ask about and
    reply to, and a run with no controlling terminal is neither. `/sessions`
    omits it by consuming this brief (`core/menu.py::roster_screen`), so the
    rule is stated once rather than once per surface.
    """
    rows = tuple(
        _row(session, focus=session.target == focus, peers=sessions)
        for session in sessions
        if session.is_addressable
    )
    ordered = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.last_activity_at.timestamp()
                if row.last_activity_at is not None
                else float("-inf")
            ),
            reverse=True,
        )
    )
    counts: dict[BriefState, int] = {}
    for row in ordered:
        counts[row.state] = counts.get(row.state, 0) + 1
    return RosterBrief(
        counts=counts,
        rows=ordered,
        focus=next((row.target for row in ordered if row.focus), None),
    )


def session(
    session: Session,
    *,
    question_answerable: bool = False,
    peers: Sequence[Session] = (),
) -> SessionBrief:
    """One Session, briefed from the row as it stands.

    `question_answerable` is the one fact a row cannot carry: whether the lane
    still holds the exact prompt and can route the next Answer Relay into it
    (`seams/agent.py::derive_reply_window`). It is a live adapter reading, so
    the hub passes it in rather than this module inventing it — the default is
    the safe one, because a question announced as answerable from here and
    answerable only at the terminal is worse than no announcement at all.

    `peers` is the roster this Session sits in, and it is needed for one word:
    a Session waiting on another is announced by **that** Session's Session Name
    (#320), which no single row carries. Handed in rather than reached for,
    because this module is a function of what it is given; and read here rather
    than stored at the Stop, because a Session Name climbs (ADR 0024) and a name
    resolved minutes ago would be announced stale. An empty roster is the honest
    default: an unknown peer gets the same generic label as a Session whose
    project has not been observed, never its transport address.
    """
    state = _state(session)
    return SessionBrief(
        target=session.target,
        name=session.name,
        agent=session.target.agent,
        state=state,
        awaited=_awaited(session, peers),
        newest=_newest(session.progress),
        decision=_decision(session),
        answerable_here=_answerable_here(session, question_answerable=question_answerable),
        last_activity_at=session.last_activity,
    )


def brief_state(session: Session) -> BriefState:
    """Which of the five states this row is in, read the way every brief reads it.

    Public for the one caller outside a brief that needs the user's five words
    rather than the lane's lifecycle: the Relay pipeline, deciding whether the
    words it carried answered a question the Session merely *said* — a Codex
    turn that ended asking, which no `WaitingFor` records (ADR 0013's
    amendment, `core/relays.py::RelayAuthority`). One reading, so the receipt
    and the notice cannot disagree about whether there was a question.
    """
    return _state(session)


def earns_a_brief(session: Session) -> bool:
    """Whether this Session has anything the user is owed a whole brief about.

    **One rule, in one place**, because a hand-over and the mid-call word have
    to agree about it: a call that briefed one set of Sessions and spoke about
    another would be two answers to one question (#209, #213).

    One way to earn one: a row that has **stopped** is asking something of the
    user. A running Session is in the roster and asking nobody anything, so it
    gets its header row and nothing more.

    A row carrying an **undelivered Relay** used to earn one too (#197), on a
    field only the Relay ceiling ever wrote. The ceiling is abolished (#321), so
    nothing can write it and the field is gone with it: the user's words now
    wait for the Session's turn rather than being dropped, and what became of
    them is said on the message they sent (ADR 0021, *a receipt is a reaction*).
    """
    return _state(session) is not BriefState.RUNNING


def omitting_newest(brief: SessionBrief) -> SessionBrief:
    """The same brief with its newest message named as too large to carry.

    ADR 0016's rule, applied where the wire is measured: an entry that will not
    fit is **named as omitted and never sliced**, so the user is told the
    message exists and could not be carried rather than handed half of it. The
    header, the state and the whole decision stay — they are what the user acts
    on, and they are small.

    Here rather than in the publisher because the wording is Briefing's: this is
    the one place that puts words to a message that is absent.
    """
    return replace(brief, newest=Newest(state=NewestState.OVERSIZE))


def spoken(brief: SessionBrief) -> SpokenBrief:
    """One Session Brief as the Call seam carries it, in this module's own words.

    The Core type may not cross a seam (ADR 0001), so what crosses is the seam's
    `SpokenBrief` — and it is filled with the wording tables above rather than
    with raw values, so that the Voice hears the same five state words the
    Companion Channel and the log print. An adapter downstream assembles these
    strings into a wire item and chooses none of them.

    The address does not travel. A `SessionTarget` is how *this* process names a
    Session; the Voice names it the way the user does, which is the Session Name
    where there is one and a generic Session label where there is not
    (`core/sessions.py::spoken_name`, the same rule `_headline` follows).
    """
    return SpokenBrief(
        name=brief.name if brief.name is not None else UNNAMED_SESSION,
        agent=str(brief.agent),
        state=state_word(brief.state, brief.awaited),
        newest=brief.newest.words,
        decision=tuple(line.strip() for line in _decision_lines(brief)),
        answerable_here=_answer_wording(brief.answerable_here),
        last_activity_at=_when(brief.last_activity_at),
    )


def notice(brief: SessionBrief) -> SessionNotice:
    """One Session Brief as the Companion Channel seam carries it (ADR 0021 §5).

    The `spoken` rule on the other surface: a Core type may not cross a seam,
    so the seam's carrier crosses, and every string in it is one this module
    worded. What the channel needs that the Voice does not is the closed state
    (to light a symbol, which is layout) and the option labels as a tuple in
    order, because a numeral picks one by position (§3) and a surface that can
    draw buttons draws one per label (§6).

    A permission's question slot is the same line `text` prints for it, and its
    labels are the table's `allow` / `deny` (§6): numeral 1 resolves to `allow`
    on every surface because the labels are Core's. A notice that asks nothing
    has an empty question and no labels; the layout is the same with the slot
    empty. `newest` is carried **whole** — cutting it to a surface's limit is
    that surface's layout act, marked with the table's words where it happens.
    """
    question, options, recommendation = _asked(brief)
    return SessionNotice(
        state=brief.state,
        state_word=state_word(brief.state, brief.awaited),
        agent=str(brief.agent),
        name=brief.name if brief.name is not None else UNNAMED_SESSION,
        question=question,
        options=options,
        recommendation=recommendation,
        newest=brief.newest.words,
        cut_marker=NOTICE_WORDING[NoticeWord.TRUNCATED],
        answerable_here=brief.answerable_here,
        answer_wording=f"answer {_answer_wording(brief.answerable_here)}",
    )


def roster_notice(brief: RosterBrief) -> RosterNotice:
    """The Roster Brief as the Companion Channel seam carries it — `text`'s rows, as data.

    Rows in `text`'s activity order, and the same counts line on every surface.
    """
    return RosterNotice(
        rows=tuple(
            RosterRowNotice(
                state=row.state,
                state_word=state_word(row.state, row.awaited),
                agent=str(row.agent),
                name=row.name if row.name is not None else UNNAMED_SESSION,
            )
            for row in brief.rows
        ),
        counts=_counts_line(brief),
    )


def for_call(
    sessions: Sequence[Session],
    focus: SessionTarget | None,
    *,
    occasion: Occasion = Occasion.HANDOVER,
    answerable: Collection[SessionTarget] = (),
) -> tuple[HandoverItem, ...]:
    """Read the current roster in the shape this occasion needs (#274).

    Hand-over holds counts, header rows and every waiting Session's brief.
    Opening carries one brief alone for one waiting Session; with several it
    carries the waiting Focus Session first, then the Roster Brief, or only the
    roster without a waiting Focus. Mid-call carries only the waiting Focus.
    No waiting Sessions means no answer.

    Fitting gives up newest bodies from the back (with omission words), then
    header rows, then whole briefs. Counts survive whenever a roster is part of
    the answer. Selection precedes fitting so unrelated bodies cannot consume
    the opening's or mid-call Focus brief's budget.
    """
    summary = roster(sessions, focus)
    by_target = {live.target: live for live in sessions}
    briefs = [
        session(
            by_target[row.target],
            question_answerable=row.target in answerable,
            peers=sessions,
        )
        for row in summary.rows
        if row.target in by_target and earns_a_brief(by_target[row.target])
    ]
    # Focus decides who is spoken to first, never the roster's row order (#359).
    briefs.sort(key=lambda brief: brief.target != focus)
    if not briefs:
        return ()
    if occasion is Occasion.MID_CALL:
        briefs = [brief for brief in briefs if brief.target == focus]
        summary = None
    elif occasion is Occasion.OPENING:
        if len(briefs) == 1:
            summary = None
        else:
            briefs = [brief for brief in briefs if brief.target == focus]
    return _fitted(summary, briefs, roster_first=occasion is Occasion.HANDOVER)


def text(brief: SessionBrief | RosterBrief) -> str:
    """The one rendering of a brief: labelled lines carrying every field.

    Nothing is dropped for brevity. The Voice condenses what it is given; a
    channel and a log show the whole thing, and an engine that shortened here
    would take that choice away from both.
    """
    if isinstance(brief, RosterBrief):
        return "\n".join(_roster_lines(brief))
    return "\n".join(_session_lines(brief))


# ----------------------------------------------------------------------
# Reading one row.
# ----------------------------------------------------------------------


def _row(session: Session, *, focus: bool, peers: Sequence[Session] = ()) -> RosterRow:
    newest = _newest(session.progress).text
    return RosterRow(
        target=session.target,
        name=session.name,
        agent=session.target.agent,
        state=_state(session),
        awaited=_awaited(session, peers),
        focus=focus,
        newest=newest.split("\n", 1)[0] if newest is not None else None,
        last_activity_at=session.last_activity,
    )


def _awaited(session: Session, peers: Sequence[Session]) -> str | None:
    """Who this Session is waiting on, in the user's own word for them (#320).

    **The lane names the party and Core names it to the user**, which is the
    same split every other word here follows. `WaitingFor.awaiting` is the
    adapter's reference: a peer Session's address as this process spells one,
    or a child's own name. This turns the first into the Session Name the user
    calls that Session, by finding it on the roster it was handed.

    A peer the roster does not hold gets a generic Session label, the same
    answer this gives a Session whose project has not been observed (#359).
    A `WAITING_ON` with nothing at all to name is a child that has none, and
    `state_word` is where that becomes words.
    """
    awaiting = session.waiting_for.awaiting
    if session.waiting_for.kind is not WaitingKind.PEER:
        # A child's name is already the name; nothing on the roster names it,
        # because a Child Process is never named there (#78).
        return awaiting
    if awaiting is None:
        return None
    named = next((peer for peer in peers if str(peer.target) == awaiting), None)
    return spoken_name(named) if named is not None else UNNAMED_SESSION


def _state(session: Session) -> BriefState:
    """The five states, read off one row.

    Order matters. A **running** Session stays RUNNING however its progress
    read went: the state is the lifecycle's, and the read only fills the fields
    — a Session that is working is not a Session that stopped on something.
    Everything else has stopped.

    **A stop nobody could read is no longer a state of its own** (#320). It was
    UNREADABLE, ahead of everything (#166 B7); the word is retired, and what
    replaces it is not a different guess but a smaller claim. A read that failed
    and a wait nobody could classify both fall through to the text pass, which
    finds no question and answers FINISHED — while `ProgressAvailability` and
    `NewestState` still carry *why* into the body's omission sentence
    (`NEWEST_WORDING`, unchanged). The user reads "finished · could not be read"
    instead of a bare "unreadable", and never reads "waiting for your decision"
    for a Session nobody asked anything of.

    **PEER and CHILD are one word to the user, and the difference is only who**
    (ADR 0021 as amended): the ball is somebody else's, and the user's action —
    none — is the same for a peer Session and for a background command.
    """
    if session.state is SessionState.RUNNING:
        return BriefState.RUNNING
    match session.waiting_for.kind:
        case WaitingKind.QUESTION:
            return BriefState.DECISION
        case WaitingKind.PERMISSION:
            return BriefState.PERMISSION
        case WaitingKind.PEER | WaitingKind.CHILD:
            return BriefState.WAITING_ON
        case _:
            return _turn_ended(session)


def _turn_ended(session: Session) -> BriefState:
    """A Session that stopped and is waiting on nothing this reader can name.

    **The default is reversed, and one deterministic rule decides** (#320, ADR
    0021 as amended). #166 B2 made DECISION the default and `_asking` a
    promotion gate out of it; the 2026-09-09 corpus measured what that cost —
    an unread newest message and a hand-over that asked nothing were both
    announced as *waiting for your decision*, so the light the user was told to
    act on was the light that meant the engine could not tell. Now the evidence
    runs the other way: DECISION is claimed only where `_asking` finds a
    question, and everything else — including `answer is None`, which is a turn
    nobody read — is FINISHED. A Session that really is asking still has its
    structured question, which `_state` settled before reaching here.

    Structured questions and permissions took precedence in `_state`; this text
    pass chooses state only, never a decision's contents. Adapted from Codex's
    #166/#188 rule; legacy's unconditional finished wording
    (`legacy@1d32845:bridge/host.py:226-234`) does not settle a prose question
    either, because a question mark still promotes.
    """
    answer = _final_answer(session.progress, phased=session.target.agent is AgentKind.CODEX)
    if answer is not None and _asking(answer):
        return BriefState.DECISION
    return BriefState.FINISHED


# ----------------------------------------------------------------------
# Did the stopped turn end on a question? (#188, on the evidence in #176.)
# ----------------------------------------------------------------------

#: What the user was shown, once what they were not is taken out: a fenced code
#: block, an inline code span, and the target half of a markdown link (the label
#: is kept, because the label is words they read). Measured: stripping code
#: spans alone removes the one false positive a raw match makes, a `done` answer
#: containing the literal `?? uv.lock` inside backticks (#176 §5, A → B).
_FENCED: Final = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_CODE_SPAN: Final = re.compile(r"`+[^`]*`+")
#: One level of balanced parentheses inside the target, because a URL may hold
#: a pair — `…/path_(part)?run=7` — and a target cut at the first `)` leaves its
#: query string standing in the prose, where `?` reads as an ask.
_LINK: Final = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_AUTOLINK: Final = re.compile(r"<[a-z][a-z0-9+.-]*:[^>\s]*>", re.IGNORECASE)

#: An interrogative in either width. The corpus is 98% Chinese, so `？` is not a
#: rare spelling here; the English behaviour of this rule is **uncertain**
#: (#176 §1.2) and errs toward DECISION, which is the cheap direction.
_ASKS: Final = re.compile(r"[?？]")


def _final_answer(progress: ProgressObservation, *, phased: bool = True) -> str | None:
    """The newest message the source marked as this turn's answer, if it did.

    **The search stops at the turn it is about.** A progress tail holds several
    turns, and an answer found behind the newest one is the *previous* turn's:
    reading it would brief a turn that is still working, or one that has
    produced only commentary, on words it never said. The newest turn is the
    turn of the newest entry, by the `turn_id` the lane put on it (#210), and
    only entries naming that turn are searched. That boundary is the source's
    own and survives a turn opened by a message with no words in it — a
    `userMessage` carrying only an image leaves no entry at all
    (`adapters/agent/codex/thread_tail.py::_entry`, and the seam refuses an
    entry with nothing said in it), so a turn opened by one that has produced
    only commentary is a DECISION rather than the previous turn's FINISHED.

    **A reading that names no turn keeps the rule it had before.** The Claude
    lane has no turn in a transcript file, and a Codex build may name none, so
    when the newest entry carries no `turn_id` the search stops at the newest
    `USER` entry instead — the boundary that is in the tail already, as the
    entry the user put there. Nothing regresses on a reading without turns; it
    is only the wordless opener that reads past it, which is the case the
    `turn_id` closes.

    Nothing is classified without an answer. For Codex, every way of not having
    one stays DECISION: a build old enough to mark no `phase`, a turn that has said
    nothing yet, and a turn whose only message so far is `commentary`. A
    `commentary` newest with this turn's answer behind it is not one of them —
    the answer is what is classified (3 of 669 turns, #176 §2.1) — and reading
    the commentary instead would manufacture a decision out of the mid-turn
    question Codex's own prompt permits there.
    """
    newest_turn = progress.recent[-1].turn_id if progress.recent else None
    for entry in reversed(progress.recent):
        if newest_turn is None:
            if entry.role is ProgressRole.USER:
                return None
        elif entry.turn_id != newest_turn:
            return None
        # Claude's transcript has no phase field. Its newest assistant message
        # in this turn is the answer; Codex retains its own final-answer rule.
        if entry.role is ProgressRole.ASSISTANT and (
            not phased or entry.phase is ProgressPhase.FINAL_ANSWER
        ):
            return entry.text
    return None


def _asking(answer: str) -> bool:
    """Whether a final answer shows the user is being asked something.

    **A question mark makes a decision; nothing else does** (#320, ADR 0021 as
    amended 2026-09-09). This was #176 §5's heuristic C, and its two other
    clauses — a lettered `A)` / `B)` option block, and a named `选项 X` /
    `方案 X` — are **deleted**. They were a promotion gate out of a DECISION
    default, where over-reading cost nothing; `_turn_ended` no longer defaults
    that way, so every clause here now *creates* a 🟡 the user is told to act
    on, and the same shape written twice must not get two words. A turn that
    lays out lettered options and asks nothing is a report of what it did, and
    that is 🟢. The tuned phrase list that scored higher on #176's sample stays
    **not adopted**, for its own reason: it was fitted after reading that
    sample's misses, and its number is not an estimate of anything (#176 §5, D).

    What survives is the interrogative in either width, which is a mark the
    writer put there on purpose rather than a shape a reader inferred.

    Legacy classified nothing here: `legacy@1d32845:bridge/transcript.py:431-454`
    returned `pending_question=None` for every Codex stop, on the ground that
    "reporting nothing is the honest answer". **Dropped, because** the choice is
    not between guessing and silence but between claiming a decision and
    claiming one on evidence.
    """
    prose = _AUTOLINK.sub(" ", _CODE_SPAN.sub(" ", _LINK.sub(r"\1", _FENCED.sub(" ", answer))))
    return bool(_ASKS.search(prose))


def _newest(progress: ProgressObservation) -> Newest:
    """The newest assistant message whole, under ADR 0016's omission rules."""
    if progress.availability is ProgressAvailability.NOT_READ:
        return Newest(state=NewestState.NOT_READ)
    if progress.availability is ProgressAvailability.UNREADABLE:
        return Newest(state=NewestState.UNREADABLE, reason=progress.reason)
    if progress.has_history is False:
        return Newest(state=NewestState.NOTHING_SAID)
    said = next(
        (entry for entry in reversed(progress.recent) if entry.role is ProgressRole.ASSISTANT),
        None,
    )
    if said is not None:
        return Newest(state=NewestState.SAID, text=said.text)
    if progress.omission is ProgressOmission.NEWEST_OVERSIZE:
        return Newest(state=NewestState.OVERSIZE)
    # History exists and this publication carried none of it — the roster
    # summary's own case (`ProgressOmission.STATUS_SUMMARY`), and the case of a
    # tail whose entries are all the user's. Nobody read the message; saying so
    # is the honest answer and the one `NOT_READ` already means.
    return Newest(state=NewestState.NOT_READ)


def _decision(session: Session) -> Decision | None:
    """What it is waiting on, whole — or `None` when it is waiting on nothing.

    Carried whatever the state, because the brief keeps whatever was read: a
    partial label is worth more to the user than a blank. A `PEER` or a `CHILD`
    wait has no decision in it at all — nothing is being asked of the user — so
    it falls to the same `None` a finished turn does.
    """
    waiting_for = session.waiting_for
    match waiting_for.kind:
        case WaitingKind.QUESTION:
            return Decision(
                prompt=waiting_for.prompt,
                options=tuple(
                    BriefOption(
                        text=option.text,
                        description=option.description,
                        recommended=option.recommended,
                    )
                    for option in waiting_for.options
                ),
                recommendation=waiting_for.recommendation,
            )
        case WaitingKind.PERMISSION:
            return Decision(tool=waiting_for.tool_name, summary=waiting_for.detail)
        case _:
            return None


def _answerable_here(session: Session, *, question_answerable: bool) -> bool:
    """Whether the user's reply can reach this Session from here.

    Two routes, and the second is not a Reply Window: a permission is answered
    by the Approval Relay, and only while the adapter still holds the handle the
    dialog is parked on. A permission whose handle is gone was handed back to
    the terminal, and the user is owed that fact rather than a notice they would
    try to answer and could not.
    """
    if not session.is_live or not session.child.is_main:
        # An ended Session accepts nothing, and a Child Process is seen and
        # never spoken to (#68). Said here rather than left to the window
        # derivation, because the permission route below does not go through it.
        return False
    if (
        session.waiting_for.kind is WaitingKind.PERMISSION
        and session.waiting_for.approval_id is not None
    ):
        return True
    window = derive_reply_window(
        session.state,
        session.waiting_for,
        session.child,
        question_answerable=question_answerable,
    )
    return window is ReplyWindow.OPEN


# ----------------------------------------------------------------------
# Fitting a hand-over into the wire's two ceilings.
# ----------------------------------------------------------------------


def _fitted(
    summary: RosterBrief | None,
    briefs: list[SessionBrief],
    *,
    roster_first: bool,
) -> tuple[HandoverItem, ...]:
    """Give things back, in the order the docstring of `for_call` names, until it fits.

    Three rungs, in the order `for_call` states — every newest body from the back,
    then the roster's header rows from the back, then whole briefs from the back.
    The loop stops at the first arrangement that fits, so a hand-over that already
    does is returned untouched: the common case, and the one where every body is
    carried whole.

    A header row is given up before a brief because it is the only thing here
    that repeats something already said. A brief is given up last because it is
    the only thing that carries a decision.

    **The counts are never given up** when a roster is carried: they still say how
    many Sessions the call could not carry (ADR 0016).
    """
    carried = list(briefs)
    rows = list(summary.rows) if summary is not None else []
    while True:
        items = _handover_items(summary, rows, carried, roster_first=roster_first)
        if _within_ceilings(items):
            return items
        if _one_body_less(carried):
            continue
        if _one_row_less(rows, carried):
            continue
        if carried:
            carried.pop()
            continue
        # Only the counts remain, bounded by their own writer.
        return items


def _one_row_less(rows: list[RosterRow], briefs: list[SessionBrief]) -> bool:
    """Give up one header row, the ones a brief already names first.

    From the back within each group, so the most recent row is the last to
    go — and a row whose Session is briefed goes before any row whose Session is
    not, because the brief says everything the row does and more.
    """
    if not rows:
        return False
    briefed = {brief.target for brief in briefs}
    for index in reversed(range(len(rows))):
        if rows[index].target in briefed:
            rows.pop(index)
            return True
    rows.pop()
    return True


def _handover_items(
    summary: RosterBrief | None,
    rows: list[RosterRow],
    briefs: list[SessionBrief],
    *,
    roster_first: bool,
) -> tuple[HandoverItem, ...]:
    spoken_briefs = tuple(spoken(brief) for brief in briefs)
    roster_items = (_spoken_roster(summary, rows),) if summary is not None else ()
    return roster_items + spoken_briefs if roster_first else spoken_briefs + roster_items


def _within_ceilings(items: tuple[HandoverItem, ...]) -> bool:
    if len(items) > MAX_HANDOVER_ITEMS:
        return False
    return sum(item.size_in_bytes for item in items) <= HANDOVER_BUDGET_BYTES


def _one_body_less(briefs: list[SessionBrief]) -> bool:
    """Name the last carried newest message as omitted. False when none is left.

    From the back, because the roster ordered these by what the user is most
    likely to be asked about first, and a hand-over that gave up the Focus
    Session's message to keep the last row's would be answering the wrong
    question with the bytes it has.
    """
    for index in reversed(range(len(briefs))):
        if briefs[index].newest.text is not None:
            briefs[index] = omitting_newest(briefs[index])
            return True
    return False


def _spoken_roster(brief: RosterBrief, rows: list[RosterRow]) -> SpokenRosterBrief:
    """The Roster Brief as the Call seam carries it — the same words `text` prints."""
    return SpokenRosterBrief(
        counts=_counts_line(brief),
        rows=tuple(_row_line(row) for row in rows),
    )


# ----------------------------------------------------------------------
# The one renderer.
# ----------------------------------------------------------------------


def _session_lines(brief: SessionBrief) -> list[str]:
    lines = [_headline(brief.name, brief.target, brief.state, brief.awaited)]
    lines.append(f"  newest: {brief.newest.words}")
    lines.extend(_decision_lines(brief))
    lines.append(f"  answer: {_answer_wording(brief.answerable_here)}")
    lines.append(f"  last activity: {_when(brief.last_activity_at)}")
    return lines


def _answer_wording(answerable_here: bool) -> str:
    """Where the user answers this, in the two phrases both carriers use."""
    return "from here" if answerable_here else "at the terminal"


def _when(last_activity_at: datetime | None) -> str:
    """The last activity stamp, or the admission that nobody read one."""
    return last_activity_at.isoformat() if last_activity_at is not None else "not read"


def _decision_lines(brief: SessionBrief) -> list[str]:
    """The one line, or the several, that say what the Session is waiting on.

    **The state decides which shape this is, not the fields.** `Decision` holds
    two shapes in one type and a permission the roster named without naming its
    tool carries neither half — `WaitingFor(kind=PERMISSION)` off a roster that
    says *waiting* and nothing more (`adapters/agent/claude/waiting_labels.py`).
    Read off the fields alone that is indistinguishable from a question nobody
    could read, and it used to render as one: "asked: it asked you something"
    about a permission dialog. The tool name is then the renderer's floor, which
    is where a name nobody supplied belongs.

    A permission that reaches here under another state word — the progress read
    failed on top of the dialog — is still recognised by its half of the fields,
    and one that carries neither is the residue: it renders as an ask nobody
    could read, which is what it is.

    Legacy (ADR 0010): `legacy@1d32845:bridge/host.py:213-235`
    (`SessionStopSpeech.render`) chose "This session is waiting for permission."
    off the Hook **event kind** it was created from, never off whether any field
    about the tool had been read. **Ported** — the same rule, read off the state
    the brief carries rather than off a ledger's event kind. What gen-1 had no
    equivalent of is the tool name and its one-line summary, which this
    generation's `WaitingFor` carries and legacy's line did not: those stay the
    renderer's optional tail, and "a tool" is the floor beneath them.
    """
    decision = brief.decision
    if decision is None:
        return []
    if _is_permission(brief):
        asked = decision.tool or "a tool"
        return [f"  permission: {asked}" + (f" — {decision.summary}" if decision.summary else "")]
    lines = [f"  asked: {decision.prompt or 'it asked you something'}"]
    lines.extend(
        f"  option: {option.text}"
        + (f" — {option.description}" if option.description else "")
        + (" (recommended)" if option.recommended else "")
        for option in decision.options
    )
    if decision.recommendation:
        lines.append(f"  recommends: {decision.recommendation}")
    return lines


def _asked(brief: SessionBrief) -> tuple[str, tuple[str, ...], str]:
    """The question slot, the labels in order and the recommendation, for a notice.

    Which shape a decision is follows `_decision_lines`' rule — the state, not
    the fields — so the two renderers cannot disagree about whether a stop is a
    permission. A permission's slot is its `permission:` line and its labels are
    the table's; a question's slot is the prompt and its labels the options'
    text; a Session asking nothing has an empty slot and no labels. The
    recommendation crosses as the whole `recommends:` line `text` prints, so
    the adapter has a line to place and no word to add to it.
    """
    decision = brief.decision
    if decision is None:
        return "", (), ""
    if _is_permission(brief):
        (line,) = _decision_lines(brief)
        return (
            line.strip(),
            (NOTICE_WORDING[NoticeWord.ALLOW], NOTICE_WORDING[NoticeWord.DENY]),
            "",
        )
    return (
        decision.prompt or "it asked you something",
        tuple(option.text for option in decision.options),
        f"recommends: {decision.recommendation}" if decision.recommendation else "",
    )


def _is_permission(brief: SessionBrief) -> bool:
    """Whether the decision a brief carries is a permission — the rule `_decision_lines` states.

    The state decides, and the two permission-only fields are the fallback for
    a permission whose state word says something else. One predicate, so the
    text renderer and the channel notice cannot come to read one stop two ways.
    """
    decision = brief.decision
    return decision is not None and (
        brief.state is BriefState.PERMISSION
        or decision.tool is not None
        or decision.summary is not None
    )


def _roster_lines(brief: RosterBrief) -> list[str]:
    return [_counts_line(brief), *(f"  {_row_line(row)}" for row in brief.rows)]


def greeting(session: Session, peers: Sequence[Session] = ()) -> str:
    """The one line a menu greets one Session with: `text`'s own header for it (#264).

    The screen that offers `brief` / `history` / `send message` about a Session
    names it the way every other surface does, so the header is the headline
    `text` prints and nothing composed here.

    `peers` for the reason `session` takes it: a Session waiting on another is
    named by *that* Session's Session Name (#320), which one row cannot supply.
    """
    return _headline(session.name, session.target, _state(session), _awaited(session, peers))


def _headline(
    name: SessionName | None,
    target: SessionTarget,
    state: BriefState,
    awaited: str | None = None,
) -> str:
    """The name and the address to ask about it by are distinct fields."""
    named = str(name) if name is not None else UNNAMED_SESSION
    return f"{named} — {target} — {state_word(state, awaited)}"


def _row_line(row: RosterRow) -> str:
    return _headline(row.name, row.target, row.state, row.awaited)


def _counts_line(brief: RosterBrief) -> str:
    """Count every roster row on every surface, whether Focus or not (#359)."""
    return f"sessions: {_counts(brief.counts)}"


def _counts(counts: Mapping[BriefState, int]) -> str:
    """Every state that has any Sessions in it, in the order the states are named."""
    said = [
        f"{counts[state]} {state_word(state, SOMEBODY_ELSE)}"
        for state in BriefState
        if counts.get(state)
    ]
    return ", ".join(said) if said else "none"
