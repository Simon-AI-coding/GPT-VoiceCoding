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
from collections.abc import Awaitable, Callable, Mapping
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
from gpt_voicecoding.core.call_keeper import CallKeeper
from gpt_voicecoding.core.clock import Clock, default_clock, wall_clock
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    CallInstructionsMissing,
    ChildSessionError,
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
    RelayReason,
    reason_for,
    receipt_sentence,
)
from gpt_voicecoding.core.router import Classification, InboundClass, InboundRouter, TextGrammar
from gpt_voicecoding.core.sessions import Session, SessionRegistry, UndeliveredRelay
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import SwitchSnapshot
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
    CallDropped,
    CallEnded,
    CallSnapshot,
    CallStarted,
    Dial,
    HandoverItem,
    SpokenBrief,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.companion_channel import (
    ChannelReceipt,
    CompanionChannel,
    InboundText,
    Notice,
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


#: What `assistant` answers until the Assistant Conversation is built (#265).
#: The verb is in the shared set now (ADR 0021 §6) so the command menu can name
#: it and one parser accepts it; the conversation behind it is that issue's.
ASSISTANT_NOT_BUILT = "the assistant is not on this engine yet"

#: The two menu verbs this hub answers with a screen of its own (ADR 0021 §6,
#: #264) rather than through the control-plane surface: a screen is an Anchor,
#: and the row is registered inside the one send site, which the surface's
#: text answer cannot reach. The same two verbs answer `bridgectl` in text
#: through `sessions_screen` / `config_screen`, so there is one screen apiece.
SCREEN_VERBS: frozenset[str] = frozenset({str(Action.SESSIONS), str(Action.CONFIG)})

#: Answers an inbound delegation when no Delegated Turn handler is wired.
NO_DELEGATE_HANDLER = "I can't take a delegated turn right now — nothing is wired to answer it"

#: The two lines a run is told the Voice held its call open by. Fixed strings
#: rather than a formatted one, because the acceptance step matches them: an
#: engine that says nothing when the ceiling is held leaves a whole-lane run no
#: way to tell "the call outlived the ceiling" from "the ceiling never ran"
#: (#184). One per edge, never per delta — a long answer is hundreds of those.
VOICE_SPEAKING_LINE = "the call's own Voice started speaking"
VOICE_QUIET_LINE = "the call's own Voice stopped speaking"

#: Why a call the *system* dialled exists. The items after it are the roster and
#: the Sessions waiting, so this says what they are for and nothing they say.
SYSTEM_DIALLED = (
    "This call was dialled because Sessions need the user. "
    "What follows is the roster and each Session that is waiting; "
    "speak from it, and do not invent anything it does not say."
)


def stop_brief(
    session: Session,
    waiting_for: WaitingFor,
    *,
    progress: ProgressObservation | None = None,
    question_answerable: bool = False,
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
    return briefing.session(row, question_answerable=question_answerable)


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

    def handover(self) -> tuple[HandoverItem, ...] | None:
        """What a system-dialled call comes up holding, or None if nobody needs the user.

        **Every row, including the one that just stopped.** Since #213 the
        registry holds a just-stopped Session as stopped, so a fresh reading
        covers the Session that provoked the dial like any other and nothing is
        passed in beside the roster (`core/briefing.py::handover`).

        **"Nobody needs the user" is read off the hand-over itself.** A call is
        worth dialling when there is a Session Brief to carry — a Session that
        stopped, whatever it stopped on — and the one place that decides which
        rows earn a brief is `briefing.handover` (every live main row that is
        not `RUNNING`). Asking it and then looking at what came back keeps that
        rule in one place; re-deriving it here would be a second answer that
        drifts, which is how a call came up saying a Session needed the user and
        never mentioning which (#209).

        `None` and not an empty tuple: a reason and a roster count with no brief
        behind them is still a call, and the distinction is the one the Keeper
        acts on — a Cool-down that elapses onto a machine where every wait has
        since been answered at the terminal ends in silence.
        """
        sessions = self._sessions.live()
        items = briefing.handover(
            sessions,
            self._sessions.focus,
            reason=SYSTEM_DIALLED,
            answerable=tuple(
                session.target for session in sessions if self._answerable_for(session)
            ),
        )
        if not any(isinstance(item, SpokenBrief) for item in items):
            return None
        return items

    def focus_brief(self) -> SpokenBrief | None:
        """The Focus Session as it stands now, or None if it is past needing the user.

        The mid-call half of the same seam (#196), read on the same terms as
        `handover` and by the same rule: **a Session earns a brief when its
        roster row is not `RUNNING`**, and that rule lives in one place. So the
        row is taken from `briefing.roster` — which is also what settles the two
        edges a target lookup would have to answer for itself, an exited Session
        and a Child Process appearing nowhere (#165 Q7).

        `None` where there is no Focus Session, where its row has gone, and
        where the row says it is running again: the wait that armed the word may
        have been answered at the terminal while the Voice was mid-sentence, and
        all three of those are the same silence.
        """
        focus = self._sessions.focus
        if focus is None:
            return None
        sessions = self._sessions.live()
        row = next(
            (row for row in briefing.roster(sessions, focus).rows if row.target == focus), None
        )
        if row is None:
            return None
        session = next((live for live in sessions if live.target == focus), None)
        if session is None:  # pragma: no cover - the roster read it out of this list
            return None
        if not briefing.earns_a_brief(session):
            return None
        return briefing.spoken(
            briefing.session(session, question_answerable=self._answerable_for(session))
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
        delegate: Callable[[Classification], Awaitable[str]] | None = None,
        inventory: tuple[SeamLoad, ...] = (),
        instruction_context: InstructionContext | None = None,
    ) -> None:
        self._state = state
        self._call = call
        self._channel = channel
        self._agents = dict(agents)
        self._events = events if events is not None else EventQueue()
        self._policy = policy or CorePolicy()
        self._control = control
        self._delegate = delegate
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
            agents=agents,
            sessions=state.sessions,
            relays=state.relays,
            policy=self._policy,
            clock=clock,
        )
        #: The Anchor Table (ADR 0021 §2): memory only, never persisted, empty
        #: after every restart. Held here beside the router that reads it and
        #: the send path that fills it, and deliberately **not** on
        #: `BridgeState`, whose one job is deciding what is durable.
        self.anchors = AnchorTable(rows_per_target=self._policy.anchor_rows_per_session)
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
        the UNREADABLE state or an unreadable `newest`, which is the honest
        answer and the one the five states were drawn to carry.
        """
        if target is None:
            return briefing.roster(self._state.sessions.all(), self._state.sessions.focus)
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
        self._state.persist()
        opened = self.adjudicator.outlets() - opened_before
        if opened:
            await self._an_outlet_opened(voice=Outlet.VOICE in opened)
        return previous

    async def live_toggle(self) -> CallSnapshot:
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
        return await self.keeper.live_toggle()

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
        self, approval_id: str, verdict: ApprovalVerdict
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
        )
        # An Approval Relay is the user's own words arriving too (#165 Q2 sets
        # the focus from it for that reason), so a verdict that lands clears
        # whatever the last Relay that did not land left on the row.
        await self._settle(outcome)
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

    async def open_assistant(self) -> str:
        """`assistant`: refused until the Assistant Conversation is built (#265)."""
        raise BridgeCoreError(ASSISTANT_NOT_BUILT)

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

    async def tick(self) -> tuple[RelayOutcome, ...]:
        """Advance the time-driven ceilings. The composition root calls this on a timer.

        Deliberately the only time-driven thing in the hub, and there are two of
        them left: the undelivered Relay ceiling, which is this method's, and
        the Call Keeper's own clock — Cool-down expiry, the Silence Ceiling and
        the settle window — which is handed the same instant and decides for
        itself what is due. Stop Notices are not replayed here.

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
        expired = self.relays.sweep_expired()
        for outcome in expired:
            await self._settle(outcome)
        if not self.events.unread(UserSpeech, UserSpeaking, VoiceSpeech):
            await self.keeper.tick(self._clock())
        return expired

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
                self._relay_receipt(event)
            case CallStarted() | CallEnded() | CallDropped():
                # The Keeper owns every one of these: it adopts a call the user
                # opened, releases the one it held, paces the Cool-down that
                # follows any end, and plays the two cues. The hub records
                # nothing here, because "the call is up" has one truth in core.
                await self.keeper.heard(event)
            case InboundText():
                await self._inbound_text(event)
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
        )
        await self._announce_waiting(
            session,
            event.target,
            event.waiting_for,
            progress=event.progress,
        )
        # **One wake per wake-worthy event, and it carries no content.** Whether
        # this Session still needs the user is read again by the Briefer at the
        # moment the Keeper acts (ADR 0017); `focus` says only whether the event
        # concerns the Focus Session, which is #196's to read.
        await self.keeper.wake(focus=self._state.sessions.focus == event.target)

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
        brief = stop_brief(
            session,
            waiting_for,
            progress=progress,
            question_answerable=(
                waiting_for.kind is WaitingKind.QUESTION and self._question_answerable(target)
            ),
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
        try:
            self._state.sessions.mark_ended(event.target)
        except BridgeCoreError:
            _log.info("a Session ended that was never registered: %s", event.target)
        self._state.persist()
        for outcome in self.relays.session_ended(event.target):
            await self._settle(outcome)
        # Rows go with their Session (ADR 0021 §2). Editing its open notices to
        # `handled` and pushing the ended line come before this, and are #266's.
        self.anchors.drop(event.target)

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

    def _relay_receipt(self, event: RelayReceipt) -> None:
        """A receipt that arrived after the call returned. The ledger records it.

        **And a late proof of delivery clears the row's `undelivered` too**
        (#197). The field says what the last Relay that did not arrive was, and
        a receipt proving one did arrive is exactly the news that ends it — it
        makes no difference to the user whether the proof came back inside the
        call or minutes later on the Claude inbox's own acknowledgement route
        (ADR 0013).
        """
        try:
            classified = self._state.relays.classify(event.receipt.request_id, event.receipt)
        except UnknownRelayError:
            _log.info("a receipt arrived for a Relay that is no longer pending")
            return
        if event.receipt.is_delivered:
            self._fold_undelivered(classified.target, None, relay=event.receipt.request_id)

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
                await self._menu_pick(found, origin=event.origin)
            case InboundClass.DELEGATION:
                # Not an Anchor: a top-level `>` is a one-shot Delegated Turn with
                # no Session context (ADR 0021 §3), so its answer names no target
                # this hub can see. #265's Assistant Conversation answers do.
                await self._reply(
                    await self._delegate(found) if self._delegate else NO_DELEGATE_HANDLER,
                    origin=event.origin,
                )
            case InboundClass.ANSWER_RELAY:
                await self._relay_inbound(found, origin=event.origin)
            case InboundClass.APPROVAL_RELAY:
                await self._approve_inbound(found, origin=event.origin)
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
        """The screen one of `SCREEN_VERBS` opens."""
        if command == str(Action.SESSIONS):
            return await self.sessions_screen()
        return self.config_screen()

    async def _answer_command(self, found: Classification) -> ControlAnswer:
        """One control-plane command, answered by the surface wired to this hub.

        A hub with no surface refuses in its own words: an answer, and not a
        successful one — nothing ran.
        """
        if self._control is None:
            return ControlAnswer(NO_CONTROL_SURFACE, ok=False)
        return await self._control(found)

    async def _run(self, action: Action, arguments: str = "", *, origin: str) -> None:
        """A press that means a control-plane verb: run it at once and answer with its words.

        `verify`, `live` and the switch flip are the same actions typed as
        `/verify` and the rest would be, so they go through the same surface
        and come back in the same words — as a toast, when the press is still
        waiting for one (ADR 0021 §4). Neither answer is an Anchor.
        """
        found = Classification(kind=InboundClass.CONTROL, command=str(action), text=arguments)
        await self._reply((await self._answer_command(found)).text, origin=origin)

    async def _reply_screen(self, screen: MenuScreen, origin: str) -> None:
        """Send one menu screen as the answer to what opened it, registering its row."""
        await self._reply(screen.text, origin=origin, notice=screen.notice, anchor=screen.anchor)

    async def _menu_pick(self, found: Classification, *, origin: str = "") -> None:
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
                await self._flip_pick(str(pick), origin=origin)
            case SessionTarget() as target:
                await self._session_pick(target, pick, origin=origin)
            case _:  # an Assistant Conversation's row (#265) offers nothing to pick
                await self._reply(briefing.NUMERAL_PICKS_NOTHING_HINT, origin=origin)

    async def _greet(self, target: SessionTarget, *, origin: str) -> None:
        """A Session picked off the roster: its greeting screen, or why it cannot be reached."""
        try:
            session = self._state.sessions.resolve(target)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        await self._reply_screen(menu.greeting_screen(session), origin)

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

    async def _flip_pick(self, name: str, *, origin: str) -> None:
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
        await self._run(Action.SWITCH, f"{name} {'off' if on else 'on'}", origin=origin)

    async def _relay_inbound(self, found: Classification, *, origin: str = "") -> None:
        """Carry a typed relay in, and answer it with the receipt as one sentence.

        **Every inbound relay is answered**, and from the same three facts the
        CLI prints as codes, not only the ones that had to wait. The channel
        used to hear a sentence when the words queued and silence when they
        went, which made "it worked" and "nothing was read" the same
        observation; then it heard the three codes, which is the log feel the
        requirements page rejects. It hears `receipt_sentence` now (ADR 0021,
        Receipts): the same facts, worded once in Core beside `receipt_line`,
        with ADR 0013's clause when the words went without the user's
        authority. `bridgectl relay` keeps the codes; the Voice keeps composing
        its own sentence from the facts (#175).
        """
        assert found.target is not None  # the router sets one for every ANSWER_RELAY
        try:
            outcome = await self.relays.relay(found.target, found.text)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        await self._settle(outcome)
        # A receipt names the Session the words went to, so it is an Anchor: a
        # reply to it is more words for that Session (ADR 0021 §2). It offers
        # nothing to pick, and a numeral on it is refused.
        await self._reply(
            receipt_sentence(outcome), origin=origin, anchor=_receipt_anchor(found.target)
        )

    async def _approve_inbound(self, found: Classification, *, origin: str = "") -> None:
        """A numeral on a permission notice: the user's verdict, on that dialog (ADR 0021 §6).

        The Approval Relay carries and nothing more (#191): whether the dialog
        is still open is `answer_approval`'s own answer, read off the roster. A
        handle no live row carries is a dialog the keyboard already settled;
        nothing is sent, the reply sends the user to the screen, and the numeral
        is never re-read as words.
        """
        assert found.target is not None and found.verdict is not None
        try:
            outcome = await self.answer_approval(found.approval_id, found.verdict)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal), origin=origin)
            return
        if outcome is None:
            await self._reply(PERMISSION_ALREADY_SETTLED_HINT, origin=origin)
            return
        await self._reply(
            receipt_sentence(outcome), origin=origin, anchor=_receipt_anchor(found.target)
        )

    async def _settle(self, outcome: RelayOutcome) -> None:
        """Land one Relay's standing on the Session's row, and wake if it is news.

        **The hub's, because only the hub can judge the Focus Session** (#197). A
        relay can pass its ceiling minutes after it was queued, and the user may
        have answered another Session in between (`CONTEXT.md`, *Focus Session*;
        ADR 0017) — so `focus` is read *here*, at the moment of waking, and the
        Relay pipeline learns nothing of the Keeper.

        Total over the outcome's reason, and every site that produces one passes
        through it:

        - `DELIVERED` clears the field. The user's words landed, so there is
          nothing left undelivered to say — and no wake: an arrival is not news.
        - `CEILING_PASSED` replaces it and wakes the Keeper once. The reason and
          the last attempt's grade travel together, because a ceiling may not
          claim non-delivery of an attempt that proved nothing
          (`core/relays.py::RelayReason`).
        - `SESSION_ENDED` and `QUESTION_UNANSWERABLE` are logged and nothing
          else. An exited Session appears nowhere (`CONTEXT.md`, *Focus
          Session*), so a field on its row is a field nobody reads; and a
          question refused before the wire was answered by the receipt the
          caller is already holding.
        - Everything still in play — queued, retained, held — leaves the field
          exactly as it stands. It says what the *last* Relay that did not
          arrive was, not what the newest one is doing.

        The field is folded onto the row beside the wait, never through it:
        #209's `with_waiting_for` and #213's `stopped_state` are untouched, and
        no Reply Window moves.
        """
        if outcome.reason is RelayReason.DELIVERED:
            self._fold_undelivered(outcome.target, None, relay=outcome.request_id)
            return
        if outcome.reason is not RelayReason.CEILING_PASSED:
            if outcome.state is Lifecycle.REPORTED_FAILED:
                _log.info(
                    "the user's words for %s will never arrive (reason=%s grade=%s), and "
                    "nothing is briefed about it",
                    outcome.target,
                    outcome.reason,
                    outcome.grade,
                )
            return
        undelivered = UndeliveredRelay(
            reason=outcome.reason,
            grade=None if outcome.receipt is None else outcome.receipt.outcome,
        )
        if not self._fold_undelivered(outcome.target, undelivered, relay=outcome.request_id):
            return
        # One wake, carrying no content: whether that Session still needs the
        # user is read again by the Briefer at the moment the Keeper acts (ADR
        # 0017). `focus` is judged now, not when the words were queued.
        await self.keeper.wake(focus=self._state.sessions.focus == outcome.target)

    def _fold_undelivered(
        self, target: SessionTarget, undelivered: UndeliveredRelay | None, *, relay: RequestId
    ) -> bool:
        """Write the field, and say whether there was a live row to write it on.

        A Session that ended while the words waited gets nothing: it appears in
        no brief, so the reason has nowhere to be read from and the log is the
        record. Same answer for a row the roster never held.

        **Every change to the field is written down, and this is the one place
        both of them pass through** (#226). The field is read twice — once by
        `bridgectl brief` and once by the Stop Notice rendered from the same
        row — and a late proof of delivery legitimately clears it between the
        two, so the two readings can disagree while both are honest. Nothing
        distinguished that from the defect until these lines existed: a reader
        holding two disagreeing readings and no record of the write between them
        cannot attribute either. It is #75's rule on the announcement side, kept
        here — an engine silent about the one event that changes a user-visible
        field leaves the run nothing to attribute.

        Both lines name the Session and the Relay: the Session because the log
        carries every Session on the machine, and the Relay because "which
        words" is the whole of what changed. An **unchanged** write says
        nothing, which is the common case and the reason the silence is worth
        keeping — a line per delivered Relay would bury the two that matter.
        """
        try:
            session = self._state.sessions.resolve(target)
        except BridgeCoreError:
            _log.info("a Relay settled for a Session this roster does not hold: %s", target)
            return False
        if not session.is_live:
            _log.info("a Relay settled for a Session that has ended: %s", target)
            return False
        if session.undelivered == undelivered:
            # Nothing to write. The common case by far — every delivered Relay
            # to a Session with nothing outstanding lands here — and a write
            # that changes nothing is a write a reader has to rule out.
            return True
        if undelivered is None:
            _log.info(
                "a Relay to %s arrived after all, and its brief no longer says so: relay=%s",
                session.target,
                relay,
            )
        else:
            _log.info(
                "a Relay to %s did not arrive, and its brief now says so: "
                "relay=%s reason=%s grade=%s",
                session.target,
                relay,
                undelivered.reason,
                undelivered.grade,
            )
        self._state.sessions.set_undelivered(session.target, undelivered)
        return True

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

    async def _reply(
        self,
        text: str,
        *,
        origin: str = "",
        notice: Notice | None = None,
        anchor: Anchor | None = None,
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
        receipt = await self._send(text, origin=origin, notice=notice, anchor=anchor)
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
        notice: Notice | None = None,
        anchor: Anchor | None = None,
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
        observer learns which provider ids it landed under, and the harness
        reads the message by that id rather than waiting for a window. The
        format is therefore fixed: `key=value`, ids comma-joined in sending
        order, empty when nothing landed. An UNKNOWN receipt from a split send
        still names the parts that did land — those are messages the user can
        reply to.

        `origin` is echoed from the inbound event when this is a reply, and
        empty for an unbidden push. `notice` is the structured brief the text
        renders, for a push that is one (ADR 0021 §5) and for a reply that is
        a menu screen or a re-sent brief (§6); a receipt or a refusal carries
        none. Nothing here revises: editing a notice in place (ADR 0021 §8) is
        #266's.
        """
        request_id = new_request_id()
        receipt = await self._channel.send(
            text, request_id=request_id, origin=origin, notice=notice
        )
        _log.info(
            "sent Companion Channel message request=%s outcome=%s message_ids=%s",
            request_id,
            receipt.outcome,
            ",".join(receipt.message_ids),
        )
        if anchor is not None:
            self.anchors.register(receipt.message_ids, replace(anchor, sent_at=self._stamp()))
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
