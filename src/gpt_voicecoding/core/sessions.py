"""The Session registry — Bridge Core state, and deliberately not a module.

Conversing with a Session is the Agent seam; what Sessions *exist* is held here,
in the hub, and every surface queries it rather than keeping a copy (ADR 0001).

**Rows arrive by observation, not by registration.** A Session is one the *user*
started (#68), so nothing tells this registry a Session exists — a lane goes and
looks, on a cadence, and hands back everything it can see as a `LaneDiscovery`.
`observe` makes the roster agree with that answer. The reference implementation
had it the other way round: a Session existed because a hook had announced it,
so a Session started before the engine, or one whose hook failed, was invisible
forever while its process ran.

Four constraints are the point of this file, and each one is a defect the reference
implementation carried:

- **An unknown identity fails closed.** Nothing resolves to "probably that one".
- **A stale identity is not an unknown one.** A wrong pid under a known session
  id means the Session forked — `--resume` starts a second process under the
  same session id — so the refusal names the pids that *are* live instead of
  pretending the session id was never seen.
- **A Child Process is seen, not spoken to.** It is listed like any other row and
  refused as a Relay target, by the registry rather than by a caller's memory —
  and it is never named, whatever a lane composed for it (#78, #79). A name is
  what the user says to reach a Session, and there is nothing here to reach.
- **A Session Name never addresses.** Names are for recognition and speech;
  only identities select a target (ADR 0024).

There is one registry and one Reply Window per Session — and the Reply Window is
**derived**, so there is no second copy to disagree with the first. The reference
implementation ran two live ledgers and rendered both; nothing here may grow a
second.

The **Focus Session** is the one piece of state here that is about the roster
rather than about a row: one pointer at the Session the user last replied to,
set by a Relay or an Approval Relay and cleared when that Session ends. It is a
pointer and not a flag on a row for the reason everything else here is one thing
— a flag per row is a shape that can hold two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from gpt_voicecoding.core.errors import (
    ChildSessionError,
    DuplicateSessionError,
    HeadlessRunError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.core.naming import NameChoice, NameRung, choose_task, compose
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.seams.agent import (
    MAIN_SESSION,
    ChildClassification,
    LaneDiscovery,
    ProgressAvailability,
    ProgressObservation,
    ReplyWindow,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Session:
    """One coding-agent run the user started, as the roster holds it.

    Lane observations supply the facts. Core owns `first_seen` and the name's
    chosen rung: it accepts better sources and follows changes within a rung
    without falling back when a source disappears (ADR 0024).
    """

    target: SessionTarget
    workspace: Path
    first_seen: float
    #: What this Session is called. `None` is ordinary: an unnamed row is listed
    #: like any other, and a Codex thread has neither a name nor an id to make
    #: one from until it takes its first turn (#73).
    name: SessionName | None = None
    name_rung: NameRung | None = None
    lifecycle: SessionLifecycle = SessionLifecycle.LIVE
    state: SessionState = SessionState.RUNNING
    waiting_for: WaitingFor = field(default_factory=WaitingFor)
    progress: ProgressObservation = field(default_factory=ProgressObservation)
    last_activity: datetime | None = None
    child: ChildClassification = MAIN_SESSION
    #: Whether a person can type into this run, as the lane read it off the
    #: process table (`seams/agent.py::SessionInspection`). The one fact the
    #: tier below is decided from, held here rather than re-derived by every
    #: reader — and merged rather than taken, by `climbed_to`.
    has_controlling_terminal: bool | None = None

    @property
    def reply_window(self) -> ReplyWindow:
        """Derived, never stored — see `seams.agent.derive_reply_window`.

        An ended Session accepts nothing, whatever it was doing when it went.
        """
        if self.lifecycle is not SessionLifecycle.LIVE:
            return ReplyWindow.CLOSED
        return derive_reply_window(self.state, self.waiting_for, self.child)

    @property
    def is_live(self) -> bool:
        return self.lifecycle is SessionLifecycle.LIVE

    @property
    def is_headless_run(self) -> bool:
        """Whether this run is one nobody can type into (#319, ADR 0020 amended).

        **The tier, and Bridge Core's alone.** Both lanes carry the fact and
        neither reads it: a rule decided here cannot drift into two rules, which
        is what a lane that silenced its own rows would become.

        `False` is the whole of it. `True` is a Session and so is `None` — a
        read that could not tell is not a claim that nobody is there, and
        silencing a real Session is the expensive error where an extra ended
        line is the cheap one.
        """
        return self.has_controlling_terminal is False

    @property
    def is_addressable(self) -> bool:
        """Whether the user may be told about this row and reply to it.

        **The one predicate every surface that picks a Session asks**, so the
        three tiers are read in one place rather than re-spelled at each. An
        ended row is gone, a Child Process is seen and never spoken to (#68,
        #79), and a Headless Run is kept and never announced (#319) — three
        facts, one question, and the question is the one `resolve` answers by
        raising.

        It is deliberately *not* what `live()` reports. A Headless Run and a
        Child Process are both still on the roster and still carried by
        `status`, which is where "appears in the roster" is true; what this
        decides is whether the user is offered them.
        """
        return self.is_live and self.child.is_main and not self.is_headless_run

    def observed(
        self,
        row: SessionInspection,
        *,
        target: SessionTarget,
        first_prompt_characters: int,
    ) -> Session:
        """This same Session, as a lane has just seen it again.

        Keeps what the registry knows and the lane does not — `first_seen` and
        the Session Name it has already accepted — and takes everything else
        from the reading. **Two fields are merged rather than taken**, and both
        for one reason: a reading that says it could not tell is not an answer,
        so it never replaces one. `with_progress` holds that for progress and
        `with_waiting_for` for the wait.

        `target` is the identity the registry settled on (`_better_known`),
        which is not always the one the reading carried: a tick with only
        process evidence names a Session it cannot see the id of. It is passed
        in rather than read off the row because the naming rule turns on it.
        """
        name, rung = self._named_as(row, target, first_prompt_characters)
        updated = replace(
            self,
            target=target,
            workspace=row.workspace,
            name=name,
            name_rung=rung,
            lifecycle=row.lifecycle,
            state=row.state,
            last_activity=row.last_activity,
            child=row.child,
            # The third merged field, and merged for the two reasons above said
            # once more: a read that could not tell never replaces one, and a
            # terminal that was there once is never taken away (#319).
            has_controlling_terminal=climbed_to(
                self.has_controlling_terminal, row.has_controlling_terminal
            ),
        )
        return updated.with_waiting_for(
            row.waiting_for, waiting=row.state is SessionState.WAITING
        ).with_progress(row.progress)

    def with_waiting_for(self, waiting_for: WaitingFor, *, waiting: bool) -> Session:
        """Apply a new reading of the wait, without letting *ask again* erase an answer.

        `caught_up=False` is the seam's documented instruction to ask again and
        never guess (`seams/agent.py::WaitingFor`), and until #209 nothing on
        this side implemented it: every reading was stored with the authority of
        knowledge. Run `20260902T065340Z` is what that cost. A discovery pass
        sampled a Claude Session inside the ~160 ms in which its roster already
        said `waiting`, its transcript had not flushed the `AskUserQuestion`
        record, and the hook had not yet parked the question; the reading was
        honestly `UNKNOWN`, and it landed on top of nothing, because the question
        arrived 100 ms later. What made it a defect rather than a race was that
        nothing corrected it until the next cadence: `status` answered `unknown`
        with a closed Reply Window about a question that was parked and
        answerable, and the acceptance's `switches` step failed on it.

        So a reading that admits it has not caught up leaves a **known** wait —
        a question or a permission somebody read from the record or the held
        dialog — exactly where it was.

        **It cannot make a ghost**, which is the risk of any rule that keeps
        state. What is kept is a *wait*, and it lasts only while the reading is
        still about one: a reading whose Session is no longer `waiting` replaces
        it, whether it caught up or not, and any caught-up reading replaces it
        outright. `NONE` and `UNKNOWN` are never kept over anything, because
        neither is an answer this rule exists to protect.

        Legacy (ADR 0010) — `legacy@1d32845:bridge/daemon.py:2115-2165`
        (`_read_caught_up`) re-read the transcript within a budget and published
        only once it was caught up, over `caught_up` as
        `legacy@1d32845:bridge/transcript.py:175-184` defines it. **Adapted**: the
        Stop announcement path already ports the re-read (`claude/window.py`
        `_settle`, #150), and blocking a whole-machine discovery pass on one
        Session's transcript is not a trade this cadence can make — so the
        budgeted re-read becomes a refusal to overwrite, which needs no I/O and
        holds for every lane.
        """
        if not self._keeps_its_wait(waiting_for, waiting=waiting):
            return replace(self, waiting_for=waiting_for)
        _log.info(
            "%s is waiting on a %s this reading could not see, and the reading has not caught "
            "up with the record (it offered %s), so the roster keeps what it knows",
            self.target,
            self.waiting_for.kind,
            waiting_for.kind,
        )
        return self

    def _keeps_its_wait(self, waiting_for: WaitingFor, *, waiting: bool) -> bool:
        """Whether this reading is the "ask again" that the wait above outranks."""
        return (
            not waiting_for.caught_up
            and waiting
            and self.waiting_for.caught_up
            and self.waiting_for.kind in (WaitingKind.QUESTION, WaitingKind.PERMISSION)
        )

    def with_progress(self, progress: ProgressObservation) -> Session:
        """Apply a new observation without replacing a readable fact with no answer."""
        unavailable = progress.availability is ProgressAvailability.UNREADABLE
        not_read_after_readable = (
            progress.availability is ProgressAvailability.NOT_READ
            and self.progress.availability is ProgressAvailability.READABLE
        )
        return self if unavailable or not_read_after_readable else replace(self, progress=progress)

    def _named_as(
        self, row: SessionInspection, target: SessionTarget, limit: int
    ) -> tuple[SessionName | None, NameRung | None]:
        """Store the pure naming judgement beside the row's existing identity."""
        if not row.child.is_main:
            return None, None
        previous = None
        if target == self.target and self.name is not None and self.name_rung is not None:
            previous = NameChoice(self.name.task, self.name_rung)
        result = choose_task(
            target.agent,
            first_prompt_characters=limit,
            previous=previous,
            user_name=row.user_name,
            ai_title=row.ai_title,
            first_prompt=row.first_prompt,
            derived_name=row.derived_name,
            thread_name=row.thread_name,
            preview=row.preview,
            short_thread_id=row.short_thread_id,
        )
        for reason in result.refusals:
            _log.debug("refused Session Name candidate for %s: %s", target, reason)
        choice = result.choice
        if choice is None:
            return None, None
        if previous is not None and choice.task == previous.task:
            return self.name, choice.rung
        name = compose(row.project_name, choice.task) if row.project_name is not None else None
        if name is None:
            return (self.name, self.name_rung) if target == self.target else (None, None)
        return name, choice.rung


def climbed_to(held: bool | None, seen: bool | None) -> bool | None:
    """Which controlling-terminal answer to keep when a run has been read twice.

    **It climbs and never falls**, which is #319's two edge cases in one rule:

    - *A reading that could not tell is not an answer*, so `None` never replaces
      one. A Headless Run whose pid leaves the process table between the lane's
      own read and the `ps` is read as `None`, and it stays a Headless Run —
      taking that as "a terminal after all" would announce the end of exactly
      the run this tier exists to silence.
    - *A run seen with a terminal stays a Session.* A `claude` whose tty column
      goes blank on a later pass is a misread of a Session, not a Session that
      stopped being typeable, and falling would silence it.

    So a run misread as headless climbs the moment a terminal is seen, and is
    announced from then on — never retroactively, because nothing here replays.
    """
    if seen is None or held is True:
        return held
    return seen


def _better_known(held: SessionTarget, seen: SessionTarget) -> SessionTarget:
    """The identity to keep when one process has been named two ways.

    Three cases, and the rule is one sentence: **`None` never overwrites a known
    session id; a different known id does.**

    - The reading names a Session we held anonymously — it has taken its first
      turn and written the rollout that names it (#73). Take the new name.
    - The reading is anonymous and we hold a name — this tick had only process
      evidence, because the shared daemon is not answering. Keep the name; a
      Session cannot un-know its own id, and dropping it would make the row
      look like a different Session to everything that addresses it.
    - The reading names a *different* Session on the same process — the user
      typed `/new` in that TUI, so the process is the same one and the thread
      is not. Take the new name: whoever could read an id is the authority on
      which one it is.
    """
    if seen.session_id is None:
        if held.session_id is not None:
            _log.info(
                "%s was read with process evidence only this tick; keeping its known session id",
                held,
            )
        return held
    if held.session_id is not None and held.session_id != seen.session_id:
        _log.info("pid %s moved from thread %s to %s", held.pid, held.session_id, seen.session_id)
    return seen


def session_from(
    row: SessionInspection,
    *,
    first_seen: float,
    policy: CorePolicy | None = None,
) -> Session:
    """A first observation uses the same naming step as every later reading."""
    return Session(target=row.target, workspace=row.workspace, first_seen=first_seen).observed(
        row,
        target=row.target,
        first_prompt_characters=(policy or CorePolicy()).first_prompt_characters,
    )


class SessionRegistry:
    """What Sessions exist. Holds state; decides no policy about them."""

    def __init__(self, *, policy: CorePolicy | None = None) -> None:
        self._policy = policy or CorePolicy()
        self._sessions: dict[SessionTarget, Session] = {}
        #: Why a lane could not enumerate, per agent. `status` shows it; nothing
        #: else reads it, because it is news about the lane and not about a row.
        self._lane_errors: dict[AgentKind, str] = {}
        #: Why a lane's rows came from a weaker source than usual, per agent.
        #: Kept apart from `_lane_errors` rather than folded in, because the two
        #: are different news: one lane has rows that are true and thin, the
        #: other has no news at all. Collapsing them would repeat the two-field
        #: encoding `LaneDiscovery` exists to avoid.
        self._lane_degradations: dict[AgentKind, str] = {}
        #: The Focus Session — the one the user last replied to, by Answer Relay
        #: or Approval Relay (#165 Q2). **One pointer on the registry, never a
        #: flag on a row**: it is a fact about the roster rather than about any
        #: Session, and there is exactly one at a time — a per-row flag is a
        #: shape that can hold two, and nothing but a sweep would notice.
        self._focus: SessionTarget | None = None

    # -- the Focus Session ----------------------------------------------

    @property
    def focus(self) -> SessionTarget | None:
        """The Session the user last replied to, or None when they have replied to none.

        What the voice does with that record is `spoken_first`, which is not
        always this: the two parted when a roster of one earned a word without
        a reply behind it.
        """
        return self._focus

    @property
    def spoken_first(self) -> SessionTarget | None:
        """The Session whose news the voice speaks, rather than rings about.

        The Focus Session when there is one. When there is none, the **sole live
        Session**: the EVENT Cue's whole meaning is "another Session wants you"
        (#167 Q7-Q9), and on a roster of one there is no other for it to mean.
        Ringing there tells the user nothing they could act on, and the word they
        are owed waits on an Answer Relay they have no reason to send — a user
        sitting at that Session's own terminal replies by typing into it, which
        no surface sees and which therefore never sets the focus.

        The two readings are ordered, not merged. A focus that is held decides
        alone, so a second Session's news still only rings while the user works
        through the first; the roster is read only for the **empty** case.

        Kept apart from `focus` because they answer different questions. `focus`
        is the record of what the user last replied to, and nothing may set it
        but a reply (#165 Q2); this is what the voice does with that record now.
        """
        if self._focus is not None:
            return self._focus
        sole = self.sole_live()
        return None if sole is None else sole.target

    def set_focus(self, target: SessionTarget) -> None:
        """The user replied to this Session, so it becomes the Focus Session.

        Held as the identity the *roster* addresses it by rather than the one
        the caller happened to write, so a Codex Session named `codex::6548` by
        a surface and `codex:abc:6548` by the roster is one focus and not two.

        Set only by the user replying — never by asking about a Session (#165
        Q2). `brief` is a read, and a read that moved the focus would let the
        Voice change what it speaks first merely by looking.

        **It never raises.** A Session the roster does not hold — an approval
        answered for a row that ended between the dialog opening and the verdict
        arriving — cannot be the one spoken first, because there is nothing to
        speak about. Refusing here would turn a verdict that was carried into a
        refusal the user reads as their answer having been dropped, which is the
        opposite of what happened. The focus stands where it was, and the
        attempt is said out loud rather than swallowed.
        """
        try:
            self._focus = self._held_row(target).target
        except (UnknownSessionError, StaleSessionError):
            _log.info("the user replied to a Session this roster does not hold: %s", target)

    def _focus_ended(self, target: SessionTarget) -> None:
        """Clear the focus if this is the Session it named. Ended is ended."""
        if self._focus == target:
            self._focus = None

    def _rekeyed(self, held: SessionTarget, target: SessionTarget, updated: Session) -> Session:
        """One row has been re-keyed. Decide what follows it, and what does not.

        `_better_known` takes a newly-read session id in **two** cases and they
        are not the same event. A row we held anonymously that has taken its
        first turn is **the same Session** gaining the id it had none of (#73),
        so everything about it follows: the user replied to that Session, and it
        is still the Session they replied to. A row whose *known* id changed
        under one pid is `/new` in that TUI — **a different Session** on the
        same process (#77).

        One thing turns on that: the focus does not follow a `/new`, because it
        would name a Session the user has never replied to, which is the one way
        the Focus Session must not be set (#165 Q2).

        Everything else on the row is the lane's own reading of the process and
        is true of it either way; the focus is a fact about the conversation.
        """
        same_session = held.session_id is None
        if self._focus == held:
            self._focus = target if same_session else None
        return updated

    # -- observation ----------------------------------------------------

    def observe(
        self, agent: AgentKind, lane: LaneDiscovery, *, now: float
    ) -> tuple[SessionTarget, ...]:
        """Make this lane's rows the roster's truth about this lane, and say what went.

        Returns the Sessions that ended on *this* observation, as they were last
        addressed. **Only this class can answer that**, and the answer is not
        recoverable by comparing the roster before and after: a Codex row gains
        its thread id at its first turn (#73) and so changes `SessionTarget`
        without anything having ended, and a Codex `/new` does the same. A
        caller diffing targets would read both as a death — and the news of a
        death is not free, because it terminates every Relay queued for that
        target.

        **Both of those are Codex, and the qualifier is load-bearing** (#79).
        They are cases of a reading that could not name itself being matched by
        its process, which is what `_same_row` does for a lane that is not
        `always_named`. Claude names every row, so a Claude row never moves
        between targets: a `/clear` is a new session id under the same process,
        and it ends the old row and starts a new one rather than re-keying the
        held one. That conversation really is over, and its queued Relays are
        answered as such.

        **A lane that could not look changes nothing.** `LaneDiscovery.error`
        means the roster has no newer information, not that the machine emptied:
        reading a failed enumeration as an empty one would end every Session on
        that lane every time a daemon restarted. The error is recorded for
        `status` and the rows are left exactly as they were.

        **A lane that looked found what it found — even if it found nothing, and
        even if it looked with something weaker than usual.** An empty
        enumeration ends rows, because a Session that is gone must stop being
        offered as a target. `LaneDiscovery.degraded` says the rows came from a
        weaker source; it does not make them less true.

        **A Session that stopped being seen ends once, then is forgotten.** It
        goes `LIVE → ENDED` on the first discovery that does not contain it, so
        a surface can still say what happened to it, and is dropped on the next
        one. Forgetting immediately would make a Session that ended between two
        ticks indistinguishable from one that never existed.

        The other lane's rows are untouched in every case: two agents fail
        independently, and one of them being down is not news about the other.
        """
        if not lane.enumerated:
            assert lane.error is not None  # `enumerated` is exactly this test
            self._lane_errors[agent] = lane.error
            _log.info("the %s lane could not enumerate: %s", agent, lane.error)
            return ()
        self._lane_errors.pop(agent, None)
        if lane.degraded is None:
            self._lane_degradations.pop(agent, None)
        else:
            self._lane_degradations[agent] = lane.degraded
            _log.info("the %s lane is reading from a weaker source: %s", agent, lane.degraded)

        seen: set[SessionTarget] = set()
        for row in lane.rows:
            if row.target.agent is not agent:
                _log.warning(
                    "the %s lane returned a %s row (%s); ignored, because a lane speaks "
                    "only for its own agent",
                    agent,
                    row.target.agent,
                    row.target,
                )
                continue
            seen.add(self.observed_one(row, now=now).target)

        ended: list[SessionTarget] = []
        for held in [row for row in self._of(agent) if row.target not in seen]:
            if held.is_live:
                self._sessions[held.target] = replace(held, lifecycle=SessionLifecycle.ENDED)
                self._focus_ended(held.target)
                ended.append(held.target)
            else:
                del self._sessions[held.target]
        return tuple(ended)

    def lane_errors(self) -> dict[AgentKind, str]:
        """Which lanes could not be enumerated at their last attempt, and why."""
        return dict(self._lane_errors)

    def lane_degradations(self) -> dict[AgentKind, str]:
        """Which lanes have rows read by something weaker than usual, and which.

        The rows are true; this says what read them. Separate from
        `lane_errors` because a user reading `status` needs to tell "Codex is
        running on the process table because the shared daemon is not up" from
        "nobody could look at Codex at all" — the first still lists Sessions.

        A lane that could not look leaves this alone, for the same reason it
        leaves its rows alone: the note describes rows that did not change.
        """
        return dict(self._lane_degradations)

    def observed_one(self, row: SessionInspection, *, now: float) -> Session:
        """Fold one freshly-read row into the roster, in place where it belongs.

        Public because the per-target read is a caller too (#76's `progress`
        verb): it looked at one Session and at no others, so it may correct that
        Session's entry and may conclude nothing about the rest. **`observe` is
        the whole-lane verb and stays the only one that ends a row** — a verb
        that ended rows because it was asked about one Session would end the
        whole lane every time somebody asked about one of its members.
        """
        held = self._same_row(row)
        if held is None:
            fresh = session_from(row, first_seen=now, policy=self._policy)
            self._sessions[fresh.target] = fresh
            self._note_tier(None, fresh)
            return fresh

        target = _better_known(held.target, row.target)
        updated = held.observed(
            row, target=target, first_prompt_characters=self._policy.first_prompt_characters
        )
        if target != held.target:
            updated = self._rekeyed(held.target, target, updated)
            del self._sessions[held.target]
        self._sessions[target] = updated
        self._note_tier(held, updated)
        return updated

    @staticmethod
    def _note_tier(held: Session | None, updated: Session) -> None:
        """Say once that a run is a Headless Run, on the reading that decides it.

        **Once per row, on the pass that first reads `False`** — user story 6:
        a run that produces no push at all is otherwise indistinguishable in
        `engine.log` from one the engine never saw, and the verdict is the whole
        explanation of the silence that follows. Repeating it every cadence
        would bury the run's other lines under one fact that has not changed,
        and `climbed_to` guarantees the transition happens at most once per row
        in the falling direction.
        """
        if updated.is_headless_run and (held is None or not held.is_headless_run):
            _log.info("%s is a Headless Run: no controlling terminal", updated.target)

    def _same_row(self, row: SessionInspection) -> Session | None:
        """The roster entry this reading is *about*, whatever it happens to name it.

        **The process is the identity; the session id is a field on it** — for
        an agent that does not always say its id. A Codex TUI exists before it
        has one (#73) and keeps its pid across `/new`, so keying on the id would
        make one process come and go from the roster every time it was read by a
        different source. Where there is no pid — a daemon thread nobody could
        tie to a TUI — the id is all there is, and it is the key.

        **An agent that always names itself is matched on its name, never on
        its process** (`AgentKind.always_named`). Claude's official roster
        carries a session id on every row from the moment the Session exists, so
        there is no Claude reading that *has* to be matched by pid — and one
        that is matched by pid is matched wrongly, because a Claude pid is not
        one Session. Two things share it, and #79 is how that stopped being
        theoretical:

        - **A Child Process runs inside its parent's process.** A Task subagent
          is not a process of its own (`claude/children.py`, measured), so its
          row carries its parent's pid. Joined on that, the child's reading
          replaces the parent's row — one tick later the user's own Session has
          become an unrelayable child, logged as the process having moved
          threads.
        - **Two children of one Session share it with each other**, so the
          second would swallow the first however the classification was
          compared.

        What is left for Claude is the exact match above, which is what the
        `--resume` fork already relies on: two processes under one session id
        are two rows, and a `/clear` under one process is a new Session that
        starts a new row while the old one ends for having left the roster.
        """
        exact = self._sessions.get(row.target)
        if exact is not None:
            return exact
        if row.target.pid is None or row.target.agent.always_named:
            return None
        return next(
            (held for held in self._of(row.target.agent) if held.target.pid == row.target.pid),
            None,
        )

    # -- addressing -----------------------------------------------------

    def register(self, session: Session) -> Session:
        """Record a Session that was found to exist. Refuses to register truth twice."""
        target = session.target
        if target in self._sessions:
            raise DuplicateSessionError(target)
        if target.named and not target.agent.addressed_by_pid and self._by_session_id(target):
            raise DuplicateSessionError(target)
        self._sessions[target] = session
        return session

    def resolve(self, target: SessionTarget) -> Session:
        """The Session that exact identity names, or a refusal saying why not."""
        candidates = self._by_session_id(target)
        if not candidates:
            raise UnknownSessionError(target)

        if target.agent.addressed_by_pid or not target.named:
            matched = [held for held in candidates if held.target.pid == target.pid]
            if not matched:
                raise StaleSessionError(
                    target,
                    reason="that session id runs under a different process",
                    live_pids=tuple(
                        held.target.pid
                        for held in candidates
                        if held.is_live and held.target.pid is not None
                    ),
                )
            session = matched[0]
        else:
            session = candidates[0]

        if not session.is_live:
            raise StaleSessionError(target, reason=f"that Session is {session.lifecycle}")
        if not session.child.is_main:
            raise ChildSessionError(target, session.child.parent)
        # **A Headless Run is refused exactly as a Child Process is** (#319):
        # here, so that `RelayPipeline.relay` refuses before any adapter is
        # touched, and so that the control plane and the Companion Channel get
        # one answer rather than each remembering the rule for itself.
        if session.is_headless_run:
            raise HeadlessRunError(target)
        return session

    def set_state(self, target: SessionTarget, state: SessionState) -> Session:
        """Record what a lane observed this Session to be doing.

        The Reply Window follows from it and is never set directly — there is
        one field, so there is nothing for a second writer to disagree with.
        """
        held = self._held_row(target)
        updated = replace(held, state=state)
        self._sessions[held.target] = updated
        return updated

    def set_stop_reading(
        self,
        target: SessionTarget,
        *,
        waiting_for: WaitingFor,
        progress: ProgressObservation,
        now: float,
        has_controlling_terminal: bool | None = None,
    ) -> Session:
        """Fold the whole reading a Stop carried into that Session's roster row.

        **The whole reading, because it is the freshest one the engine holds.** A
        Stop is read at the moment the Session stopped, through the lane's own
        overlay — which consults the dialog parked on the approval socket, the
        one place a question exists before the transcript flushes it — and after
        the announcement path has waited for the record to catch up (#150). Until
        #209 only the progress half was written back and the wait was dropped, so
        `status` went on answering from the last discovery pass: in run
        `20260902T065340Z` that was a reading taken 100 ms before the question was
        parked, and a parked, answerable question read as `unknown` for the rest
        of the cadence.

        Both halves go through the same merge rules the discovery pass uses, for
        the same reason: a Stop that could not say what it stopped on
        (`caught_up=False`, which #150's budget can end on) is not an answer
        either, and it does not erase one.

        **The state comes with the reading**, because a row that held them apart
        would answer two ways: `reply_window` is derived from the state *and* the
        wait (`seams/agent.py::derive_reply_window`), so a row carrying a
        question beside `RUNNING` would report a parked question and a closed
        window in the same breath. Until #213 only a wait that `needs_the_user`
        moved the state and a Stop that merely ended a turn was left exactly as
        the last discovery pass found it — normally `RUNNING` — for the next pass
        to correct one cadence later (≈5.6 s, measured in #209). For that cadence
        the engine held two answers about one Session: the notice said the turn
        had finished and every reader of the roster said it was running, and the
        dial had to be handed the notice's own brief to cover the row.

        So the state is derived from the *merged* wait, by the one rule
        `WaitingFor.stopped_state` states: `NONE` is `IDLE`, anything else is
        `WAITING`. Derived from the merged wait and not the reading, so a Stop
        that could not say what it stopped on still leaves the question this row
        already knew standing — and standing in `WAITING`, where it was.

        **Discovery still wins afterwards.** The next caught-up reading overwrites
        this state as it overwrites any other; the Stop's write shrinks the stale
        window to zero rather than becoming a second source of truth.

        **A Stop for a Session no pass has landed lands a row here too** (#216).
        Discovery runs on a cadence, so a Session can stop before any reading of
        its lane has covered it — and a Stop *is* a reading, taken of a Session
        that demonstrably exists. Recording it is what lets every reader answer
        alike: the notice's text and the fresh roster reading a system-dialled
        call is briefed from (ADR 0017) had disagreed about whether the Session
        existed at all, so the user was told in words and never rung. `now` is
        that row's `first_seen` — when *we* first saw it, which is exactly what
        this Stop was.

        Legacy (ADR 0010) — **adapted.** The reference implementation put a
        Session on its roster from the event itself: every hook message, a Stop
        included, upserted the row (`legacy@1d32845:bridge/store.py:1644-1712`,
        called from `bridge/daemon.py:1192-1248`), so a Stop was never about a
        Session the store had no record of. What is adapted is that principle
        and not its shape — rows here still arrive by observation (this module's
        own rule), and the row a Stop lands is a stand-in carrying only what the
        Stop read, which the next discovery pass reconciles.
        """
        held = self._held_or_stood_in_for(target, now=now)
        updated = held.with_waiting_for(waiting_for, waiting=True).with_progress(progress)
        updated = replace(
            updated,
            state=updated.waiting_for.stopped_state,
            # **The tier is settled here, before the caller announces anything**
            # (#319). A Stop can precede every discovery pass, so the row a Stop
            # stands in for would otherwise be a Session by default and be
            # announced once before the first pass could correct it — which is
            # exactly the one push a Headless Run must not produce.
            has_controlling_terminal=climbed_to(
                held.has_controlling_terminal, has_controlling_terminal
            ),
        )
        self._sessions[held.target] = updated
        self._note_tier(held, updated)
        return updated

    def _held_or_stood_in_for(self, target: SessionTarget, *, now: float) -> Session:
        """The live row for that identity, or a new one standing in for it (#216).

        Both refusals `_held_row` raises mean the same thing to a Stop — the
        roster holds no row for *this* identity — and a Stop is evidence the
        identity is real: unknown is a Session the cadence has not reached yet,
        and stale is a second process under a session id the roster knows (a
        `--resume` fork). Either way the row the Stop is about is the one being
        created, and the next whole-lane pass reconciles it like any other.

        **An ended Session keeps its ended row, and a Stop does not resurrect
        it.** `_held_row` answers with the row held for that identity whatever
        its lifecycle, which is what this wants: `mark_ended` recorded that the
        Session is gone, and that is the later fact. The Stop's reading still
        lands on the row — the roster does not lose a reading — but the row
        stays out of `live()`, so nothing is briefed or dialled about a Session
        that has ended. Standing a *second*, live row in for it would be this
        registry inventing a Session out of two events about one.
        """
        try:
            return self._held_row(target)
        except (UnknownSessionError, StaleSessionError):
            return self.register(stand_in(target, first_seen=now))

    def mark_ended(self, target: SessionTarget) -> Session:
        """A Session is gone. Its Reply Window closes with it, by derivation.

        Deliberately not routed through `resolve`: a Child Process ends like
        anything else, and refusing to record that because it may not be
        Relayed into would leave the roster claiming a dead process is running.
        `resolve` guards *addressing*; this records *what happened*.
        """
        held = self._held_row(target)
        ended = replace(held, lifecycle=SessionLifecycle.ENDED)
        self._sessions[held.target] = ended
        self._focus_ended(held.target)
        return ended

    def _held_row(self, target: SessionTarget) -> Session:
        """The row held for that identity, whatever it is — or why there is none.

        **Whatever it is includes its lifecycle**: an ended row is still that
        Session's row and comes back from here, which is what all four callers
        want — `mark_ended` is idempotent, `set_focus` and `set_stop_reading`
        write onto the row the roster holds, and `_held_or_stood_in_for` stands
        a row in only where there is none to hold. It was called `_live_row`
        until #216, and the name read as a guarantee it never made: `live()` is
        what filters by lifecycle, and it is the only thing that does.
        """
        candidates = self._by_session_id(target)
        if not candidates:
            raise UnknownSessionError(target)
        if target.agent.addressed_by_pid or not target.named:
            matched = [held for held in candidates if held.target.pid == target.pid]
            if not matched:
                raise StaleSessionError(
                    target, reason="that session id runs under a different process"
                )
            return matched[0]
        return candidates[0]

    def forget(self, target: SessionTarget) -> None:
        """Drop a Session entirely. Resolving it afterwards is unknown, not stale."""
        session = self._sessions.pop(target, None)
        if session is None:
            raise UnknownSessionError(target)
        self._focus_ended(target)

    def live(self) -> tuple[Session, ...]:
        """The roster, in the order the Sessions were first seen."""
        return tuple(held for held in self._sessions.values() if held.is_live)

    def addressable(self) -> tuple[Session, ...]:
        """The live rows the user may be told about and reply to, in first-seen order.

        **What a surface picking a Session reads, where `live()` is what the
        roster holds.** The two parted when the roster grew rows that are kept
        and not offered: a Child Process (#68, #79) and, since #319, a Headless
        Run. A surface reading `live()` to choose a target offers one of those
        and is then refused by `resolve` a moment later — or, worse, is not
        refused, because it chose the only row there was.
        """
        return tuple(held for held in self._sessions.values() if held.is_addressable)

    def sole_live(self) -> Session | None:
        """The one addressable Session on the roster, or None when there are none or several.

        Child Processes are not counted: a Session that spawned a subagent has
        not become two Sessions the user must choose between (#68), and counting
        one would make a roster of one look like a roster of many to every
        caller that asks this in order to skip an ambiguity. A Headless Run is
        not counted for the same reason and one more: `spoken_first` reads this,
        and a roster of one real Session beside one Headless Run would otherwise
        report two — so the Session the user is owed a word about would only
        ring (#319).
        """
        main = self.addressable()
        return main[0] if len(main) == 1 else None

    def all(self) -> tuple[Session, ...]:
        """Every Session held, ended ones included, in the order first seen."""
        return tuple(self._sessions.values())

    def _of(self, agent: AgentKind) -> tuple[Session, ...]:
        return tuple(held for held in self._sessions.values() if held.target.agent is agent)

    def _by_session_id(self, target: SessionTarget) -> list[Session]:
        return [
            held
            for held in self._sessions.values()
            if held.target.agent is target.agent and held.target.session_id == target.session_id
        ]


#: The one `Session` field nobody observed, for the row a Stop stands in for.
#: `Session` is the roster's record and requires a workspace; a Stop carries
#: none. Named here, once, and named as unobserved rather than spelled at the
#: call site as a plausible-looking `Path("/")` — a stand-in that reads like a
#: fact is the thing to avoid. No brief reads it, and `Briefing` prints the
#: address for a row with no name (`spoken_name`'s own floor).
UNOBSERVED_WORKSPACE: Final = Path()


def stand_in(target: SessionTarget, *, first_seen: float) -> Session:
    """A row for a Stop the roster holds no Session for, carrying only its address.

    A Stop can arrive for a Session no discovery pass has landed yet, and a
    notice nobody receives is worse than one naming the Session by the address
    the user can still answer it by. **Nothing here is invented**: there is no
    name, the progress stays the default `NOT_READ` — Briefing's word for
    "nobody looked" — the workspace is unobserved, and the wait and the state
    are written by `set_stop_reading` from the reading the Stop itself carried.
    `first_seen` is the one fact the registry owns and this moment supplies.
    """
    return Session(target=target, workspace=UNOBSERVED_WORKSPACE, first_seen=first_seen)


def spoken_name(session: Session) -> str:
    """What to call one Session out loud: its Session Name, else its address.

    One answer for saying and showing. When no source has supplied a name,
    the address remains the honest floor; no name is invented (ADR 0024).
    """
    if session.name is not None:
        return str(session.name)
    return spoken_target(session.target)


def spoken_target(target: SessionTarget) -> str:
    """One identity, said out loud. The floor under every name."""
    return f"{target.agent} {target.session_id or f'pid {target.pid}'}"
