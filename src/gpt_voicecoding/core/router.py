"""The inbound-text router — what arriving text *means*.

The Companion Channel hands text up and no opinion about it: the seam's event
has no field an adapter could use to volunteer one, because classifying it is
Bridge Core's job (ADR 0001). This is that job.

**Unknown or ambiguous input fails closed with an honest reply.** Never guessed
into a command, and never guessed into the wrong Session either — a wrong guess
delivers the user's own words, carrying the user's authority, somewhere they did
not mean. A refusal costs one round trip.

Four top-level forms, and every marker and every command name is **injected**.
The command set belongs to the control-plane surface, so this module knows no
command by name and cannot grow one:

    /<command> …      a control-plane command
    ><prompt>         a Delegated Turn
    @<name>: words    the user's own words, for the Session that name names
    words             the user's own words, for the target of the newest Anchor —
                      or, with no Anchor held, for the one live Session

**And a fifth thing a message can be: a reply** (ADR 0021 §2, §3; #263). The
adapter reports which of our own messages the user replied to (`in_reply_to`),
and the Anchor Table (`core/anchors.py`) says what that message was about. A
reply to a known Anchor is for that Anchor's target, whatever it says — the
reply decided whose words these are, so the markers above are not read inside
one, with a single exception: a `/x` line inside a reply belongs to the Session
and is carried inside the words that name the act (`run the skill /x with
arguments …`, #248), with no pre-check of the name (#256). A `>` inside a reply
is the user's words. A reply to an Anchor Core no longer holds — pre-restart,
fallen out, the user's own message — is treated exactly as a message that replied
to nothing, **when it is words**.

**A numeral must reply to the message it answers** (ruling, 2026-09-06). A
numeral picks that Anchor's Nth option label: on a permission notice it is the
user's verdict on the row's approval id; on a question it is the label, sent as
an Answer Relay only while the roster still shows *that* question as the
Session's wait and the Agent seam says it is still answerable from here (ADR
0015's fact) — otherwise nothing is sent and the reply says so.
A numeral with an unknown `in_reply_to`, or with none, is refused with one fixed
hint and is never re-targeted: Core does not know what that message's options
were, and reading "2" against the newest Anchor would pick a different message's
second option. A numeral on a message that offers nothing to pick is refused the
same way. Free text is never subject to any of this.

Bare text resolving to the newest Anchor's target, or to the single live Session,
is not a guess in the forbidden sense: it classifies into the least dangerous
class, and with exactly one candidate nothing is being picked *between*. Zero or
several fails closed and asks, reusing the registry's locked "a Session Name
disambiguates or asks" rule rather than minting a second disambiguation mechanism.

The one collision worth spelling out is a bare word that is also a registered
command. It fails closed in **both** directions at top level: bare `stop` is
never promoted into the command, and `/stop` is never injected into a Session as
text. The reply offers both readings and lets the user say which. Inside a reply
there is no collision: the reply already said whose words they are.

Asking for progress is not here as a class of its own. It is a read — a
control-plane status query — and it never touches a Session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from gpt_voicecoding.core.anchors import Anchor, AnchorKind, AnchorTable
from gpt_voicecoding.core.briefing import (
    NON_TEXT_HINT,
    NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT,
    NUMERAL_PICKS_NOTHING_HINT,
    QUESTION_ALREADY_ANSWERED_HINT,
)
from gpt_voicecoding.core.errors import AmbiguousNameError, BridgeCoreError, NameMatchError
from gpt_voicecoding.core.sessions import Session, SessionRegistry, spoken_name
from gpt_voicecoding.seams.agent import ApprovalVerdict, WaitingKind
from gpt_voicecoding.seams.identity import SessionTarget

#: How a `/x` line inside a reply is carried to the Session: the words name the
#: act, because neither transport expands a leading `/` (#248). The Session
#: resolves the name itself; a wrong one costs one turn and is never refused here.
SKILL_WORDS = "run the skill {command}"
SKILL_WORDS_WITH_ARGUMENTS = "run the skill {command} with arguments: {arguments}"


class InboundClass(StrEnum):
    """What arriving text turned out to be. Five, and the fifth is a refusal."""

    #: A status query or a switch flip. Never gated by any switch (ADR 0002).
    CONTROL = "control"
    #: The user's own words for a Session, carrying the user's authority.
    ANSWER_RELAY = "answer_relay"
    #: The user's verdict on one pending permission, picked by numeral on the
    #: notice that carried it (ADR 0021 §6).
    APPROVAL_RELAY = "approval_relay"
    #: Work handed to a coding model on the user's behalf.
    DELEGATION = "delegation"
    #: Nothing could be said about it honestly. Carries the reply to send back.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextGrammar:
    """The markers and command names this router recognises. All configuration.

    Defaults are here so a test or a bare engine has a working grammar; the
    control-plane surface passes its real command set in. Nothing in this module
    may hard-code a command name — that would put half the command set here and
    half of it there.
    """

    control_prefix: str = "/"
    delegate_prefix: str = ">"
    relay_marker: str = "@"
    #: What the control plane will actually answer to. Empty means none.
    control_commands: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        markers = (self.control_prefix, self.delegate_prefix, self.relay_marker)
        for marker in markers:
            if not marker.strip():
                raise ValueError("a marker that is whitespace cannot mark anything")
        if len(set(markers)) != len(markers):
            raise ValueError(f"two forms cannot share one marker: {markers}")


@dataclass(frozen=True, slots=True)
class Classification:
    """What the text is, and what to do about it — including how to refuse."""

    kind: InboundClass
    #: The payload with its marker stripped: the command's arguments, the
    #: delegation prompt, the words to Relay, or the verdict's word.
    text: str = ""
    #: Set for CONTROL only. The verb, already known to be registered.
    command: str = ""
    #: Set for ANSWER_RELAY and APPROVAL_RELAY. The exact Session identity, never a name.
    target: SessionTarget | None = None
    #: Set for APPROVAL_RELAY only. The pending dialog's handle, from the Anchor row.
    approval_id: str = ""
    #: Set for APPROVAL_RELAY only. What the numeral resolved to.
    verdict: ApprovalVerdict | None = None
    #: Set for UNKNOWN only. What to say back — honest, and never a guess.
    reply: str = ""


def _never_answerable(target: SessionTarget) -> bool:
    """The fail-closed default when no lane is wired to say otherwise."""
    return False


class InboundRouter:
    """Classifies arriving text. Sends nothing, decides nothing else."""

    def __init__(
        self,
        *,
        sessions: SessionRegistry,
        grammar: TextGrammar | None = None,
        anchors: AnchorTable | None = None,
        answerable: Callable[[SessionTarget], bool] | None = None,
    ) -> None:
        self._sessions = sessions
        self._grammar = grammar or TextGrammar()
        #: Bridge Core's Anchor Table. None is a router with no reply concept at
        #: all — every message replied to nothing and nothing is the newest.
        self._anchors = anchors
        #: The Agent seam's live fact: whether a Session's question can still be
        #: answered from here (ADR 0015). A callable, because it is read at the
        #: moment a numeral arrives and never kept.
        self._answerable = answerable or _never_answerable

    def classify(self, text: str, *, in_reply_to: str = "") -> Classification:
        """Read one inbound line. Fails closed on anything it cannot place.

        `in_reply_to` is the adapter's id of the message the user replied to,
        empty when they replied to nothing. Matched by equality against the
        Anchor Table and never parsed.
        """
        body = text.strip()
        if not body:
            # Empty is what a non-text message (voice note, photo, file) arrives
            # as (ADR 0021 §4). The hint is Core's wording, held in the table.
            return self._refuse(NON_TEXT_HINT)

        anchor = self._anchors.lookup(in_reply_to) if self._anchors is not None else None
        if anchor is not None:
            session = self._live_session_of(anchor)
            if session is not None:
                return self._as_reply(body, anchor, session)
            # A row whose Session the roster no longer holds is an unknown
            # Anchor: words fall through to the grammar below, a numeral is
            # refused there. A reply to an Anchor that is not a Session's — an
            # Assistant Conversation's (ADR 0021 §7) — is #265's branch here.

        grammar = self._grammar
        if body.startswith(grammar.control_prefix):
            return self._as_command(body[len(grammar.control_prefix) :])
        if body.startswith(grammar.delegate_prefix):
            return self._as_delegation(body[len(grammar.delegate_prefix) :])
        if body.startswith(grammar.relay_marker):
            return self._as_named_relay(body[len(grammar.relay_marker) :])
        if _numeral(body) is not None:
            # Unknown anchor or none at all: Core cannot say which message's
            # options the number was read against, so it picks nothing.
            return self._refuse(NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT)
        return self._as_bare_text(body)

    def _live_session_of(self, anchor: Anchor) -> Session | None:
        """The live roster row an Anchor points at, or None when it points at none.

        None for a row that is not a Session's, and for a Session the roster no
        longer holds under that identity — ended between two discoveries, or a
        Codex row re-keyed on its first turn (#73). The table is trimmed to the
        roster after every discovery pass, so this is the gap between passes.
        """
        if not isinstance(anchor.target, SessionTarget):
            return None
        try:
            session = self._sessions.resolve(anchor.target)
        except BridgeCoreError:
            return None
        return session if session.is_live else None

    def _as_reply(self, body: str, anchor: Anchor, session: Session) -> Classification:
        """Text replying to a message Core sent about one Session."""
        target = session.target
        position = _numeral(body)
        if position is not None:
            return self._as_pick(anchor, session, position)
        prefix = self._grammar.control_prefix
        if body.startswith(prefix):
            return Classification(
                kind=InboundClass.ANSWER_RELAY,
                text=_skill_words(body[len(prefix) :], prefix),
                target=target,
            )
        return Classification(kind=InboundClass.ANSWER_RELAY, text=body, target=target)

    def _as_pick(self, anchor: Anchor, session: Session, position: int) -> Classification:
        """A numeral on a known Anchor: the Nth label, resolved by the row's kind."""
        if anchor.kind is not AnchorKind.NOTICE or not 1 <= position <= len(anchor.options):
            # A receipt, a prompt, an answer, or a number past the last option.
            # Menu screens resolve by position too and are #264's branch here.
            return self._refuse(NUMERAL_PICKS_NOTHING_HINT)
        label = anchor.options[position - 1]
        if anchor.approval_id:
            # A permission notice: the label is one of the two verdicts the row
            # was registered with (ADR 0021 §6). Whether the dialog is still open
            # is the Approval Relay's own answer, not a check made here.
            return Classification(
                kind=InboundClass.APPROVAL_RELAY,
                text=label,
                target=session.target,
                approval_id=anchor.approval_id,
                verdict=ApprovalVerdict(label),
            )
        if not self._still_asking(session, anchor) or not self._answerable(session.target):
            # Answered at the terminal, the hook ended, or the Session has moved
            # on to a different question. Nothing is sent: the label would arrive
            # as words to a question that no longer exists — or, worse, as an
            # answer to one it was never offered for.
            return self._refuse(QUESTION_ALREADY_ANSWERED_HINT)
        return Classification(kind=InboundClass.ANSWER_RELAY, text=label, target=session.target)

    @staticmethod
    def _still_asking(session: Session, anchor: Anchor) -> bool:
        """Whether the roster's current wait is the question this notice carried.

        The row holds the question's option labels and the roster holds the
        Session's current wait; a numeral is honest only when the two agree. A
        Session that answered one question at the terminal and is now holding
        another still reports its question answerable, and without this check
        the old notice's label would be relayed into the new question with the
        user's authority.
        """
        waiting_for = session.waiting_for
        if waiting_for.kind is not WaitingKind.QUESTION:
            return False
        return tuple(option.text for option in waiting_for.options) == anchor.options

    def _as_command(self, rest: str) -> Classification:
        verb, _, arguments = rest.strip().partition(" ")
        if verb.casefold() not in self._grammar.control_commands:
            return self._refuse(f"I have no command called {verb!r}")
        return Classification(
            kind=InboundClass.CONTROL, command=verb.casefold(), text=arguments.strip()
        )

    def _as_delegation(self, rest: str) -> Classification:
        prompt = rest.strip()
        if not prompt:
            return self._refuse("that asked me to delegate, but did not say what")
        return Classification(kind=InboundClass.DELEGATION, text=prompt)

    def _as_named_relay(self, rest: str) -> Classification:
        name, separator, words = rest.partition(":")
        if not separator:
            return self._refuse(
                f"name the session and then the words, like "
                f"{self._grammar.relay_marker}<session>: your words"
            )
        try:
            session = self._sessions.match_name(name.strip())
        except AmbiguousNameError as ambiguous:
            return self._refuse(self._which_one(ambiguous.candidates))
        except NameMatchError:
            return self._refuse(f"nothing running matches {name.strip()!r}")

        if not words.strip():
            return self._refuse(f"that named {spoken_name(session)} but carried no words")
        return Classification(
            kind=InboundClass.ANSWER_RELAY, text=words.strip(), target=session.target
        )

    def _as_bare_text(self, body: str) -> Classification:
        live = self._sessions.live()
        if not live:
            return self._refuse("nothing is running for me to pass that to")

        collision = self._command_collision(body, live)
        if collision is not None:
            return collision

        # "Reply to the previous message" (ADR 0021 §2): with messages lying flat
        # on this surface, the newest Anchor's target takes the place the Focus
        # Session has on the voice side. A newest row the roster no longer holds
        # is skipped rather than refused on, and today's rule stands.
        newest = self._anchors.newest() if self._anchors is not None else None
        session = self._live_session_of(newest) if newest is not None else None
        if session is not None:
            return Classification(kind=InboundClass.ANSWER_RELAY, text=body, target=session.target)

        if len(live) > 1:
            return self._refuse(self._which_one(live))
        return Classification(kind=InboundClass.ANSWER_RELAY, text=body, target=live[0].target)

    def _command_collision(self, body: str, live: tuple[Session, ...]) -> Classification | None:
        """A bare word that is also a registered command has two honest readings.

        Exact match only. Guarding a whole sentence that merely *starts* with a
        command word would swallow ordinary speech — "stop after the tests pass"
        is words for a Session and nothing else.
        """
        if body.casefold() not in self._grammar.control_commands:
            return None
        return self._refuse(
            f"{body!r} could be the command or words for a session — "
            f"say {self._grammar.control_prefix}{body.casefold()} for the command, or "
            f"{self._which_one(live)}"
        )

    def _which_one(self, candidates: tuple[Session, ...]) -> str:
        """Name every candidate and the form that picks one. Never picks itself."""
        marker = self._grammar.relay_marker
        named = ", ".join(f"{marker}{spoken_name(session)}" for session in candidates)
        return f"say which one: {named}"

    @staticmethod
    def _refuse(reply: str) -> Classification:
        return Classification(kind=InboundClass.UNKNOWN, reply=reply)


def _numeral(body: str) -> int | None:
    """The position a body names, or None when it is words. Whole digits only.

    `int` is the test, not a spelling check before it (#211's rule in
    `control_plane/commands.py`): a digit string `int` refuses — past CPython's
    digit limit — is words, not a traceback the event dies in unanswered.
    """
    if not body.isdecimal():
        return None
    try:
        return int(body)
    except ValueError:
        return None


def _skill_words(rest: str, prefix: str) -> str:
    """The words that carry a `/x` line into a Session, naming the act (#248)."""
    name, _, arguments = rest.strip().partition(" ")
    command = f"{prefix}{name}"
    if not arguments.strip():
        return SKILL_WORDS.format(command=command)
    return SKILL_WORDS_WITH_ARGUMENTS.format(command=command, arguments=arguments.strip())
