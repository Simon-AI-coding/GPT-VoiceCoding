"""Bridge Core assembled — the hub, and the one loop that drains its events.

Every seam's events land on one queue and one dispatch drains it (`core.events`),
which is what makes ordering, serialisation and Reply-Window queueing naturally
the hub's business. This is that dispatch: it owns no policy of its own, it
turns each event into a call on the pipeline that owns the decision.

**Adapters are injected as Protocols.** Bridge Core never imports
`gpt_voicecoding.adapters`, so the whole hub runs against a fake call, fake
agents and a fake channel with no network and no audio (ADR 0001, principle 4).
Assembling the *real* adapters from configuration is the composition root's job
and lives with the control-plane surface, not here.

**ADR 0002 is honoured by two verbs that consult nothing.** `status` and
`flip_switch` never ask the adjudicator, so they answer with every switch off,
including Duty. Replying to text the user just sent is the same category: a
reply is not a push, and the Companion Channel is one of the control-plane's
surfaces, so an inbound message always gets an answer. If that were gated, the
one way to turn Duty back on from away from the computer would be gated too.

Three things are recorded here rather than decided here. `UserSpeech` is the
in-call transcript, and Bridge Core never parses one: spoken intent arrives as
structured control-plane calls the voice thread makes, so the event is written
to the log and nothing else. The two speaking spans are the same conversation's
edges and are treated the same way. The control-plane command set and the
Delegated Turn's execution belong to the surfaces that own them, so both arrive
as injected handlers with honest defaults rather than being invented here.

**Every call event is also handed to the Call Keeper** (`core/call_keeper.py`),
which is where the call's *time* is kept: one call at a time, Cool-down, the
Silence Ceiling and the two cues. This module dispatches to it and reads
`status()` off it; it holds no call state of its own, because "the call is up"
having two answers in core is the defect #195 closed.

**What used to be the escalation pipeline is one Companion Channel push.** The
route matrix, open-and-speak and the two call routes are gone: dialling is the
Keeper's, and it dials from a fresh reading at the moment it acts rather than
from the notice that provoked it (ADR 0017). What is left is `_push` — text,
under the Message Switch — which is the half a Live Call was never a surface for
(`CONTEXT.md`, *Stop Notice*).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from gpt_voicecoding.core import briefing, menu
from gpt_voicecoding.core.adjudication import Outlet, SwitchAdjudicator
from gpt_voicecoding.core.anchors import Anchor, AnchorKind, AnchorPick, AnchorTable, Screen
from gpt_voicecoding.core.briefing import (
    PERMISSION_ALREADY_SETTLED_HINT,
    MenuWord,
    RosterBrief,
    SessionBrief,
)
from gpt_voicecoding.core.call_keeper import CallKeeper, Occasion
from gpt_voicecoding.core.clock import Clock, default_clock, wall_clock
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    CallInstructionsMissing,
    ChildSessionError,
    HeadlessRunError,
    LaneUnreadable,
    ProgressUnavailable,
    StaleSessionError,
    UnknownRelayError,
)
from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.core.instructions import InstructionContext, Instructions, generate
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.menu import MenuScreen
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay
from gpt_voicecoding.core.relays import (
    RelayAuthority,
    RelayOutcome,
    RelayPipeline,
    reaction_for,
    reason_for,
    receipt_sentence,
    sentence_stands_alone,
)
from gpt_voicecoding.core.router import Classification, InboundClass, InboundRouter, TextGrammar
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import SwitchSnapshot
from gpt_voicecoding.core.turns import (
    THE_NAMELESS_ONE,
    DelegatedAnswer,
    DelegatedTurnFinished,
    DelegatedTurns,
)
from gpt_voicecoding.core.verification import (
    AGENT_SEAM_PREFIX,
    CALL_SEAM,
    CHANNEL_SEAM,
    SeamLoad,
    SeamVerification,
    Verifiable,
    compare,
)
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    ApprovalRequest,
    ApprovalVerdict,
    HistoryPage,
    LaneUnavailable,
    ProgressAvailability,
    ProgressObservation,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.call import (
    CallAdapter,
    CallAgent,
    CallDropped,
    CallEnded,
    CallSnapshot,
    CallStarted,
    CallState,
    Dial,
    HandoverItem,
    ModelChoice,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.companion_channel import (
    BriefState,
    ChannelReceipt,
    CompanionChannel,
    InboundText,
    Notice,
    SessionNotice,
)
from gpt_voicecoding.seams.control_plane import Action
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import (
    AgentKind,
    RequestId,
    SessionTarget,
    new_request_id,
)

_log = logging.getLogger(__name__)

#: Answers an inbound command when no control-plane surface is wired to this hub.
NO_CONTROL_SURFACE = "I recognised that command, but no control surface is wired up here"


@dataclass(frozen=True, slots=True)
class ControlAnswer:
    """What the wired control surface said, and whether it was a refusal.

    The words are what the user is told either way, so most callers use `text`
    and nothing else. `ok` is the one fact that cannot be recovered from them:
    a refusal reads as ordinary prose, and a path that registers an Anchor on
    the answer has to know which of the two it got — **a refusal is never an
    Anchor** (ADR 0021 §2, #263). The surface holds that fact already
    (`Reply.ok`) and used to drop it on the way back, which is how the menu's
    `history` choice came to anchor a Session to a refusal (#264 review).
    """

    text: str
    ok: bool


#: The three menu verbs this hub answers with a screen of its own (ADR 0021 §6
#: §7, #264, #265) rather than through the control-plane surface: a screen is an
#: Anchor, and the row is registered inside the one send site, which the
#: surface's text answer cannot reach. The same three answer `bridgectl` in text
#: through `sessions_screen` / `config_screen` / `open_assistant`, so there is
#: one screen apiece.
SCREEN_VERBS: frozenset[str] = frozenset(
    {str(Action.SESSIONS), str(Action.CONFIG), str(Action.ASSISTANT)}
)

#: Answers an inbound delegation when no Delegated Turn handler is wired.
NO_DELEGATE_HANDLER = "I can't take a delegated turn right now — nothing is wired to answer it"

#: The two lines a run is told the Voice held its call open by. Fixed strings
#: rather than a formatted one, because the acceptance step matches them: an
#: engine that says nothing when the ceiling is held leaves a whole-lane run no
#: way to tell "the call outlived the ceiling" from "the ceiling never ran"
#: (#184). One per edge, never per delta — a long answer is hundreds of those.
VOICE_SPEAKING_LINE = "the call's own Voice started speaking"
VOICE_QUIET_LINE = "the call's own Voice stopped speaking"


def stop_brief(
    session: Session,
    waiting_for: WaitingFor,
    *,
    progress: ProgressObservation | None = None,
    question_answerable: bool = False,
    peers: Sequence[Session] = (),
) -> SessionBrief:
    """The Session Brief a Stop announces — this reading, on the row it is about.

    **Bridge Core words nothing about a Session.** `CONTEXT.md`'s *Stop Notice*
    is "a Session Brief published as text", so what a stop produces here is the
    brief and `briefing.text` is what renders it — one vocabulary for the
    channel, the log and `bridgectl brief`, which is the defect #166 named. The
    five renderers that used to live here are gone: the composer, the "it said /
    nothing said yet / oversize" line, the per-wait-kind sentence with its
    options and recommendation, the "answer it in the terminal" constant, and
    their use of a `spoken_reference` helper, which went with the approval
    announcement that was its last caller (#191). The omission wording lives in
    `briefing.NEWEST_WORDING` and the state wording in `briefing.STATE_WORDING`.

    Legacy (ADR 0010): `legacy@1d32845:bridge/host.py:213-235` announced a
    content-free notice — **adapted**, the notice now carries the brief. Naming
    the Session on it (ported in #109) is carried by the brief's name field.

    **The reading this path is announcing wins over the row's.** A Stop is read
    at the moment the Session stopped, and the sweep and reconcile paths each
    carry the wait they are about; the roster row supplies everything else — the
    Session Name, the workspace, the last activity — that the reading itself does
    not know. The row is also what the brief is *addressed* as: it carries the
    better-known target where the roster holds one, so no separate address is
    passed in beside it.

    **The state is the row's, and this path never derives one.** A Stop is not a
    Session running, and this used to be the one place that said so: it derived
    the state from the wait because `SessionRegistry.set_stop_reading` left a row
    that merely ended a turn in whatever state the last discovery pass found
    (#209). Since #213 the registry derives it, by the same rule in the one place
    it now lives (`WaitingFor.stopped_state`), so a row is briefed as the
    registry holds it and nothing here overrides it. **There is always a row**:
    since #216 a Stop for a Session no discovery pass has landed registers one
    standing in for it (`core/sessions.py::stand_in`), so the second, private
    derivation this function used to keep for that case is gone with the case.
    """
    row = replace(
        session,
        waiting_for=waiting_for,
        progress=progress if progress is not None else session.progress,
    )
    return briefing.session(row, question_answerable=question_answerable, peers=peers)


def _as_read_now(read: Session, fresh: ProgressObservation) -> Session:
    """The folded row, but saying what *this* read found about its progress.

    `Session.with_progress` keeps a readable observation when a newer pass could
    not answer, which is the right rule for a roster: a row that says nothing
    where it used to say something has lost a fact rather than gained one, and
    the roster is a standing account.

    It is the wrong rule for a verb that answers *now*. `brief <address>` is one
    fresh reading taken at the moment the user is spoken to, so a read that
    failed has to reach them as a failure — otherwise the Voice reads out a
    message from some earlier tick as though it had just been said, which is the
    one thing "read at the moment you speak" exists to prevent. The roster keeps
    what it had; the brief says what it found.
    """
    if fresh.availability is not ProgressAvailability.UNREADABLE:
        return read
    return replace(read, progress=fresh)


def _state_behind(window: ReplyWindow, held: SessionState) -> SessionState:
    """The Session state a Reply Window report implies, given what we already hold.

    An open window is a Session that will take the next turn, which is `IDLE`.
    A closed one has two causes and the report cannot tell them apart — mid-turn,
    or holding a dialog — so a Session already known to be `WAITING` keeps that,
    and anything else becomes `RUNNING`. Guessing the other way would erase a
    permission dialog from the roster while it is still on the user's screen.
    """
    if held is SessionState.WAITING:
        return held
    return SessionState.IDLE if window is ReplyWindow.OPEN else SessionState.RUNNING


#: The two verdicts a permission notice offers by number, in the order the
#: notice lists them (ADR 0021 §6): `1. allow  2. deny`. `ask` gets no line — not
#: answering hands the dialog back to the terminal, which is already the default.
PERMISSION_LABELS: tuple[str, ...] = (str(ApprovalVerdict.ALLOW), str(ApprovalVerdict.DENY))

#: The two states in which the user is being asked to settle something, and so
#: the two whose notice closes to `handled` when it stops being answerable from
#: this surface (ADR 0021 §8 as amended on #324). Read off the state rather than
#: off the question line, because a question asked in prose fills no question
#: line and is a decision all the same.
ASKING_STATES: frozenset[BriefState] = frozenset({BriefState.DECISION, BriefState.PERMISSION})


def _notice_anchor(target: SessionTarget, waiting_for: WaitingFor) -> Anchor:
    """The Anchor row a Stop Notice registers: what a numeral on it picks from.

    A question's own option labels, in the order shown; the two verdicts, on the
    dialog's handle, for a permission that has one to answer on; nothing for
    anything else — a `finished` notice offers nothing to pick, and a numeral on
    it is refused. `sent_at` is stamped by the send that registers the row.
    """
    if waiting_for.kind is WaitingKind.QUESTION:
        return Anchor(
            kind=AnchorKind.NOTICE,
            target=target,
            options=tuple(option.text for option in waiting_for.options),
        )
    if waiting_for.kind is WaitingKind.PERMISSION and waiting_for.approval_id:
        return Anchor(
            kind=AnchorKind.NOTICE,
            target=target,
            options=PERMISSION_LABELS,
            approval_id=waiting_for.approval_id,
        )
    return Anchor(kind=AnchorKind.NOTICE, target=target)


def _handled(notice: SessionNotice) -> SessionNotice:
    """The same brief, closed: the fixed closed word, and no labels (ADR 0021 §8).

    Two fields and nothing else. **Nothing is added** — no mark on the option
    the user chose, no line saying where it was answered; their own reply and
    its receipt already sit below it. And the word is fixed rather than the
    Session's state now, because a notice is the record of one stop and not a
    live roster row: tracking later states would mean re-editing it on every
    transition. **Empty labels are what draws no buttons** (§6), which is why
    no "remove the buttons" instruction exists anywhere on this seam.
    """
    return replace(
        notice, state_word=briefing.NOTICE_WORDING[briefing.NoticeWord.HANDLED], options=()
    )


def _receipt_anchor(target: SessionTarget) -> Anchor:
    """The Anchor row a relay's receipt registers: that Session, nothing to pick."""
    return Anchor(kind=AnchorKind.RECEIPT, target=target)


@dataclass(frozen=True, slots=True)
class Status:
    """Everything the control plane can ask for. Answered with any switch off."""

    switches: SwitchSnapshot
    sessions: tuple[Session, ...]
    #: Why a lane could not be enumerated at its last attempt, by agent. Empty
    #: is the ordinary case. It is here and not on a row because it is news
    #: about the lane: an unavailable lane's Sessions are not *missing*, they
    #: are unknown, and a roster that showed nothing without saying so would be
    #: claiming the machine is empty.
    lanes: Mapping[AgentKind, str]
    #: Which lanes are reading from a weaker source than usual, and which source.
    #: Distinct from `lanes`: these lanes *do* have Sessions to show, so folding
    #: the two together would hide a working lane behind a warning shaped like
    #: an outage. The Codex lane sits here whenever no shared daemon is up.
    degraded_lanes: Mapping[AgentKind, str]
    #: The call the system owns, or None. One voice surface, so one id.
    call_id: str | None
    #: Seconds of Cool-down left, or 0.0 when the system may dial right now.
    #: Published rather than kept inside the Keeper because it is the one rule
    #: with no surface of its own: a call that does *not* happen is invisible,
    #: so an operator asking why nothing rang has nothing else to read (#195).
    cool_down_remaining: float
    #: Whether an event inside that Cool-down bought a dial not yet paid.
    dial_owed: bool
    pending_relays: tuple[PendingRelay, ...]
    #: Reply Window levels include the lane's live question-route fact, which is
    #: deliberately not copied onto the roster row.
    reply_windows: Mapping[SessionTarget, ReplyWindow] = field(default_factory=dict)
    call_agent: CallAgent | None = None
    dial_attempt: str | None = None


class RosterBriefer:
    """The Call Keeper's Briefer, over the roster and `Briefing` (#167, ADR 0017).

    The production half of the Keeper's one seam. It answers one question —
    *who needs the user, right now* — and it answers it by reading the roster at
    the moment it is asked, never from an event that was replayed to it. The
    Keeper's own tests run against a fake with the same one verb, which is what
    lets Cool-down and the Silence Ceiling be proved with no Sessions in sight.

    Held as a named class rather than a closure so it has a docstring and a
    place for the one fact the roster cannot carry: whether a lane can still
    route an Answer Relay into a Session's question (#213). That fact is a live
    adapter reading, so it arrives as a callable the hub supplies rather than as
    a value read once and kept.
    """

    def __init__(
        self, sessions: SessionRegistry, *, answerable: Callable[[SessionTarget], bool]
    ) -> None:
        self._sessions = sessions
        self._answerable = answerable

    def read(self, occasion: Occasion) -> tuple[HandoverItem, ...] | None:
        """Ask Briefing for this occasion's answer from the live roster."""
        sessions = self._sessions.live()
        return (
            briefing.for_call(
                sessions,
                self._sessions.spoken_first,
                occasion=occasion,
                answerable=tuple(
                    session.target for session in sessions if self._answerable_for(session)
                ),
            )
            or None
        )

    def _answerable_for(self, session: Session) -> bool:
        """The one fact a row cannot carry, for one Session (`handover`'s own test)."""
        return session.waiting_for.kind is WaitingKind.QUESTION and self._answerable(session.target)


class BridgeCore:
    """The hub: one truth, five pipelines, and the loop that feeds them."""

    def __init__(
        self,
        *,
        state: BridgeState,
        call: CallAdapter,
        channel: CompanionChannel,
        agents: Mapping[AgentKind, AgentAdapter],
        events: EventQueue | None = None,
        policy: CorePolicy | None = None,
        grammar: TextGrammar | None = None,
        clock: Clock = default_clock,
        stamp: Clock = wall_clock,
        control: Callable[[Classification], Awaitable[ControlAnswer]] | None = None,
        delegate: Callable[[Classification], Awaitable[DelegatedAnswer]] | None = None,
        open_conversation: Callable[[], Awaitable[str]] | None = None,
        inventory: tuple[SeamLoad, ...] = (),
        instruction_context: InstructionContext | None = None,
    ) -> None:
        self._state = state
        self._desktop_reminder: briefing.DesktopReminder | None = None
        self._call = call
        self._channel = channel
        self._agents = dict(agents)
        self._events = events if events is not None else EventQueue()
        self._policy = policy or CorePolicy()
        self._control = control
        self._delegate = delegate
        #: Starts one Assistant Conversation's thread and names it (ADR 0021
        #: §7). None is an engine with no assistant, which says so and opens
        #: nothing. Bridge Core keeps the id it returns on the Anchor row and
        #: holds no list of its own: the table is the memory, and it is the
        #: thing that already forgets on a restart.
        self._open_conversation = open_conversation
        self._inventory = inventory
        #: Both generated instruction sets, made once from facts only the
        #: composition root knows — where the control-plane CLI is, and which
        #: engine it reaches. None until a root supplies them; a hub assembled
        #: for a test has no CLI to name and does not pretend to.
        self._instructions = generate(instruction_context) if instruction_context else None
        #: Durations are measured with `clock`; anything read outside this
        #: process is stamped with `stamp`. A Session's `first_seen` travels to
        #: every surface in the `sessions` payload, and a monotonic reading
        #: would name no moment on the far side.
        self._stamp = stamp
        #: The same reading the Keeper measures its own ceilings with, so the
        #: instant this hub calls "now" on a tick is the instant they are due at.
        self._clock = clock

        self.adjudicator = SwitchAdjudicator(state.switches)
        self.keeper = CallKeeper(
            call=call,
            briefer=RosterBriefer(state.sessions, answerable=self._question_answerable),
            adjudicator=self.adjudicator,
            dial_for=self._dial,
            policy=self._policy,
            clock=clock,
        )
        self.relays = RelayPipeline(
            agents=agents, sessions=state.sessions, relays=state.relays, clock=clock
        )
        #: The Delegated Turns in flight, and the replies queued behind each
        #: conversation (#268). Beside the Anchor Table for the same reason and
        #: with the same lifetime: memory only, and gone with the process that
        #: holds the threads they resume.
        self.turns = DelegatedTurns(run=self._run_turn, events=self._events)
        #: The emoji standing on each Relay's own message, keyed by Relay
        #: (ADR 0021, *a receipt is a reaction*; #321). The bot cannot read a
        #: reaction back out of a private chat, so a swap is made from what this
        #: engine last set rather than from what is there. Memory only, like the
        #: Relay queue it shadows, and an entry goes when its Relay does.
        self._relay_reactions: dict[RequestId, str | None] = {}
        #: The Anchor Table (ADR 0021 §2): memory only, never persisted, empty
        #: after every restart. Held here beside the router that reads it and
        #: the send path that fills it, and deliberately **not** on
        #: `BridgeState`, whose one job is deciding what is durable.
        self.anchors = AnchorTable(
            rows_per_target=self._policy.anchor_rows_per_session,
            conversations=self._policy.assistant_conversations,
            # **A conversation's queue goes with its rows, however they go.** The
            # table forgets a conversation on two occasions — the thread ended,
            # or it fell out past the per-chat cap — and only the first is one
            # this hub can see. A queue left behind the second would answer a
            # reply on a conversation the table no longer holds and re-register
            # it as the newest Anchor, sending the next thing typed to the
            # conversation the cap had just forgotten (#268 review).
            forgotten=self.turns.drop,
        )
        self.router = InboundRouter(
            sessions=state.sessions,
            grammar=grammar,
            anchors=self.anchors,
            answerable=self._question_answerable,
        )

    @property
    def instructions(self) -> Instructions | None:
        """All three instruction sets, as plain data.

        Generated once, from the catalogue and this engine's own installation.
        A Live Call is two audiences (ADR 0018): the Call adapter starts its
        realtime thread with the **agent** set, which is the half that acts, and
        the voice set waits for the Dial to give that seam a payload per
        audience. The Codex adapter starts a Delegated Turn with the delegated
        one. None of them rewrites a set, and none reads anything from disk to
        get one.
        """
        return self._instructions

    @property
    def events(self) -> EventQueue:
        """The sink every adapter is handed. One queue, one drain."""
        return self._events

    # ------------------------------------------------------------------
    # The control plane. ADR 0002: never gated, by anything, ever.
    # ------------------------------------------------------------------

    def status(self) -> Status:
        """What the system is doing. Consults no switch, so it always answers."""
        keeping = self.keeper.status()
        return Status(
            switches=self._state.switches.snapshot(),
            sessions=self._state.sessions.all(),
            lanes=self._state.sessions.lane_errors(),
            degraded_lanes=self._state.sessions.lane_degradations(),
            call_id=keeping.call_id,
            dial_attempt=keeping.dial_attempt,
            call_agent=self._call.call_agent,
            cool_down_remaining=keeping.cool_down_remaining,
            dial_owed=keeping.dial_owed,
            pending_relays=self._state.relays.pending(),
            reply_windows={
                session.target: self._reply_window(session)
                for session in self._state.sessions.all()
            },
        )

    def _question_answerable(self, target: SessionTarget) -> bool:
        adapter = self._agents.get(target.agent)
        if adapter is None:
            return False
        try:
            return adapter.question_answerable(target)
        except Exception:  # noqa: BLE001 - a query can only close, never drop, a Session
            _log.exception("the %s lane could not report its question route", target.agent)
            return False

    def _reply_window(self, session: Session) -> ReplyWindow:
        if session.lifecycle is not SessionLifecycle.LIVE:
            return ReplyWindow.CLOSED
        return derive_reply_window(
            session.state,
            session.waiting_for,
            session.child,
            question_answerable=self._question_answerable(session.target),
        )

    async def history(self, target: SessionTarget, *, before: int | None = None) -> HistoryPage:
        """One page of what an exact Session said and was told, read now (#171).

        A hub verb, and a *read*: it resolves one identity, asks that lane and
        no other, and returns a page of `history_page_entries` entries
        newest-first with `older` saying whether more remain. The reference
        implementation's rule for its one-Session read, ported —
        `legacy@1d32845:bridge/daemon.py:2202-2271` and
        `legacy@1d32845:bridge/codex.py:1319-1348` resolved one exact registered
        identity, asked only that agent's own authority, and never fell back to
        another lane, a terminal or a screen. Legacy had **no paging**: its tail
        was a fixed 12 entries / 32 KB (`legacy@1d32845:config.plist:449-452`,
        `bridge/transcript.py:2841`), **dropped, because** a fixed tail cannot
        answer "the five before those". The count-bounded page and the ordinal
        cursor are new.

        **It is not a Relay and it costs no turn**, and it is **not folded into
        the roster** (ADR 0016's amendment). `inspect` keeps answering the
        newest tail and folding; a page is a separate read and is not a roster
        fact, so nothing here observes the row.

        **One lane read per page.** The registry's `resolve` supplies three of
        the four refusals — an identity nobody registered, a stale one, and a
        Child Process, which is seen and never spoken to (#68) — and the lane's
        own read supplies the fourth. A second `inspect` for a fresher staleness
        check would fold this read back into the cadence it is kept out of.

        The fourth refusal is two facts under one code:

        - *The lane could not be read.* `LaneUnreadable`, carrying the lane's
          own words.
        - *Nothing could read what it said.* `ProgressUnavailable` — a Codex
          thread the shared daemon does not hold, or a Claude Session whose
          transcript this engine was never told about. A Session that *was* read
          and had nothing before the cursor is not this: it answers with an empty
          page and `older=False`, which is an answer.
        """
        session = self._state.sessions.resolve(target)
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise LaneUnreadable(str(target.agent), "this engine has no adapter for that agent")
        try:
            page = await adapter.history(
                session.target,
                before=before,
                count=self._policy.history_page_entries,
            )
        except LaneUnavailable as unread:
            raise LaneUnreadable(str(unread.agent), unread.reason) from None
        if page.read_at is None:
            raise ProgressUnavailable(target)
        return page

    async def brief(self, target: SessionTarget | None = None) -> RosterBrief | SessionBrief:
        """The Roster Brief, or one Session Brief with Detail — read now.

        One verb with an optional address, because they are one question at two
        widths: *what is everything doing* and *what is that one doing*. The
        words come from Briefing and from nowhere else (#166), so the Voice, the
        Companion Channel and `bridgectl` are told the same thing.

        With no address it is a read of state the hub already holds — no lane is
        touched, so it answers as fast as `status` and cannot be made to hang by
        a lane that is down. With an address it is exactly one `inspect`,
        legacy's "exactly one fetch, read at the moment you speak"
        (`legacy@1d32845:skill/announcing.md` step 1, `bridge/host.py:399-405`),
        **ported**.

        **It never sets the Focus Session** (#165 Q2): asking about a Session is
        not replying to one, and a read that moved the focus would let the Voice
        change what it speaks first merely by looking.

        Its refusals are `history`'s, minus one. An unknown identity, a stale
        one, a Child Process and a lane that could not be read all refuse here
        exactly as they do there. What does **not** refuse is a Session whose
        *progress* could not be read: `history` exists to answer with a
        Session's own words and has nothing to say without them, while a brief
        still has a state, a wait and a name — so an unreadable reading becomes
        an unreadable `newest` beside a state read off the wait, which is the
        honest answer and the one the five states were drawn to carry (#320
        retired the sixth).
        """
        if target is None:
            roster = briefing.roster(self._state.sessions.all(), self._state.sessions.focus)
            if self._desktop_reminder is not None and (
                not self.adjudicator.may_use("duty")
                or self._desktop_reminder.row.target not in {row.target for row in roster.rows}
            ):
                self._desktop_reminder = None
            return replace(roster, desktop_reminder=self._desktop_reminder)
        brief, _ = await self._session_brief_now(target)
        return brief

    async def _session_brief_now(self, target: SessionTarget) -> tuple[SessionBrief, Session]:
        """One Session Brief, and the reading it was made from.

        Two callers need the reading itself and not only its words: `brief`
        returns the brief alone, and the Companion Channel's `brief` choice
        registers an Anchor whose labels must be *this* reading's wait. Taking
        the wait off a Session resolved before the read would let the row's
        labels disagree with the ones the notice printed — the same fault the
        unbidden Stop Notice path avoids by passing one reading to both
        (`_notice_anchor`, ADR 0021 §2).
        """
        row = await self._inspect_now(target)
        read = self._state.sessions.observed_one(row, now=self._stamp())
        return (
            briefing.session(
                _as_read_now(read, row.progress),
                question_answerable=self._question_answerable(read.target),
                peers=self._state.sessions.all(),
            ),
            read,
        )

    async def _inspect_now(self, target: SessionTarget) -> SessionInspection:
        """One exact Session, read now through its own lane and no other.

        `brief`'s reading: resolve one identity, ask the one lane that owns it,
        and refuse rather than answer from somewhere else. The row is returned
        unfolded, because the caller folds it on its own terms. `history` does
        not come through here — a page is a separate read that observes nothing
        (ADR 0016).
        """
        session = self._state.sessions.resolve(target)
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise LaneUnreadable(str(target.agent), "this engine has no adapter for that agent")
        try:
            row = await adapter.inspect(session.target)
        except LaneUnavailable as unread:
            raise LaneUnreadable(str(unread.agent), unread.reason) from None
        if row.lifecycle is not SessionLifecycle.LIVE:
            raise StaleSessionError(target, reason=f"that Session is {row.lifecycle}")
        return row

    async def flip_switch(self, name: str, on: bool) -> bool:
        """Flip a switch and report the state it held before.

        The flip itself is never gated. What follows it is: turning an outlet on
        is an outlet transition, and a transition is the *only* thing that asks
        the next discovery pass to reconcile still-actionable waits.

        Which flips are transitions is read off the outlets themselves rather
        than from the flip's direction, because not every switch is an outlet.
        The Auto Hang-up Switch is the plain case: it opens no way to reach the
        user, so turning it on owes nobody a re-announcement of what they are
        already waiting on.

        **A transition is one `wake`, and it is acted on now.** It used to set a
        flag the next discovery pass consumed, which meant the announcement was
        composed from rows read before the outlet existed. Since #195 the Keeper
        reads the roster at the moment it dials (ADR 0017) and the text side
        reads it here, so there is nothing left for a flag to defer — and one
        wake for the transition, rather than one per Session, is what keeps
        "Duty on" from ringing once per waiting row.
        """
        opened_before = self.adjudicator.outlets()
        previous = self._state.switches.flip(name, on)
        if not self.adjudicator.may_use("duty"):
            self._desktop_reminder = None
        self._state.persist()
        opened = self.adjudicator.outlets() - opened_before
        if opened:
            await self._an_outlet_opened(voice=Outlet.VOICE in opened)
        return previous

    async def models(self) -> tuple[ModelChoice, ...]:
        """The startup catalog, available with every switch off."""
        return await self._call.models()

    async def forget_call_agent(self) -> None:
        """A fresh agent may be requested between calls, never during one."""
        if (await self._call.call_state()).state is not CallState.DOWN:
            raise BridgeCoreError("end the call before forgetting its Call Agent")
        self._call.forget_call_agent()

    async def live_toggle(self, *, attempt_id: str | None = None) -> CallSnapshot:
        """The one action: end the call the system owns, or start one if none is up.

        **Never gated, by any switch.** The Live Toggle is a control-plane
        action, and ADR 0002 is absolute. The switches read the other way round:
        Duty, Voice and Message constrain what *the system* may do on its own —
        speak, push, touch the call unbidden — and this is the user touching the
        call with the system as the instrument, exactly like flipping a switch.
        Gating it would produce the indefensible case: Voice is flipped off while
        a call is up, and the user's explicit "end this call" is refused by the
        very switch that says the system should be quiet.

        Only the Call Keeper constrains it, in both directions: one call at a
        time, and Cool-down does not apply to the user's own toggle
        (`CONTEXT.md`). There is one path here and every surface calls it — a
        surface holding its own call state is how two toggles once opened two
        calls.
        """
        return await self.keeper.live_toggle(attempt_id=attempt_id)

    async def cancel_dial(self, attempt_id: str) -> bool:
        """The user's cancellation is scoped to the dial they saw."""
        return await self.keeper.cancel_dial(attempt_id)

    def _dial(self, hand_over: tuple[HandoverItem, ...]) -> Dial:
        """What a call this hub opens is opened on: two audiences and a hand-over.

        The one place a `Dial` is built, and therefore the one place that can say
        which half is missing when this engine generated nothing. The refusal is
        here rather than in the Call Keeper because that is a door and this is a
        source: the Keeper decides *whether* a call may open and when, and only
        the hub knows what it would be opened on (ADR 0018; #193's deferred note
        on the error's old name). The Keeper is handed this method and calls it
        at the moment it dials, so a set the hub regenerated is never stale.
        """
        instructions = self._instructions
        if instructions is None:
            raise CallInstructionsMissing("prose for the Voice or rules for the Call Agent")
        if not instructions.voice.text.strip():
            raise CallInstructionsMissing("prose for the Voice")
        if not instructions.agent.text.strip():
            raise CallInstructionsMissing("rules for the Call Agent")
        return Dial(
            voice=instructions.voice.text, agent=instructions.agent.text, hand_over=hand_over
        )

    async def _an_outlet_opened(self, *, voice: bool) -> None:
        """An outlet the switches now allow just became usable. Say what still waits.

        **One wake for the voice side, one fresh reading for the text side, and
        each only when its own outlet is what opened.** The Keeper is told when
        the *Voice* outlet opens — a Duty or Voice switch turning on is one more
        `wake` (#195) — and it decides for itself whether that is a dial, an owed
        dial or nothing, briefing from the roster at the moment it acts (ADR
        0017). Turning the **Message** Switch on is not a reason to ring: it
        opens a way to reach the user in text, and reaching them in text is what
        the push below does. Waking on it would dial a call the user asked for
        messages instead of.

        The Companion Channel has no component of its own, so its reading is
        taken here and each live main row that still needs the user is pushed —
        on either transition, because a row that still waits is news on whatever
        outlet has just become available.

        **Bridge Core keeps no memory of what it has already announced (#161).**
        Whether to announce is a function of this reading alone, so a wait that
        still needs the user is reported on every transition, in the same words.
        A set of delivered waits used to suppress the repeat; it could not work,
        because the action that invalidates it — an adapter handing an unanswered
        dialog back to the terminal — happens where Bridge Core cannot see it.

        What went with #195: the *deferral*. This used to set a flag that the
        next discovery pass consumed, and only for lanes that pass had actually
        read; both halves now read the roster at the moment of acting, so the
        flag, the `outlets_changed` hook that set it and the lane filter that
        qualified it are gone.
        """
        for session in self._state.sessions.live():
            if not session.child.is_main or not session.waiting_for.needs_the_user:
                continue
            await self._announce_waiting(session, session.target, session.waiting_for)
        if voice:
            await self.keeper.wake(focus=False)

    async def relay(
        self, target: SessionTarget, text: str, *, route: RelayRoute = RelayRoute.DELIVER
    ) -> RelayOutcome:
        """An Answer Relay: the user's own words, for one exact Session.

        A hub verb rather than a pipeline a surface reaches into. Outsiders see
        one Bridge Core (ADR 0001), and a surface that knew which pipeline owned
        which decision would be a surface that has to be changed when the hub
        rearranges itself.
        """
        outcome = await self.relays.relay(target, text, route=route)
        # The user has just spoken to this Session, so it becomes the Focus
        # Session (#165 Q2) — whatever the receipt says. Focus follows the
        # user's attention, and a Relay that failed to land is precisely the
        # Session they are still waiting on.
        self._state.sessions.set_focus(outcome.target)
        await self._settle(outcome)
        return outcome

    async def answer_approval(
        self,
        approval_id: str,
        verdict: ApprovalVerdict,
        *,
        message_id: str = "",
        origin: str = "",
    ) -> RelayOutcome | None:
        """Carry the user's verdict. None when no live row carries that handle.

        **The Approval Relay carries and nothing more** (#191). There is no
        pending-approval ledger to consult: the roster's current reading is the
        one truth about which dialogs are open, and the row that carries this
        handle in its wait is the Session the verdict belongs to. A handle no
        row carries is a hook that has ended — the dialog is the keyboard's
        again — and the receipt for that is the refusal this None becomes.

        **A spawned target is refused in its own words.** A Codex subagent
        thread can raise a real permission prompt; answering it would carry the
        user's authority into a Session `resolve` refuses to address a moment
        later, so it stays the keyboard's — "never spoken to" includes never
        answered (advisor, 2026-08-27). It reads differently from the refusal
        above because the user acts on it differently.

        Answering a permission is replying to that Session, so it takes the
        focus exactly as an Answer Relay does — whatever the receipt says.

        The receipt is `RelayOutcome`, the same shape `relay` answers with
        (#192): the state, the attempt's grade, and one reason code. `DELIVERED`
        exactly when the adapter proved it; every other grade is terminal here,
        because a verdict is never retried on this system's own authority and
        the dialog on screen is still the thing that can resolve it.
        """
        found = self._dialog_on_the_roster(approval_id)
        if found is None:
            _log.info("no live Session carries the dialog %s; the verdict is refused", approval_id)
            return None
        session, request = found
        if not session.child.is_main:
            raise ChildSessionError(session.target, session.child.parent)
        # **And a Headless Run is refused in its own words too** (#319). An
        # Approval Relay is a Relay, and this path does not go through
        # `resolve`, so the registry's one rule is asked for here explicitly.
        if session.is_headless_run:
            raise HeadlessRunError(session.target)

        adapter = self._agents.get(session.target.agent)
        if adapter is None:
            _log.info("no %s lane is loaded to carry the verdict", session.target.agent)
            return None

        request_id = new_request_id()
        receipt = await adapter.approval_relay(request, verdict, request_id=request_id)
        self._state.sessions.set_focus(session.target)
        outcome = RelayOutcome(
            request_id=request_id,
            target=session.target,
            state=Lifecycle.DELIVERED if receipt.is_delivered else Lifecycle.REPORTED_FAILED,
            route=RelayRoute.DELIVER,
            reason=reason_for(receipt),
            receipt=receipt,
            # A verdict is nothing but the user's own decision (ADR 0013's
            # amendment): the one route besides the held hook that carries it.
            authority=RelayAuthority.AS_THE_USER,
            # The message the numeral was typed in, where it was typed at all —
            # what the receipt's reaction goes on — and where it came from, so a
            # receipt sent later goes back the same way (#321).
            message_id=message_id,
            origin=origin,
        )
        # An Approval Relay is the user's own words arriving too (#165 Q2 sets
        # the focus from it for that reason), so a verdict that lands clears
        # whatever the last Relay that did not land left on the row.
        await self._settle(outcome)
        # **A settled approval is one of the three facts that close a notice**
        # (ADR 0021 §8), and it is closed here rather than on the Telegram path
        # so that a verdict carried from any surface closes it — the dialog is
        # gone from the terminal too, and the buttons on the phone would invite
        # a press that earns only a refusal.
        #
        # **Settled means the verdict arrived.** One that did not leaves the
        # dialog open on screen — "the dialog on screen is still the thing that
        # can resolve it", above — and taking its buttons away would close a
        # decision the user can still make from here.
        if receipt.is_delivered:
            await self._close_open_notices(session.target)
        return outcome

    def _dialog_on_the_roster(self, approval_id: str) -> tuple[Session, ApprovalRequest] | None:
        """The live row whose current wait carries that handle, and the request for it.

        Read off `WaitingFor.as_approval_request`, which is the one place a wait
        becomes the request the Approval Relay addresses, so the roster row the
        user was briefed from and the request the adapter is handed are the same
        fact. Child rows are found here and refused by the caller: they are on
        the roster, and a handle nobody could match would be the wrong refusal.
        """
        for session in self._state.sessions.live():
            request = session.waiting_for.as_approval_request(session.target)
            if request is not None and request.approval_id == approval_id:
                return session, request
        return None

    async def sessions_screen(self) -> MenuScreen:
        """`sessions`: the roster as a menu screen — text, brief and Anchor (ADR 0021 §6).

        The rows are the Roster Brief's, so the screen says what `brief` says
        and adds only labels; with no live Session it is the roster text and
        the nothing-running hint, and no Anchor.
        """
        brief = await self.brief()
        assert isinstance(brief, RosterBrief)  # `brief` with no target is the roster
        return menu.roster_screen(brief)

    def config_screen(self) -> MenuScreen:
        """`config`: `switch` / `verify` / `live` as choices."""
        return menu.config_screen()

    def switches_screen(self) -> MenuScreen:
        """The switch screen: one label per switch carrying its state now."""
        return menu.switches_screen(self._state.switches)

    async def open_assistant(self) -> MenuScreen:
        """`assistant`: open an Assistant Conversation and answer with its opening line.

        ADR 0021 §7. A thread is started with the delegated instructions and the
        `[delegate] model`, and the fixed opening line goes back as an Anchor
        carrying that thread id. **No turn is run**: the line is Core's own
        words, so a conversation costs nothing until the user replies to it.

        An engine with no assistant wired, or one whose Call seam could not
        start a thread, refuses in Core's own words rather than the wire's —
        what the user needs is the fact, not the failure — and anchors nothing.
        """
        if self._open_conversation is None:
            raise BridgeCoreError(briefing.ASSISTANT_UNAVAILABLE_HINT)
        try:
            thread_id = await self._open_conversation()
        except Exception as refusal:  # noqa: BLE001 - any lane failure is one fact to the user
            _log.warning("an Assistant Conversation could not be opened", exc_info=refusal)
            raise BridgeCoreError(briefing.ASSISTANT_UNAVAILABLE_HINT) from refusal
        if not thread_id:
            raise BridgeCoreError(briefing.ASSISTANT_UNAVAILABLE_HINT)
        return menu.assistant_screen(thread_id)

    async def verify(self) -> tuple[SeamVerification, ...]:
        """What configuration named, against what this engine actually loaded.

        ADR 0003: the comparison is the hub's because only the hub knows the
        configured side, and **every** pluggable seam is asked for itself. The
        ADR generalises past the Companion Channel deliberately — `verify` is a
        seam verb on all four for exactly this reason — so a Call adapter whose
        far side is down reports that, rather than the engine reciting the
        configuration back and calling it an observation.
        """
        reports: list[SeamVerification] = []
        for load in self._inventory:
            adapter = self._behind(load.seam)
            reports.append(compare(load, await adapter.verify() if adapter is not None else None))
        return tuple(reports)

    def _behind(self, seam: str) -> Verifiable | None:
        """The adapter this engine actually holds for one seam name, if any."""
        if seam == CALL_SEAM:
            return self._call
        if seam == CHANNEL_SEAM:
            return self._channel
        if seam.startswith(AGENT_SEAM_PREFIX):
            try:
                return self._agents.get(AgentKind(seam[len(AGENT_SEAM_PREFIX) :]))
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # The dispatch loop.
    # ------------------------------------------------------------------

    async def drain(self) -> int:
        """Dispatch everything waiting, in arrival order. Returns how many."""
        waiting = self._events.drain()
        for event in waiting:
            await self.dispatch(event)
        return len(waiting)

    async def tick(self) -> None:
        """Advance the time-driven ceilings. The composition root calls this on a timer.

        Deliberately the only time-driven thing in the hub, and there is one of
        them left: the Call Keeper's own clock — Cool-down expiry, the Silence
        Ceiling and the settle window — which is handed the instant and decides
        for itself what is due. Stop Notices are not replayed here.

        **A queued Relay has no clock at all** (#321). This method used to sweep
        the Relay ceiling and report every entry past ten minutes as a failure,
        which is how words were dropped ten seconds before the turn they were
        waiting for. They wait for that turn now, or for the Session's end.

        **No clock runs on a held dialog** (ADR 0015, amended by #191). A parked
        permission or question is bounded by the wire that holds it — Claude Code
        ends its hook at the installed block's timeout, the listener releases the
        entry when that socket closes, and a Codex dialog stays answerable from
        the TUI — so an engine-side sweep here was a second clock racing the
        first. What a release does is unchanged, and nothing is pushed on it: the
        next brief reads a row with no handle and says `answer: at the terminal`
        (ADR 0017, a fresh reading).

        **The Keeper is not ticked while unread call activity is waiting.** This
        runs on its own task and the dispatch loop runs on another
        (`engine/composition.py`), so a speaking edge that has been emitted but
        not yet taken has not reached the Keeper — and measuring silence then is
        how a call gets ended in the middle of the answer that was about to say
        it was not silent (#184). Waiting for the next pass costs a second on a
        sixty-second ceiling, and the same second on a Cool-down.

        The three kinds are named rather than the queue being asked whether it
        holds anything: this is the ceiling's own question — *was there activity
        on the call* — and a `SessionStopped` waiting to be read is not an answer
        to it. Asking the wider question let news about a Session hold a silent
        call open. The user's speaking span joins the pair the moment the seam
        raises one (#195).
        """
        if not self.events.unread(UserSpeech, UserSpeaking, VoiceSpeech):
            await self.keeper.tick(self._clock())

    async def discover(self) -> tuple[SessionTarget, ...]:
        """Ask every lane what Sessions exist, and make the roster agree.

        **This is how a Session gets onto the roster at all.** v1.0 bridges the
        Sessions the *user* starts (#68), so nothing announces one — the hub
        goes and looks, on the cadence the composition root sets, and each lane
        answers for itself.

        **One lane raising does not stop the others.** A lane is supposed to
        report its own trouble as `LaneDiscovery(error=...)`; one that raises
        instead is a defect in that adapter, and the answer to a defective lane
        is to leave its rows alone and keep asking the other one — which is
        exactly what the seam's own contract already says an error means.

        Returns the Sessions that ended on this pass, having already answered
        whatever was queued for them: a Session that disappears between two
        ticks owes the user the same news as one that reported its own death,
        and the roster is the only witness to the first kind.

        **Which rows ended is the registry's answer, not a diff taken here.** A
        Codex row is re-keyed when its Session takes its first turn and gains a
        thread id, and again when the user types `/new` (#73) — the same row,
        under a new `SessionTarget`. Comparing the roster before and after would
        read both as a departure and terminate the Relays queued for a Session
        that is sitting there waiting for them, so the question is asked of the
        one component that can tell a re-keying from a death.
        """
        # Read before the pass, because a Session it finds gone is marked ended
        # by it: the row is still the only place its name and its classification
        # are, and the line that announces it is written from that row.
        before = {session.target: session for session in self._state.sessions.live()}
        gone: list[SessionTarget] = []
        for kind, adapter in self._agents.items():
            try:
                lane = await adapter.discover()
            except Exception:  # noqa: BLE001 - a defective lane must not stop the rest
                _log.exception("the %s lane raised instead of reporting its trouble", kind)
                continue
            gone.extend(self._state.sessions.observe(kind, lane, now=self._stamp()))

        for target in gone:
            _log.info("Session %s is no longer running", target)
            for outcome in self.relays.session_ended(target):
                await self._settle(outcome)
            # **A Session that left the roster closes its notices too** (ADR
            # 0021 §8's fifth cause), and before the rows go, because the edits
            # are addressed to the ids those rows carry.
            await self._close_open_notices(target)
            # **And a Session found gone is announced like any other ending**
            # (ADR 0021 §9): a closed terminal is exactly the case the line
            # exists for, and Core does not distinguish it from a crash.
            await self._announce_ended(before.get(target))
            # Rows go with their Session (ADR 0021 §2): a reply to one of its
            # notices from here on takes the unknown-Anchor path.
            self.anchors.drop(target)
        # And a row is keyed by the identity the roster holds *now*: a Codex row
        # re-keyed on its first turn or on `/new` (#73, #77) is not among `gone`,
        # so the table is trimmed to the live roster after every pass.
        self.anchors.keep_sessions([session.target for session in self._state.sessions.live()])
        return tuple(gone)

    async def dispatch(self, event: Event) -> None:
        """Turn one event into a call on whichever pipeline owns the decision."""
        match event:
            case SessionStopped():
                await self._session_stopped(event)
            case SessionEnded():
                await self._session_ended(event)
            case ReplyWindowChanged():
                await self._reply_window_changed(event)
            case RelayReceipt():
                await self._relay_receipt(event)
            case CallStarted() | CallEnded() | CallDropped():
                # The Keeper owns every one of these: it adopts a call the user
                # opened, releases the one it held, paces the Cool-down that
                # follows any end, and plays the two cues. The hub records
                # nothing here, because "the call is up" has one truth in core.
                await self.keeper.heard(event)
            case InboundText():
                await self._inbound_text(event)
            case DelegatedTurnFinished():
                # The one event this hub raises for itself: a Delegated Turn ran
                # beside the loop and its answer comes back on it, so the send,
                # the Anchor and the reply bar are written where every other
                # piece of state is (#268).
                await self._turn_finished(event)
            case UserSpeaking():
                await self.keeper.heard(event)
            case VoiceSpeech():
                # Both edges are activity, and the ceiling is held between them.
                # Recorded, never read back: this system does not listen to
                # itself, and the words are the Voice's own (#184).
                await self.keeper.heard(event)
                _log.info("%s", VOICE_SPEAKING_LINE if event.speaking else VOICE_QUIET_LINE)
            case UserSpeech():
                await self.keeper.heard(event)
                # Recorded, never parsed. Spoken intent reaches Bridge Core as
                # structured control-plane calls the voice thread makes (#5),
                # over the transport the Call adapter raises (#6) — the router's
                # marker grammar would collapse every utterance to bare text and
                # relay the user talking *to* the system into a coding Session.
                # Written down rather than dropped: no-loss applies to events too.
                _log.info("user speech, for the voice thread to act on: %r", event.text)
            case _:
                _log.info("no pipeline consumes %s here", type(event).__name__)

    async def _session_stopped(self, event: SessionStopped) -> None:
        """A Session stopped. Say so in the log, then announce it.

        **The log line is the run's only way to attribute a notice to this
        engine.** Until #75 the announcement path wrote nothing when it *worked* —
        only its old retention and failure paths wrote — so a Stop Notice that
        reached the user left no trace at all, and the acceptance's
        `stop notice` step could satisfy its attribution check only on the
        failure path. An engine silent about the one event it exists to produce
        is the gap #48 named on the inbound side, on the outbound side.
        """
        if self._spawned(event.target):
            return
        # **Every Stop writes its reading to the roster, including the first one
        # about a Session no discovery pass has covered** (#216): the registry
        # stands a row in for it, so the text this path pushes and the fresh
        # roster reading a dial is briefed from (ADR 0017) say the same thing
        # about the same Session. There is no `known` branch left here — which
        # Session exists is the registry's question, and it has one answer.
        session = self._state.sessions.set_stop_reading(
            event.target,
            waiting_for=event.waiting_for,
            progress=event.progress,
            now=self._stamp(),
            has_controlling_terminal=event.has_controlling_terminal,
        )
        # **A Headless Run stops here, with its reading kept** (#319, ADR 0021
        # §9 as amended). The row is written first and the tier read off it
        # afterwards, so a Stop that beat every discovery pass is judged on the
        # fact it carried rather than on the default a stand-in would otherwise
        # have. Nothing follows: no Stop Notice, no Anchor, and no wake — a wake
        # re-briefs a roster this row is not on, so it is work with no consumer.
        if session.is_headless_run:
            return
        if session.is_addressable and self.adjudicator.may_use("duty"):
            row = briefing.roster((session,), None).rows[0]
            if row.state in {
                briefing.BriefState.DECISION,
                briefing.BriefState.PERMISSION,
                briefing.BriefState.FINISHED,
            }:
                self._desktop_reminder = briefing.DesktopReminder(new_request_id(), row)
        await self._announce_waiting(
            session,
            event.target,
            event.waiting_for,
            progress=event.progress,
        )
        # **One wake per wake-worthy event, and it carries no content.** Whether
        # this Session still needs the user is read again by the Briefer at the
        # moment the Keeper acts (ADR 0017); `focus` says only whether the event
        # concerns the Session spoken first, which is #196's to read.
        await self.keeper.wake(focus=self._state.sessions.spoken_first == event.target)

    async def _announce_waiting(
        self,
        session: Session,
        target: SessionTarget,
        waiting_for: WaitingFor,
        *,
        progress: ProgressObservation | None = None,
    ) -> None:
        """Announce one current wait through the same producer as its live event.

        **Bridge Core keeps no memory of what it has already announced (#161).**
        Whether to announce is a function of this reading alone, so a wait that
        still needs the user is reported on every outlet transition, in the same
        words. A set of delivered waits used to suppress the repeat; it could not
        work, because the action that invalidates it — an adapter handing an
        unanswered dialog back to the terminal and dropping its handle — happens
        where Bridge Core cannot see it, so any key Bridge Core computes goes
        stale unseen.

        **One wait, one notice, and the handle is not read here at all.** A
        permission used to reach this method twice — once as the Stop, once as
        the announcement its own pipeline made — and the tiebreak between them
        lived here. Since #191 a dialog travels on the Stop alone, so what the
        handle is for is `Briefing`'s question of whether the user can answer
        from here, read off the row it is given.

        Legacy (ADR 0010) — **dropped, because** its record of what a Session was
        last announced on is `CurrentSessionStop`
        (`legacy@1d32845:bridge/store.py:870-897`), with its identity derived
        from live state (`legacy@1d32845:bridge/daemon.py:1420-1493`). It is one
        of the durable ledgers #67's port table leaves behind, and the rule that
        replaces it is #80's — reconcile the current state and replay nothing.
        """
        if session.is_headless_run:
            # **Read off the row, never off the event** (#319). This path is
            # reached from a Stop and from an outlet transition alike, and only
            # the row carries what every reading of this run has established.
            _log.info("%s is a Headless Run, so nothing is announced about it", target)
            return
        brief = stop_brief(
            session,
            waiting_for,
            progress=progress,
            question_answerable=(
                waiting_for.kind is WaitingKind.QUESTION and self._question_answerable(target)
            ),
            # The roster, for the one word a single row cannot supply: a Session
            # waiting on another is announced by *that* Session's Session Name
            # (#320).
            peers=self._state.sessions.all(),
        )
        # The log carries the brief's text too, so the one wording is what the
        # run's own record shows (#166 B5/B6). `Session stopped:` opens it
        # unchanged: `tests/acceptance/journey.py::ENGINE_STOP_LINE` greps the
        # first line of the record, and `drain_boot_notice` reads that grep.
        _log.info("Session stopped: %s", briefing.text(brief))
        # **Text here; the voice side is the Keeper's `wake`.** The brief is not
        # handed to a call from this path at all any more: what a call is opened
        # holding is read fresh at the moment it is dialled, from the roster
        # rather than from this reading (ADR 0017, #195). Which is why nothing
        # is returned — there is no route matrix left to report which door the
        # notice went through. The structured brief travels beside the text
        # (ADR 0021 §5): same words, and a surface with a layout of its own
        # arranges them rather than printing the lines.
        # **Every Stop Notice is an Anchor** (ADR 0021 §2), whatever it stopped
        # on: a reply to it is words for this Session, and a numeral picks from
        # the labels registered with it — the question's options, or the two
        # verdicts when it carried a permission (§6), on that dialog's handle.
        await self._push(
            briefing.text(brief),
            notice=briefing.notice(brief),
            anchor=_notice_anchor(target, waiting_for),
        )

    async def _session_ended(self, event: SessionEnded) -> None:
        """A Session is gone: close what it left open, say so once, forget its rows.

        **The order is fixed by the ids** (ADR 0021 §9). The edits name the
        message ids of this Session's own rows, so they go while the table still
        holds them; the ended line follows, so the chat reads in the order the
        facts happened; the rows go last, and a reply to any of them from then
        on takes the unknown-Anchor path.

        Which of the three the user actually sees is the switches' answer and
        differs between them on purpose: closing a notice is a correction to a
        message already sent and obeys Duty alone (§8), while the ended line is
        an unbidden push like every other and rides the Message Switch (§9).
        """
        # **Whether this is the ending or a second sighting of it.** A discovery
        # pass that misses a Session ends its row itself (#266's announcement is
        # made there too), and the lane's own event can arrive after it —
        # `mark_ended` is idempotent and hands back the same ended row, so the
        # row alone cannot say which of the two this is. The roster before the
        # mark can.
        was_live = event.target in {session.target for session in self._state.sessions.live()}
        ended: Session | None = None
        try:
            ended = self._state.sessions.mark_ended(event.target)
        except BridgeCoreError:
            _log.info("a Session ended that was never registered: %s", event.target)
        self._state.persist()
        for outcome in self.relays.session_ended(event.target):
            await self._settle(outcome)
        await self._close_open_notices(event.target)
        await self._announce_ended(ended if was_live else None)
        # Rows go with their Session (ADR 0021 §2), and last: the edits above
        # are addressed to the ids these rows carry.
        self.anchors.drop(event.target)

    async def _announce_ended(self, ended: Session | None) -> None:
        """Say once that a Session is gone — wherever the end was seen (ADR 0021 §9).

        Both paths come here: the lane's own `SessionEnded`, and the discovery
        pass that finds a Session no longer on the roster. Core does not
        distinguish exit, crash or closed terminal, so neither does this.

        **A Session nobody was told about is not announced as gone.** An
        unregistered target has no row to name it from, and a Child Process is
        seen and never spoken *about* (#79) — it got no Stop Notice either, so
        an ended line would be the first and last the user ever heard of it. The
        row's own classification answers that, because `resolve` refuses an
        ended Session before it reaches the question of whether it was a child.
        """
        if (
            ended is not None
            and self._desktop_reminder is not None
            and self._desktop_reminder.row.target == ended.target
        ):
            self._desktop_reminder = None
        if ended is None or not ended.child.is_main or ended.is_headless_run:
            return
        await self._push(briefing.ended_line(ended))

    async def _reply_window_changed(self, event: ReplyWindowChanged) -> None:
        """An adapter saw the window move between two discoveries. Land it on the state.

        The window is derived, so there is nothing here to set directly: this
        event is a coarser reading of the same fact and it lands on the field
        the fine-grained one lands on. The next discovery overwrites both, which
        is what makes this a shortcut rather than a second source of truth.

        A held question is the exception to the state shortcut. Its listener can
        open the route before the roster has reported `WAITING`; the event proves
        the route, not `IDLE`, so only a discovery pass or a Stop — the two
        readings that looked at the Session itself — may change the state there.
        """
        try:
            held = self._state.sessions.resolve(event.target)
            question_route_open = event.window is ReplyWindow.OPEN and self._question_answerable(
                event.target
            )
            if not question_route_open:
                self._state.sessions.set_state(
                    event.target, _state_behind(event.window, held.state)
                )
        except BridgeCoreError:
            _log.info("a Reply Window changed on an unknown Session: %s", event.target)
            return
        if event.window is ReplyWindow.OPEN:
            for outcome in await self.relays.reply_window_opened(event.target):
                await self._settle(outcome)
            return
        # **The window closing closes a permission's notice and leaves a
        # question's as sent** (ADR 0021 §8 as amended 2026-09-09, #323).
        #
        # The rule is stated as *why* the window closed, and on this path "why"
        # reduces to *what the notice carried*, which is a fact Core holds. The
        # other causes close at their own call sites before any window event
        # reaches here — our settled verdict (`answer_approval`), our delivered
        # Relay, `SessionEnded`, a Session gone from a discovery pass — and a
        # notice closes once (`mark_handled`), so a question notice still open
        # when the window shuts was answered at the terminal. That is the stop
        # the user resolved themselves, and the record of it needs no edit. A
        # permission handed back to the keyboard is the one cause left on this
        # path, and it still closes: its buttons would invite a press that earns
        # only a refusal.
        await self._close_open_notices(event.target, close_questions=False)

    async def _relay_receipt(self, event: RelayReceipt) -> None:
        """A receipt that arrived after the call returned. The ledger records it.

        **And a late proof of delivery re-renders the receipt on the user's own
        message** (#321). It makes no difference to the user whether the proof
        came back inside the call or minutes later on the Claude inbox's own
        acknowledgement route (ADR 0013): what they see is the same message
        wearing the standing the words are actually in, so this is a settlement
        like any other and goes through the one place that renders them.
        """
        try:
            classified = self._state.relays.classify(event.receipt.request_id, event.receipt)
        except UnknownRelayError:
            _log.info("a receipt arrived for a Relay that is no longer pending")
            return
        await self._settle(
            RelayOutcome(
                request_id=event.receipt.request_id,
                target=classified.target,
                state=(Lifecycle.DELIVERED if event.receipt.is_delivered else Lifecycle.RETAINED),
                route=classified.route,
                reason=reason_for(event.receipt),
                receipt=event.receipt,
                message_id=classified.message_id,
            )
        )

    async def _inbound_text(self, event: InboundText) -> None:
        """Classify one inbound line, act on it, and always answer the user."""
        # Which of our own messages the user replied to is a fact the adapter
        # saw; what it means is read here against the Anchor Table (ADR 0021 §2).
        found = self.router.classify(event.text, in_reply_to=event.in_reply_to)
        # Every answer goes back the way the text came: the event's `origin` is
        # echoed onto the reply (ADR 0021 §4), which is what the field promised.
        match found.kind:
            case InboundClass.CONTROL if found.command in SCREEN_VERBS:
                # A menu verb answers with a screen, which is an Anchor: sent
                # from here so its row is registered (ADR 0021 §6, #264).
                await self._reply_screen(await self._screen_for(found.command), event.origin)
            case InboundClass.CONTROL:
                await self._reply((await self._answer_command(found)).text, origin=event.origin)
            case InboundClass.MENU_PICK:
                await self._menu_pick(found, origin=event.origin, pressed_on=event.in_reply_to)
            case InboundClass.DELEGATION:
                await self._delegated_turn(found, origin=event.origin)
            case InboundClass.ANSWER_RELAY:
                await self._relay_inbound(found, origin=event.origin, message_id=event.message_id)
            case InboundClass.APPROVAL_RELAY:
                await self._approve_inbound(found, origin=event.origin, message_id=event.message_id)
            case InboundClass.UNKNOWN:
                await self._reply(found.reply, origin=event.origin)
        if found.kind in (InboundClass.ANSWER_RELAY, InboundClass.APPROVAL_RELAY):
            assert found.target is not None  # the router sets one for every Relay
            _log.info(
                "handled inbound Companion Channel message kind=%s target=%s",
                found.kind,
                found.target,
            )
        else:
            _log.info("handled inbound Companion Channel message kind=%s", found.kind)

    async def _screen_for(self, command: str) -> MenuScreen:
        """The screen one of `SCREEN_VERBS` opens, or a refusal in its own words.

        Only the assistant's can refuse — it is the one screen whose making
        reaches a seam — and a refusal is text and never an Anchor (ADR 0021 §2).
        """
        if command == str(Action.SESSIONS):
            return await self.sessions_screen()
        if command == str(Action.ASSISTANT):
            try:
                return await self.open_assistant()
            except BridgeCoreError as refusal:
                return MenuScreen(text=str(refusal))
        return self.config_screen()

    async def _delegated_turn(self, found: Classification, *, origin: str) -> None:
        """One turn handed to a coding model: the top-level `>`, or a conversation's.

        **The dispatch loop is not held while the model thinks.** A turn is
        bounded only by `delegated_turn_timeout_seconds` — five minutes by
        default — and awaiting one here made every other event wait behind it:
        a Stop Notice for a Session that just hit a permission prompt, a numeral
        verdict, an Answer Relay, a `/status` (#268). So the turn is handed to
        `DelegatedTurns`, which runs it as a task and raises
        `DelegatedTurnFinished` on this hub's own queue when it ends; the answer
        is sent from `_turn_finished`, on the serial loop, where every other
        piece of state is written (ADR 0001).

        **Replies to a conversation whose turn is running are queued, in order,
        and each becomes its own turn** (ADR 0021 §7, amended by #268). No cap,
        no coalescing, no refusal: the user may send several messages and each
        is answered in the order typed. Two conversations run at the same time.
        The top-level `>` is one more conversation — the nameless one — so two
        of them are sequential as well: each runs on its own fresh thread, one
        after another, and never as two approval-free agents at once.

        Legacy (ADR 0010): `legacy@1d32845` has no assistant of any kind — no
        model the user talks to, and no delegated turn. This is **adapted** from
        this generation's own Delegated Turn
        (`adapters/call/realtime/adapter.py::delegate`), which is unchanged
        under it: the same instructions, the same action set, the same model
        setting. What is new is that the thread it runs on can be named, kept,
        and resumed (ADR 0021 §7), and that the turn runs beside the loop.
        """
        if self._delegate is None:
            await self._reply(NO_DELEGATE_HANDLER, origin=origin)
            return
        self.turns.submit(found, origin)

    async def _run_turn(self, found: Classification) -> DelegatedAnswer:
        """Run one turn on whatever is wired to answer it. `DelegatedTurns` calls this."""
        if self._delegate is None:  # unreachable: `_delegated_turn` refuses before submitting
            return DelegatedAnswer(text=NO_DELEGATE_HANDLER)
        return await self._delegate(found)

    async def _turn_finished(self, event: DelegatedTurnFinished) -> None:
        """A Delegated Turn ended. Answer it, and let the next reply on its thread go.

        **The two kinds of turn differ only in what the answer is.** A `>` is one
        shot with no Session context and no thread to continue, so its answer
        names no target this hub can see and registers no row (ADR 0021 §3). A
        turn on an Assistant Conversation's thread answers *as* that
        conversation: the answer is an Anchor of the same thread, so a reply to
        it continues the conversation, and a split answer is an Anchor in every
        part of itself, which the send site does by entering the row under every
        id it landed on.

        **The answer is whatever came back, including a refusal.** The one
        outcome that is not words is a thread that cannot be resumed: the
        conversation has ended, so Core says its own fixed hint **once**, drops
        the conversation's rows *and* whatever was typed behind it — every one
        of those replies would earn this same hint — and anchors nothing. A
        reply to the hint is words with an unknown Anchor.

        The next queued turn starts only once this answer has left, so the
        answers arrive in the order the replies were typed and not merely the
        turns. It starts even if the send raised: a queue stopped on a failed
        push would leave every reply behind it unanswered, with nothing to say
        so.
        """
        found = event.found
        if not found.thread_id:
            try:
                await self._reply(event.answer.text, origin=event.origin)
            finally:
                # The nameless conversation is released like any other: a second
                # `>` typed while this one ran is waiting on it (`core/turns.py`).
                self.turns.ended(THE_NAMELESS_ONE)
            return
        if event.answer.thread_gone:
            # **The rows go with the conversation** (`AnchorTable.drop`'s own
            # case: "a thread that ended"). Left in place they would still be
            # the newest Anchor, so the next thing typed in the chat — meant
            # for a Session — would be classified as a turn on a thread that is
            # gone, and answered with this hint again, and again.
            self.anchors.drop(found.thread_id)  # and the queue behind it, with the rows
            await self._reply(briefing.ASSISTANT_CONVERSATION_GONE_HINT, origin=event.origin)
            return
        try:
            await self._reply(
                event.answer.text,
                origin=event.origin,
                anchor=menu.assistant_answer(found.thread_id),
                reply_bar=briefing.ASSISTANT_REPLY_PLACEHOLDER,
            )
        finally:
            self.turns.ended(found.thread_id)

    async def _answer_command(self, found: Classification) -> ControlAnswer:
        """One control-plane command, answered by the surface wired to this hub.

        A hub with no surface refuses in its own words: an answer, and not a
        successful one — nothing ran.
        """
        if self._control is None:
            return ControlAnswer(NO_CONTROL_SURFACE, ok=False)
        return await self._control(found)

    async def _run(self, action: Action, arguments: str = "", *, origin: str) -> bool:
        """A press that means a control-plane verb: run it at once and answer with its words.

        `verify`, `live` and the switch flip are the same actions typed as
        `/verify` and the rest would be, so they go through the same surface
        and come back in the same words — as a toast, when the press is still
        waiting for one (ADR 0021 §4). Neither answer is an Anchor.

        Reports whether the action ran, because a caller may owe the user
        something more when it did — and nothing at all when it did not
        (`_flip_pick`, #266). The words are the same either way, and the answer
        the surface gave is the one thing that tells them apart.
        """
        found = Classification(kind=InboundClass.CONTROL, command=str(action), text=arguments)
        answer = await self._answer_command(found)
        await self._reply(answer.text, origin=origin)
        return answer.ok

    async def _reply_screen(self, screen: MenuScreen, origin: str) -> None:
        """Send one menu screen as the answer to what opened it, registering its row."""
        await self._reply(
            screen.text,
            origin=origin,
            notice=screen.notice,
            anchor=screen.anchor,
            reply_bar=screen.reply_bar,
        )

    async def _menu_pick(
        self, found: Classification, *, origin: str = "", pressed_on: str = ""
    ) -> None:
        """A numeral on a menu screen: what the row says that position stands for (#264).

        Read off the row's `picks`, never the label's text (ADR 0021 §6). Which
        screen it was decides what the pick is: a Session on the roster, a
        menu word on the config screen or a greeting, a switch name on the
        switch screen. A pick whose Session the roster no longer holds is
        refused in `resolve`'s own words — the refusal `relay` gives.
        """
        assert found.anchor is not None and found.position  # the router sets both
        row = found.anchor
        pick: AnchorPick | None = row.picks[found.position - 1] if row.picks else None
        match row.target:
            case Screen.ROSTER:
                assert isinstance(pick, SessionTarget)
                await self._greet(pick, origin=origin)
            case Screen.CONFIG:
                await self._config_pick(pick, origin=origin)
            case Screen.SWITCHES:
                await self._flip_pick(str(pick), origin=origin, pressed_on=pressed_on)
            case SessionTarget() as target:
                await self._session_pick(target, pick, origin=origin)
            case _:  # a target with no screen behind it: nothing to pick by number
                await self._reply(briefing.NUMERAL_PICKS_NOTHING_HINT, origin=origin)

    async def _greet(self, target: SessionTarget, *, origin: str) -> None:
        """A Session picked off the roster: its greeting screen, or why it cannot be reached."""
        try:
            session = self._state.sessions.resolve(target)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        await self._reply_screen(menu.greeting_screen(session, self._state.sessions.all()), origin)

    async def _session_pick(self, target: SessionTarget, word: AnchorPick, *, origin: str) -> None:
        """`brief`, `history` or `send message`, about one Session."""
        try:
            session = self._state.sessions.resolve(target)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        match word:
            case MenuWord.BRIEF:
                await self._brief_as_anchor(session, origin=origin)
            case MenuWord.HISTORY:
                # The newest page, in the surface's own rendering; an Anchor of
                # that Session with nothing to pick, so words replying to it
                # are more words for the Session.
                #
                # **The page is the Anchor; a refusal is not** (#264 review).
                # This is the one menu choice answered through the control
                # surface rather than read here, so it is the one that can be
                # handed a refusal, and it used to register a row on one.
                # Words are not the harm — with no row they fall through to the
                # newest Anchor and reach the Session anyway (ADR 0021 §2).
                # What the row cost was a slot: each Session keeps its newest N
                # (`anchor_rows_per_session`), so a failed read could evict a
                # real Stop Notice from the table. A numeral gets the right
                # hint of the two for the same reason.
                found = Classification(
                    kind=InboundClass.CONTROL, command=str(Action.HISTORY), text=str(target)
                )
                answer = await self._answer_command(found)
                await self._reply(
                    answer.text,
                    origin=origin,
                    anchor=Anchor(kind=AnchorKind.HISTORY, target=target) if answer.ok else None,
                )
            case MenuWord.SEND_MESSAGE:
                await self._reply_screen(menu.prompt_screen(session), origin)
            case _:  # a word a greeting never offers; the row's picks are this hub's own
                await self._reply(briefing.NUMERAL_PICKS_NOTHING_HINT, origin=origin)

    async def _brief_as_anchor(self, session: Session, *, origin: str) -> None:
        """That Session's brief, sent as a notice: the same Anchor a Stop Notice is.

        The row's labels come from the reading the brief was made from, never
        from the Session resolved before it: the read is an `await`, the wait
        can move under it, and a row carrying the older wait would offer a
        numeral a label the notice never printed.
        """
        try:
            brief, read = await self._session_brief_now(session.target)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        await self._reply(
            briefing.text(brief),
            origin=origin,
            notice=briefing.notice(brief),
            anchor=_notice_anchor(read.target, read.waiting_for),
        )

    async def _config_pick(self, word: AnchorPick, *, origin: str) -> None:
        """`switch` opens the switch screen; `verify` and `live` run at once."""
        match word:
            case MenuWord.SWITCH:
                await self._reply_screen(self.switches_screen(), origin)
            case MenuWord.VERIFY:
                await self._run(Action.VERIFY, origin=origin)
            case MenuWord.LIVE:
                await self._run(Action.LIVE, origin=origin)
            case _:
                await self._reply(briefing.NUMERAL_PICKS_NOTHING_HINT, origin=origin)

    async def _flip_pick(self, name: str, *, origin: str, pressed_on: str = "") -> None:
        """A press on a switch label means flip — against the board as it stands now.

        The label may be stale: flipped elsewhere since the screen was sent,
        it still says the old state. The row carries the name alone, and the
        state to flip *from* is read here (ADR 0021 §6). The flip itself is the
        `switch` action, so it answers in the words `/switch` would.
        """
        try:
            on = self._state.switches.is_set(name)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        flipped = await self._run(Action.SWITCH, f"{name} {'off' if on else 'on'}", origin=origin)
        # **And the screen is re-sent onto itself** (ADR 0021 §8), so every
        # label shows the state it holds now. Only this screen, only after a
        # flip made through it, and only when the flip actually happened: a flip
        # made anywhere else leaves the label stale, a press on a stale label
        # still means "flip" (§6), and a refused action changed no state for the
        # labels to show.
        if flipped:
            await self._redraw_switches(self.anchors.sent_under(pressed_on))

    async def _redraw_switches(self, ids: tuple[str, ...]) -> None:
        """Re-send the switches brief onto the screen the press was made on.

        **Past every switch, Duty included** (ADR 0002, ADR 0021 §8). This is
        not a correction the system decided to make: it is the answer to a
        control-plane action the user took a moment ago, and the control plane
        is never gated. Gating it on Duty would produce the indefensible case —
        the user flips Duty off from the phone, and the screen they flipped it
        on never shows that it landed.

        A row this table no longer holds is nothing to edit, and the send is
        skipped rather than addressed to nobody.
        """
        if not ids:
            return
        screen = self.switches_screen()
        if screen.notice is not None:
            await self._correct(ids, screen.notice)

    async def _relay_inbound(
        self, found: Classification, *, origin: str = "", message_id: str = ""
    ) -> None:
        """Carry a typed relay in, and answer it with the receipt (ADR 0021).

        **Every inbound relay is answered**, and from the same three facts the
        CLI prints as codes, not only the ones that had to wait. The channel
        used to hear a sentence when the words queued and silence when they
        went, which made "it worked" and "nothing was read" the same
        observation; then it heard the three codes, which is the log feel the
        requirements page rejects; then one sentence per outcome, worded once in
        Core beside `receipt_line`.

        **The sentence is now the exception** (#321, amended ADR 0021). A
        sentence is a message of its own in the chat for news the user already
        expects, so the receipt is a reaction on the message they sent —
        rendered by `_settle`, which owns it for every settlement this Relay
        ever has, not only this first one. What is left here is the case with no
        message to react on: a button press, whose receipt stays the toast it
        has always been. `bridgectl relay` keeps the codes; the Voice keeps
        composing its own sentence from the facts (#175).
        """
        assert found.target is not None  # the router sets one for every ANSWER_RELAY
        try:
            outcome = await self.relays.relay(
                found.target, found.text, message_id=message_id, origin=origin
            )
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        await self._settle(outcome)
        # **Words that arrived are the answer, and they close the notice that
        # asked** (ADR 0021 §8's first cause: answered from Telegram). Words
        # that only queued answered nothing — the Session has not taken the turn
        # they are waiting for, and the question is still the user's to answer
        # from here.
        if outcome.state is Lifecycle.DELIVERED:
            await self._close_open_notices(found.target)
        if not message_id:
            # A receipt names the Session the words went to, so it is an Anchor:
            # a reply to it is more words for that Session (ADR 0021 §2). It
            # offers nothing to pick, and a numeral on it is refused.
            await self._reply(
                receipt_sentence(outcome), origin=origin, anchor=_receipt_anchor(found.target)
            )

    async def _approve_inbound(
        self, found: Classification, *, origin: str = "", message_id: str = ""
    ) -> None:
        """A numeral on a permission notice: the user's verdict, on that dialog (ADR 0021 §6).

        The Approval Relay carries and nothing more (#191): whether the dialog
        is still open is `answer_approval`'s own answer, read off the roster. A
        handle no live row carries is a dialog the keyboard already settled;
        nothing is sent, the reply sends the user to the screen, and the numeral
        is never re-read as words.

        **A verdict earns the same receipt as any other Relay** (#321). A typed
        numeral is a message the user sent and its answer is a `RelayOutcome`,
        so the reaction goes on it exactly as it does for words; a numeral that
        arrived as a *press* has no message and keeps its sentence, which is the
        toast it has always been. The refusals above are sentences either way:
        they are not receipts for a Relay, and there is no standing to show.
        """
        assert found.target is not None and found.verdict is not None
        try:
            outcome = await self.answer_approval(
                found.approval_id, found.verdict, message_id=message_id, origin=origin
            )
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        if outcome is None:
            await self._reply(PERMISSION_ALREADY_SETTLED_HINT, origin=origin)
            return
        if not message_id:
            await self._reply(
                receipt_sentence(outcome), origin=origin, anchor=_receipt_anchor(found.target)
            )

    async def _settle(self, outcome: RelayOutcome) -> None:
        """Render one Relay's standing as the receipt on the user's own message.

        **The receipt is a reaction, and this is the one place it is put on**
        (ADR 0021, amended 2026-09-09; #321). Every site that produces a
        `RelayOutcome` passes through here, so a Relay's emoji is re-rendered
        from its *current* reason at every settlement — set when the words are
        taken, swapped when they go in or when the Session ends under them, and
        resting on the state they finally reached. Re-sending the same one is
        safe, so this is idempotent by construction.

        **The hub's, because only the hub knows which message to react on.** The
        id is the channel's, learned on the `InboundText` the words arrived in
        and carried through the queue on the outcome; a Relay from anywhere else
        — the CLI, the Voice, a button — has none, and nothing is rendered for
        it. Its receipt is the sentence its own caller sends, as before.

        **What each reason wears, and which sentences are sent, is Core's
        table** (`core/relays.py`): three outcomes are news the user must act on
        and carry a sentence whatever the reaction did; the rest are the
        reaction alone. A reaction the channel refused sends that reason's own
        sentence instead, once — so a receipt is never absent, and it is never
        retried, because the fallback already told the user what the emoji would
        have.
        """
        if outcome.state is Lifecycle.REPORTED_FAILED:
            _log.info(
                "the user's words for %s will never arrive (reason=%s grade=%s)",
                outcome.target,
                outcome.reason,
                outcome.grade,
            )
        if not outcome.message_id:
            return
        moved, stood = await self._render_reaction(outcome)
        if not moved:
            # This standing has already had its receipt — the emoji, or the
            # sentence that stood in for one the surface refused. A late receipt
            # that repeats a grade is not news, and saying it again is how one
            # Relay becomes two identical messages about it.
            return
        if sentence_stands_alone(outcome.reason) or not stood:
            # A reply rather than a push: this is the answer to words the user
            # sent, so it goes back the way they came (ADR 0021 §4) and is never
            # gated (ADR 0002). It hangs under the message those words were
            # typed in, so a chat holding several Relays says which one this
            # sentence is about; and it names the Session, so it is an Anchor
            # like every receipt.
            await self._reply(
                receipt_sentence(outcome),
                origin=outcome.origin,
                anchor=_receipt_anchor(outcome.target),
                reply_to=outcome.message_id,
            )

    async def _render_reaction(self, outcome: RelayOutcome) -> tuple[bool, bool]:
        """Put this standing's emoji on the message. Answers two facts.

        Whether the standing **moved** — a settlement at the standing already
        rendered asks the surface nothing and owes the user nothing — and, when
        it did, whether the reaction **stands**, which is what decides between
        the emoji and the sentence that stands in for it.

        **The state is tracked here because the bot cannot read it back.** A
        reaction in a private chat is not something Telegram will report, so a
        swap could only ever be made from a state this engine remembers; the
        table holds the emoji last *rendered* per Relay — set, or attempted and
        refused — and a standing that has not changed costs no call and no
        second sentence. It is the Relay row's memory kept one step
        outward, because the row is released before the last settlement renders
        it (`core/relays.py::RelayPipeline._report_failed`).

        **The entry lives exactly as long as the queue row does.** It is dropped
        when the pipeline has released the Relay — the words went in, the
        Session went, the question was refused before the wire — and kept while
        the queue still holds it, which includes the two standings that are
        terminal for *sending* and not for the entry: words parked in front of a
        person, and words nobody can vouch for. Both stay in the queue, so both
        can settle again on a late receipt, and an entry dropped on the reason
        would make that second settlement re-render an emoji already standing
        and say a sentence already said. What is left when it does go is the
        emoji resting on the message, which is the receipt.

        Not persisted, like the queue it shadows — an engine that restarts
        leaves whatever it last set standing there, and no reader here rebuilds
        it (#321: the Relay queue is not durable either, and persistence is not
        reopened by this).
        """
        wanted = reaction_for(outcome.reason)
        # **Never rendered and rendered as nothing are different.** A Relay this
        # table has no row for has had no receipt at all, and a row holding
        # `None` is one whose standing wears no emoji and has already been
        # answered in words. Reading the absence as `None` would swallow the
        # first receipt of the two standings that wear nothing.
        rendered = self._relay_reactions.get(outcome.request_id)
        moved = outcome.request_id not in self._relay_reactions or rendered != wanted
        stood = True
        if moved and not (wanted is None and rendered is None):
            # There is an emoji to set, or one standing to take away. A standing
            # that wears none, on a message that wears none, asks the surface
            # for nothing.
            stood = await self._channel.react(outcome.message_id, wanted)
            if not stood:
                _log.info(
                    "the receipt for relay %s could not be a reaction, so it is a sentence: "
                    "reason=%s",
                    outcome.request_id,
                    outcome.reason,
                )
        if moved:
            # Recorded whether or not it stood, because "not retried" is what
            # the fallback buys: a standing that has been attempted has had its
            # receipt, in the emoji or in the sentence.
            self._relay_reactions[outcome.request_id] = wanted
        if not self._state.relays.holds(outcome.request_id):
            self._relay_reactions.pop(outcome.request_id, None)
        return moved, stood

    async def _push(
        self, text: str, *, notice: Notice | None = None, anchor: Anchor | None = None
    ) -> None:
        """One Companion Channel push, under the Message Switch. The only outlet left.

        What remains of the escalation pipeline (#195). The route matrix,
        open-and-speak and the two call routes went with it: opening a call is
        the Call Keeper's and it briefs from a fresh reading, and speaking into
        one is mid-call behaviour (#196). So there is one outlet here, one
        attempt at it, and no replay — Stop-Notice no-loss is the current-state
        reading `_an_outlet_opened` takes, never a historical notice re-sent.

        **Adjudicated, unlike `_reply`.** This is the system reaching the user
        unbidden, which is exactly what the Message Switch answers for; a reply
        to text the user just sent is not (ADR 0002).
        """
        if not text:
            return
        if not self.adjudicator.may_push():
            _log.info("the Message Switch is off; this notice reaches no outlet")
            return
        receipt = await self._send(text, notice=notice, anchor=anchor)
        if receipt.is_delivered:
            return
        _log.info(
            "notice not delivered; this attempt is not replayed (%s: %s)",
            receipt.outcome,
            receipt.reason,
        )

    async def _close_open_notices(
        self, target: SessionTarget, *, close_questions: bool = True
    ) -> None:
        """Edit every notice this Session left open to the one closed word (ADR 0021 §8).

        **One fact closes them, and the edit does not distinguish which.**
        Answered from Telegram, a permission handed back, the Session ended or
        gone from the roster — each reaches here, and the edit shows none of
        them, because what it says is that the decision is no longer answerable
        from this surface and not how that came about.

        **`close_questions=False` leaves a question's notice as sent** (#323): the one
        caller that passes it is the Reply Window closing, where an open question
        notice means the user answered at the terminal — a stop they resolved
        themselves, whose record needs no edit. The reasoning for why that caller
        may read the cause off the notice is on the call site. Every other caller
        knows a fact that closes a question too, and takes the default.

        Every open notice of the Session, not just the newest: two questions can
        be on screen at once and a fact that closes one closes both. A row whose
        brief carried no decision is left alone — a `finished` notice records a
        turn that ended, and there is nothing on it to close.

        **What is closable is read off the state, not the question line** (#324).
        A Session that ends its turn on a question it typed in prose is briefed
        `waiting for your decision` with an empty question slot — there is a
        decision open and no structured ask to word it with — and the old test,
        a non-empty question line, left exactly that notice 🟡 for ever: answered
        from Telegram, or raised by a Session that has since died, nothing ever
        edited it. The state is the honest reading of "the user is being asked
        to settle this", and it is the same reading `close_questions` already
        makes below.

        **Duty, and not the Message Switch.** An edit is not a push: it notifies
        nobody and only settles a message the user already has, so it obeys the
        master switch like every unbidden act toward the user, and outlives the
        Message Switch going off — a notice already out still has to close, or
        its stale buttons invite a press that earns only a refusal.

        **Duty off leaves the row open**, where a failure closes it. Nothing was
        attempted, so nothing was spent: the next fact about this Session — it
        ends, it leaves the roster — closes the notice then, with the switch back
        on. A refusal is not a failed attempt, and only an attempt is spent once.
        """
        if not self.adjudicator.may_correct():
            _log.info("the Duty Switch is off; a closed notice is left as it was sent")
            return
        for sent in self.anchors.open_notices(target):
            notice = sent.anchor.notice
            if not isinstance(notice, SessionNotice) or notice.state not in ASKING_STATES:
                continue
            # A question's notice and a permission's both carry a question line,
            # so the brief's own state is what tells them apart (#323).
            if not close_questions and notice.state is BriefState.DECISION:
                continue
            # Said before the attempt, not after: an edit is never retried, so a
            # row that has been attempted is done whichever way it went.
            self.anchors.mark_handled(sent.ids)
            await self._correct(sent.ids, _handled(notice))

    async def _correct(self, ids: tuple[str, ...], notice: Notice) -> None:
        """Rewrite messages already sent (ADR 0021 §8). One attempt, never repeated.

        **Ungated here, deliberately**, because the two callers answer to
        different switches and neither answer belongs to the mechanism: closing
        a notice obeys Duty (`_close_open_notices`), while the switches screen
        re-sent onto itself passes every switch, being the answer to a
        control-plane action (`_redraw_switches`, ADR 0002). A gate written here
        would have to be right for both, and there is no such gate.

        **Failure is logged and dropped**: an edit Telegram rejects, or a row
        that a restart left behind, gets no retry and no fresh message — a
        second copy of a notice whose decision has closed is worse than a stale
        one. "Message is not modified" is the adapter's success, not a failure.

        No text goes beside the brief. A surface that cannot lay a notice out
        ignores `revises` and edits nothing (the null channel), so the words a
        text would carry reach nobody by construction; the brief is the message
        here.
        """
        receipt = await self._send("", revises=ids, notice=notice)
        if receipt.is_delivered:
            return
        _log.info(
            "a closed notice was not edited; it is left as it was sent (%s: %s)",
            receipt.outcome,
            receipt.reason,
        )

    async def _reply(
        self,
        text: str,
        *,
        origin: str = "",
        notice: Notice | None = None,
        anchor: Anchor | None = None,
        reply_bar: str = "",
        reply_to: str = "",
    ) -> None:
        """Answer text the user sent. **Never gated** — a reply is not a push.

        ADR 0002 is absolute, and the Companion Channel is one of the surfaces it
        names. Gating this would gate the one way to flip Duty back on from away
        from the computer, using the switch that is off.

        **One attempt, graded, and never sent again** (P15, #61 C2). The
        reference implementation settled every outbound attempt to `sent`,
        `failed`, `indeterminate` or `suppressed` and refused to resend an
        indeterminate one, because a duplicate notification costs the user more
        than a missing one (`legacy@1d32845:bridge/channel.py:11-13,75-86`;
        `legacy@1d32845:bridge/daemon.py:830-879`). That rule is **ported**. Its
        storage is **simplified**: legacy wrote the grade to a durable ledger
        (`legacy@1d32845:bridge/store.py:1517-1614`) and this is a direct answer
        to text the user just sent, so the grade is said here and forgotten. It
        does not enter the Answer Relay queue either — it is a direct reply, and
        queueing it would replay an answer to a question the user asked minutes
        ago when its Reply Window next opened.

        **The words never enter the diagnostic.** The reply carries whatever the
        user's own business is; the log carries the grade and the adapter's
        reason, and the Telegram adapter already guarantees its token appears in
        no error message (`telegram/api.py`).
        """
        if not text:
            return
        receipt = await self._send(
            text,
            origin=origin,
            notice=notice,
            anchor=anchor,
            reply_bar=reply_bar,
            reply_to=reply_to,
        )
        if receipt.is_delivered:
            return
        _log.warning(
            "the reply to the user was not delivered (%s: %s); it is not sent again",
            receipt.outcome,
            receipt.reason,
        )

    async def _send(
        self,
        text: str,
        *,
        origin: str = "",
        revises: tuple[str, ...] = (),
        notice: Notice | None = None,
        anchor: Anchor | None = None,
        reply_bar: str = "",
        reply_to: str = "",
    ) -> ChannelReceipt:
        """One Companion Channel send, the one record every send writes — and its Anchor.

        **Every send says what a reply to it means, or that it means nothing**
        (ADR 0021 §2). `anchor` is the row this message registers under every
        id it lands on, stamped here with the moment it was sent; `None` is a
        message that names no target — a `/status` answer, a refusal, a `>`
        answer — and registers nothing. Registering inside the one send site is
        what stops a new outbound kind (#264's prompt and menus, #265's answers)
        from being sent without its row and silently routing replies to the
        newest Anchor instead. An UNKNOWN receipt from a split send that half
        landed still names the parts that did, and those are Anchors too; a
        send that landed nowhere registers nothing.

        **The only place `_channel.send` is called**, so that the record below
        is written for every send there is — a push and a reply alike — and
        for every channel there is, the null one included. ADR 0021 §10 makes
        it part of the acceptance contract, beside the #48 inbound line: a
        Stop Notice is unbidden, so this line is the only way an outside
        observer learns which provider ids it landed under, and which Session
        it was about. The format is therefore fixed: `key=value`, ids
        comma-joined in sending order, empty when nothing landed. An UNKNOWN
        receipt from a split send still names the parts that did land — those
        are messages the user can reply to.

        **`target=` is whoever this message is about**, rendered exactly as the
        roster row and the `Session stopped:` line render it — `str` on the
        Anchor's target, which is `agent:session_id[:pid]` for a Session, the
        screen's own name for a menu screen that is nobody's, and the thread id
        for an Assistant Conversation. A send with no Anchor names nobody and
        writes the field empty. One engine bridges every Session on the machine,
        so an observer reading this log has no other way to tell a message about
        *its* Session from a message about someone else's — and reading the
        newest send line as one's own is exactly how a Stop Notice for another
        lane's Session came to be graded as this lane's turn (#355).

        `origin` is echoed from the inbound event when this is a reply, and
        empty for an unbidden push. `notice` is the structured brief the text
        renders, for a push that is one (ADR 0021 §5) and for a reply that is
        a menu screen or a re-sent brief (§6); a receipt or a refusal carries
        none. `reply_bar` is Core's placeholder words for a message that asks
        for words back — the `Say to <name>:` prompt, an Assistant
        Conversation's messages — and a courtesy, never the routing (§7).
        `reply_to` is the user's own message this one answers, empty for
        everything that answers no message: a receipt sentence hangs under the
        words it is about (ADR 0021, *a receipt is a reaction*), and nothing
        else does.

        `revises` names the messages this send replaces rather than adds to (ADR
        0021 §8): a send that revises registers nothing, because the row it edits
        is already in the table and re-registering it would make an old notice
        the newest Anchor again.

        **The row keeps the brief it was sent as** (§8, #266). It is the only
        record of what that message says, and the edit that closes it re-fills
        that brief rather than composing a new one — so it is stamped onto the
        row here, at the one site that holds both.
        """
        request_id = new_request_id()
        receipt = await self._channel.send(
            text,
            request_id=request_id,
            origin=origin,
            revises=revises,
            notice=notice,
            reply_bar=reply_bar,
            reply_to=reply_to,
        )
        _log.info(
            "sent Companion Channel message request=%s outcome=%s target=%s message_ids=%s",
            request_id,
            receipt.outcome,
            "" if anchor is None else anchor.target,
            ",".join(receipt.message_ids),
        )
        if anchor is not None and not revises:
            self.anchors.register(
                receipt.message_ids, replace(anchor, sent_at=self._stamp(), notice=notice)
            )
        return receipt

    def _spawned(self, target: SessionTarget) -> bool:
        """Whether the roster **positively says** this is a Child Process (#79).

        A Child Process is seen, never spoken to — and never spoken *about*: a
        Stop Notice names a Session the user is invited to answer, and the
        answer to a child is refused. Suppressing the announcement is therefore
        the same rule as refusing the Relay, said one step earlier so the user
        is never asked for something the system will not carry.

        **Asked of `resolve`, so there is one definition.** The registry is the
        only thing that decides what a child is (`core/sessions.py`), and it
        already refuses one by raising. Re-deriving the test here would be a
        second answer to a question that has one.

        **Unknown is not child, and the asymmetry is deliberate.** Discovery
        runs on a cadence, so a Session can stop before the roster holds a row
        for it; reading that silence as "child" would drop the one notice the
        engine exists to send. A child wrongly announced costs one message about
        something that is refused anyway — the cheaper mistake by far.

        It is here rather than in a lane because a lane raising the event is not
        wrong: a Codex subagent thread really does leave `active`, and its
        adapter really does watch every thread the daemon holds. What the hub
        does with that is the hub's.
        """
        try:
            self._state.sessions.resolve(target)
        except ChildSessionError as spawned:
            # Said out loud, because a notice that was never sent is otherwise
            # indistinguishable in the log from one that failed to reach anybody.
            _log.info("%s is a Child Process, so nothing is announced about it", spawned.target)
            return True
        except BridgeCoreError:
            return False
        return False
