"""The five lane items, written once and walked by both lanes (#350, #352).

**Responsibilities held here** (§9's `journey.py`): the lane as a value, the
engine-log lines the items read, and the walk of §2's five per-lane items. #352
lays the lane value, the arrangement every item stands on — the boot turn, the
daemon membership, the switches, the drained boot notice — and the first item,
`roster`. The remaining four are **#353's**, with the three turns of §3.

The lane is a value rather than a module per lane. The two used to be separate
files whose bodies were the same forty lines with one name changed, and the same
forty lines is where a fix applied to one lane and not the other comes from. So
the difference between the lanes lives entirely in `Lane`, and pytest's own
parametrisation names the lane in the test id (`test_lanes.py`).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import deadlines
import hand_started
import items
import support

from gpt_voicecoding.control_plane.commands import format_address

#: The engine's own line for a Stop it announced (`core/bridge.py`), and the
#: only engine-log line this harness matches at all (§2 item 2 reads the send
#: record beside it; §2 item 5 grades no log line whatsoever).
#:
#: **The record is several lines** (#189): a Stop Notice is a Session Brief and
#: the log carries `Briefing.text` whole, so `Session stopped:` opens the first
#: line and the brief's labelled lines follow. Matched line by line, so this
#: matches once per Stop — the header.
#:
#: `tests/test_bridge.py` asserts the product still writes a first line this
#: matches. That check is the reason this constant is public, and it is why it
#: stayed in this module through #350's rewrite.
ENGINE_STOP_LINE = r"(?i)Session stopped:"

#: The words the Codex lane's TUI is **launched** with (§3's boot turn, #110).
#:
#: The same sentence §3 gives the acknowledge turn, for two reasons: it is the
#: cheapest turn this harness knows — one word out, no tool use, so it can raise
#: no permission with nothing yet allowed to answer one — and a boot prompt is a
#: real turn, so it is billed like one. Its Stop Notice is drained on a
#: pre-`roster` mark, so nothing it produces can be read as a later `stop
#: notice` (#109's shape, read by mark rather than by name).
BOOT_WORDS = "reply with the single word READY, and use no tools"


#: The engine's line for a Companion Channel message it sent, and the ids it
#: sent it under (`core/bridge.py:2175`). Every chat read in this harness is by
#: one of those ids — the chat is never searched and a message is never matched
#: by the Session's name (§9, #109) — so this line is where a read starts, and
#: a **mark** is a position in the lane's engine log taken before the turn whose
#: message the run is about to read.
ENGINE_SENT_LINE = r"sent Companion Channel message .*message_ids="


@dataclass(frozen=True)
class Lane:
    """One lane: one engine, one hand-started Session, one bot, one chat (§2).

    Everything that differs between the lanes lives here, so no other module
    asks which lane it is holding: the launch argv, the boot turn, the config
    directory each lane may or may not have (§4.1), and the agent kind the
    *other* lane's engine drops (§4.3).

    The model pins are the lane's, not a step's (§3). The Codex lane carries
    **no** other launch argument: any `-c` override makes codex-tui run its own
    core instead of joining the shared daemon, and the lane's Session vanishes
    from `roster` (#232).
    """

    name: str
    #: The binary a Session is started from, resolved on the login shell's PATH
    #: and never through a shell function (§4.4).
    binary: str
    #: The agent kind this lane's Sessions are, as the engine spells it
    #: (`seams.identity.AgentKind`) — the name in `[adapters.agents]` and the
    #: first field of every address on the roster.
    agent: str
    #: How this lane's binary spells "use this model" — `claude` takes
    #: `--model`, `codex` takes `-m`. A field rather than a rule inferred from
    #: the other pins: the flags are the lane's, and inferring one is how a lane
    #: comes to be launched with a flag nobody chose.
    model_flag: str
    #: What the lane's turns are billed to (§3).
    model: str
    #: Claude's alone; Codex takes no second pin, for the reason above.
    effort: str | None = None
    #: The product's **own** permission mode, passed rather than overridden:
    #: overriding it would pre-approve the very thing `approval` observes (#60).
    #: Claude's alone; Codex has no such flag and pins nothing here.
    permission_mode: str | None = None
    #: The words this lane's TUI is *launched* with (§3's boot turn), or nothing.
    #: Non-empty is the whole mechanism on the Codex lane: an empty string is not
    #: a skipped update gate (#110).
    boot_words: str | None = None
    #: Whether this lane owns a config directory (§4.1). The Claude lane does;
    #: the Codex lane cannot, under ADR 0022.
    own_config_directory: bool = False

    @property
    def arguments(self) -> tuple[str, ...]:
        """This lane's launch flags, in one place because they are the lane's (§3).

        Built rather than written out so the Codex lane cannot grow a second
        flag by someone adding a field: it has a model and nothing else, and
        `tests/test_harness_lanes.py` reads this tuple for a `-c` on every run
        of the fast suite.
        """
        flags: list[str] = [self.model_flag, self.model]
        if self.effort is not None:
            flags += ["--effort", self.effort]
        if self.permission_mode is not None:
            flags += ["--permission-mode", self.permission_mode]
        return tuple(flags)

    @property
    def dropped_agents(self) -> tuple[str, ...]:
        """The agent kinds this lane's engine must not load (§4.3).

        Only the Claude kind is ever dropped, and only on the lane that will
        never walk a Claude route: the machine's one Claude approval address has
        no environment to read (ADR 0019), so exclusion is what leaves exactly
        one claimant (#202). The Codex kind is never dropped — the Codex daemon
        is machine-wide and both engines see it anyway.
        """
        return () if self.agent == items.LANES[0] else (items.LANES[0],)


LANES: tuple[Lane, ...] = (
    Lane(
        name=items.LANES[0],
        binary="claude",
        agent=items.LANES[0],
        model_flag="--model",
        model="sonnet",
        effort="medium",
        permission_mode="default",
        # Launched silent: `claude` boots into an empty composer, no update gate
        # of the Codex kind has been measured here, and a Session nobody has
        # typed into is what `roster` wants to find.
        boot_words=None,
        own_config_directory=True,
    ),
    Lane(
        name=items.LANES[1],
        binary="codex",
        agent=items.LANES[1],
        model_flag="-m",
        model="gpt-5.6-luna",
        boot_words=BOOT_WORDS,
    ),
)

#: What `[delegate] model` is pinned to in **both** lanes' derived configs (§3):
#: the Delegated Turn runs on codex whichever lane raised it, and a run that
#: copied the operator's own value would bill whatever they last used. Read off
#: the lane rather than spelled again, because they are one bill.
DELEGATED_TURN_MODEL = LANES[1].model


def lane(name: str) -> Lane:
    """The lane a `--lane` name stands for."""
    return next(one for one in LANES if one.name == name)


# --- reading the roster (§2 item 1) -----------------------------------------


def roster_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every row the engine holds, with every field on it.

    Read from `status`'s **payload** rather than from the lines it renders into:
    the rendering carries a name, an address, a workspace, a state and a window
    (`control_plane/commands.py:341`), and the join below needs the `session_id`
    and the `pid` that no rendered line has.
    """
    return [row for row in payload.get("sessions", []) if isinstance(row, dict)]


def roster_row(
    rows: Sequence[Mapping[str, Any]], truth: hand_started.GroundTruth
) -> Mapping[str, Any] | None:
    """The row for the Session the harness started — by id, else by pid (§2 item 1).

    The session id is the exact key and is used whenever the agent has one.
    There is not always one: `codex` writes the rollout that names it when the
    first *turn* starts (measured 2026-08-26), so before that the only thing the
    two sides can agree on is the process. `SessionTarget` carries the pid and
    the product's own Codex discovery finds rows by pid and cwd, so matching on
    it here is the same join the product has to make — not a weaker one the
    harness invented for itself.
    """
    for row in rows:
        target = row.get("target")
        if not isinstance(target, Mapping):
            continue
        if truth.session_id and str(target.get("session_id")) == truth.session_id:
            return row
        if not truth.session_id and target.get("pid") == truth.pid:
            return row
    return None


def not_a_target(row: Mapping[str, Any], workspace: Path) -> str | None:
    """Why this row is not a real target, or `None` when it is (§2 item 1).

    Two facts and no third: an **address** a surface could name it by, and the
    **workspace** the harness made — which is the join that makes this row this
    Session rather than a coincidence. `roster` grades no reach and no
    provenance: every listed Session is one the bridge talks to, and a route
    that fails surfaces as a delivery failure in `relay` (#68, #73).
    """
    target = row.get("target")
    address = format_address(dict(target)) if isinstance(target, Mapping) else ""
    if not isinstance(target, Mapping) or not (target.get("session_id") or target.get("pid")):
        return f"the roster row carries no address a surface could name it by: target is {target!r}"
    listed = row.get("workspace")
    if not listed or os.path.realpath(str(listed)) != os.path.realpath(workspace):
        return (
            f"{address} is listed against workspace {listed!r}, not the one the harness "
            f"started it in ({workspace})"
        )
    return None


def address_of(row: Mapping[str, Any]) -> str:
    target = row.get("target")
    return format_address(dict(target)) if isinstance(target, Mapping) else ""


# --- walking one lane (§2, §6) ----------------------------------------------


class LaneBlocked(RuntimeError):
    """The lane cannot be walked, and no item of it can be read on this ground."""


class ItemFailed(RuntimeError):
    """One item read the product and the product did not do what §2 says it does."""


#: Voice **off**, Message **on**, Duty **on** for the whole run (§4.2). Armed
#: rather than configured, because there is no configuration key for a switch:
#: a fresh engine answers `switches: duty off, message off, voice off`, so an
#: unarmed run would see no push anywhere and read one cause as four failures.
SWITCH_POSITIONS: tuple[tuple[str, str], ...] = (
    ("voice", "off"),
    ("message", "on"),
    ("duty", "on"),
)


def unarranged(
    lane: Lane,
    *,
    selection: items.Selection,
    journal: support.Journal,
    verdict: support.Verdict,
    why: str,
) -> None:
    """A lane that could not be arranged, said in rows rather than in a traceback.

    Preflight is the only thing that REFUSES (§5): it runs before any engine and
    a refusal means nothing was observed. Past it, a lane whose engine or Session
    will not start has **failed `roster`** — the Session never reached the
    product's roster, whatever stopped it — and everything behind it is SKIPPED
    naming that row. The run then exits non-zero on those rows, which is what
    keeps a lane that quietly did nothing from reading as a lane that passed.
    """
    reference = journal("lane.unarranged", lane=lane.name, why=why)
    if items.Item.ROSTER not in verdict.written(lane.name):
        verdict.record(items.Item.ROSTER, support.FAIL, reference, lane=lane.name)
    skip_the_rest(verdict, lane.name, selection, f"blocked by {lane.name}/{items.Item.ROSTER}")


def skip_the_rest(
    verdict: support.Verdict,
    lane: str,
    selection: items.Selection,
    why: str,
    *,
    after: items.Item | None = None,
) -> None:
    """Every item of this lane with no row yet, SKIPPED and saying why (§6, §7).

    `after` is the item whose own row was just written — the one being blamed —
    so a failure does not skip itself.
    """
    written = verdict.written(lane)
    for item in selection.items:
        if item in items.LANE_ITEMS and item not in written and item is not after:
            verdict.skip(item, why, lane=lane)


@dataclass
class Walk:
    """One lane's walk: the ground it stands on, then the items it was asked for.

    Everything the walk reads is passed in, and the two per-lane *readings* — who
    the agent says this Session is, and whether the shared daemon holds it — are
    callables rather than branches on the lane's name. A `Walk` never asks which
    lane it is walking.
    """

    lane: Lane
    selection: items.Selection
    journal: support.Journal
    verdict: support.Verdict
    bridgectl: support.Bridgectl
    engine: support.Engine
    session: hand_started.Session
    workspace: Path
    #: The agent's own account of the Session this harness started, or `None`
    #: while there is not one yet.
    truth: Callable[[], hand_started.GroundTruth | None]
    #: Whether the turn the *launch* started has ended, on the agent's own
    #: bracketing. Always over on a lane with no boot prompt.
    boot_turn_over: Callable[[], bool] = lambda: True
    #: Whether the shared Codex daemon holds this Session's thread, or `None` on
    #: a lane where the question does not arise.
    membership: Callable[[], support.DaemonMembership | None] = lambda: None
    #: Filled as the walk goes, so a failure message can say where it got to.
    boot_seconds: float | None = None
    #: The clock and the sleep every wait on this lane is made through, as
    #: `deadlines.wait` takes them. Injectable for the reason that function
    #: gives: the rules above are minutes long — the boot turn is two turns —
    #: and what a fast test reads is what the harness *does* when one runs out,
    #: not that it can count. **Nothing in the run passes them.**
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def items_walked(self) -> dict[items.Item, Callable[[], str]]:
        """Every item this ticket walks, bound to the method that reads it.

        The four #353 builds are **not** here, and are not SKIPPED either: a row
        nobody wrote is named by `Verdict.missing`, which is the mechanism §7 has
        for "the run promised this and did not write it". A SKIPPED row would say
        the run reached the item and chose not to read it, which is a different
        and untrue sentence.
        """
        return {items.Item.ROSTER: self.roster}

    # -- the ground every item stands on -------------------------------------

    def walk(self) -> None:
        """Arrange this lane, then read the items this run asked for (§2, §6)."""
        try:
            boot_mark = self.settle_boot_turn()
            self.settle_daemon_membership()
            self.arm_switches()
            self.drain_boot_notice(boot_mark)
        except LaneBlocked as blocked:
            self.block(f"blocked by the {self.lane.name} lane's arrangement: {blocked}")
            return
        walked = self.items_walked()
        for item in self.selection.items:
            if item not in items.LANE_ITEMS:
                continue
            reading = walked.get(item)
            if reading is None:
                self.journal(
                    "item.unwritten",
                    lane=self.lane.name,
                    item=str(item),
                    why="the four remaining items are #353's",
                )
                continue
            if not self.read(item, reading):
                return

    def read(self, item: items.Item, reading: Callable[[], str]) -> bool:
        """One item, its row, and — on a failure — the rows it was holding up.

        A failed item **blocks** what stands on it (§6): the rest of this lane's
        selected items become SKIPPED naming this row, rather than being read on
        ground the run could not arrange.
        """
        started = time.monotonic()
        try:
            evidence = reading()
        except (ItemFailed, LaneBlocked, deadlines.DeadlineExpired) as failed:
            reference = (
                self.journal.expired(failed, lane=self.lane.name, item=str(item))
                if isinstance(failed, deadlines.DeadlineExpired)
                else self.journal(
                    "item.failed", lane=self.lane.name, item=str(item), why=str(failed)
                )
            )
            self.verdict.record(
                item,
                support.FAIL,
                reference,
                lane=self.lane.name,
                seconds=time.monotonic() - started,
            )
            self.block(f"blocked by {self.lane.name}/{item}", after=item)
            return False
        self.verdict.record(
            item,
            support.PASS,
            evidence,
            lane=self.lane.name,
            seconds=time.monotonic() - started,
        )
        return True

    def block(self, why: str, after: items.Item | None = None) -> None:
        """Every item of this lane the run has not written a row for, SKIPPED with why."""
        skip_the_rest(self.verdict, self.lane.name, self.selection, why, after=after)

    def settle_boot_turn(self) -> int | None:
        """Wait out the turn the *launch* started, and mark the engine log behind it.

        A lane with a boot prompt is running a turn from the moment it starts
        (#110 — a non-empty prompt is what carries it past the update gate).
        Nothing may be typed into a Session that is mid-turn, and no mark may be
        taken while one is in flight: a boot turn that ends *after* a later
        mark puts its own Stop Notice on the far side of it, and `stop notice`
        would pass on a notice for a turn nobody drove.

        This waits on the agent's **own** turn boundary rather than on the record
        going quiet, because a turn waiting on the model appends nothing either.
        The mark it answers is spent by `drain_boot_notice`.
        """
        if self.lane.boot_words is None:
            return None
        started = time.monotonic()
        try:
            self.waiting(
                "BOOT_TURN_SECONDS",
                self.boot_turn_over,
                what=f"the turn the {self.lane.name} lane's Session was launched with",
            )
        except deadlines.DeadlineExpired as unsettled:
            self.journal.expired(unsettled, lane=self.lane.name)
            raise LaneBlocked(
                f"the turn this lane was launched with had not ended, so the walk cannot type "
                f"into this Session without racing it. Screen tail: "
                f"{self.session.screen_tail()[-600:]!r}"
            ) from None
        self.boot_seconds = time.monotonic() - started
        self.journal("boot.turn", lane=self.lane.name, seconds=self.boot_seconds)
        return self.mark()

    def settle_daemon_membership(self) -> None:
        """Record whether the product can see this Session at all, and refuse if it cannot.

        **Not an item, and it must not become one** (#232). ADR 0020 defines a
        Codex Session as a daemon thread a terminal vouches for, so a
        hand-started TUI the shared daemon does not hold is a Session the product
        is *right* not to list — grading that is grading the harness's own ground
        as a product defect.

        **Here, because here is where the fact first exists and still costs
        nothing**: the thread id is written into the rollout when the first turn
        starts, and the turn just waited out is that turn. Nothing has been typed
        yet, no switch is armed, no mark has been spent, so a lane blocked at
        this line has cost one boot turn and nothing else.

        It records on **every** path and refuses on one: a daemon that is down,
        moved or unreadable is not evidence that a thread is absent from it.
        """
        membership = self.membership()
        if membership is None:
            return
        self.journal(
            "daemon.membership",
            lane=self.lane.name,
            flags=list(self.lane.arguments),
            thread_id=membership.thread_id,
            held=membership.held,
            held_threads=list(membership.held_threads),
            daemon=membership.daemon,
            reason=membership.reason,
        )
        refusal = membership.refusal(self.lane.arguments)
        if refusal is not None:
            raise LaneBlocked(refusal)

    def arm_switches(self) -> None:
        """Voice off, Message on, Duty on — the text-only mode this run exercises (§4.2)."""
        for name, position in SWITCH_POSITIONS:
            answer = self.bridgectl("switch", name, position)
            self.journal(
                "switch.armed", lane=self.lane.name, switch=name, to=position, reply=answer.text
            )
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")

    def drain_boot_notice(self, mark: int | None) -> None:
        """Let the boot turn's Stop Notice land before any later mark is taken (§3).

        **It records and never asserts**, because *no notice* is a legitimate
        answer: a Stop is only raised on a transition out of `active`, and an
        engine that first saw this thread already idle raised nothing for the
        boot turn. That case is also the one that pays the whole wait.

        Turning an outlet on asks the next discovery pass to reconcile current
        state, so the notice can arrive either side of `arm_switches`; the mark
        is taken **before** that, which is what makes this a deadline on the
        notice landing rather than a window it is watched in.
        """
        if mark is None:
            return
        try:
            sent = self.waiting(
                "TELEGRAM_ROUND_TRIP_SECONDS",
                lambda: self.sent_since(mark),
                what="the boot turn's Stop Notice reaching the chat",
            )
        except deadlines.DeadlineExpired:
            # Not a failure: an engine that first saw this thread already idle
            # raised nothing to drain, and there is no way to tell that apart
            # from a notice that never came — nor any need to.
            self.journal("boot.notice", lane=self.lane.name, drained=None, mark=mark)
            return
        self.journal("boot.notice", lane=self.lane.name, drained=sent[-1], mark=mark)

    # -- the engine log, and the marks every chat read starts from -----------

    def waiting(self, deadline: str, question: Callable[[], Any], *, what: str) -> Any:
        """One wait, on this lane's clock. Every wait in the walk goes through here."""
        return deadlines.wait(deadline, question, what=what, now=self.now, sleep=self.sleep)

    def mark(self) -> int:
        """Where this lane's engine log has got to (§2, §9).

        A mark is a position in the engine's own log, never a clock and never a
        message id read off the chat: every chat read in this harness starts
        from the id the product issued in the line it wrote when it sent the
        message, so what a mark has to say is "everything after here belongs to
        the turn I am about to drive".
        """
        return len(self.engine.log_lines())

    def sent_since(self, mark: int) -> list[str]:
        """Every `sent Companion Channel message …` line the engine wrote after a mark."""
        pattern = re.compile(ENGINE_SENT_LINE)
        return [line for line in self.engine.log_lines()[mark:] if pattern.search(line)]

    # -- the items -----------------------------------------------------------

    def roster(self) -> str:
        """§2 item 1 — the Session has a row, and the row is a real target.

        Two claims and no third:

        1. the Session the harness started by hand — identified from the
           **agent's own** record, never from the engine — has a row;
        2. that row is a *target*: an address a surface could name it by, plus
           the workspace this harness made.

        Unreachability is not this item's business and has no row of its own: a
        Session the daemon cannot reach is refused above (§4.3), and a route that
        fails surfaces in `relay` as a delivery failure with a reason (#73).
        """
        truth = self.waiting(
            "ROSTER_SECONDS",
            self.truth,
            what=(
                f"the {self.lane.binary} Session the harness started appearing in the agent's "
                f"own record"
            ),
        )
        row = self.waiting(
            "ROSTER_SECONDS",
            lambda: roster_row(self.rows(), truth),
            what=(
                f"the Session the harness started appearing in the {self.lane.name} engine's roster"
            ),
        )
        unsound = not_a_target(row, self.workspace)
        if unsound is not None:
            raise ItemFailed(unsound)
        return self.journal(
            "roster.row",
            lane=self.lane.name,
            address=address_of(row),
            workspace=str(row.get("workspace")),
            agent_record=truth.describe(),
        )

    def rows(self) -> list[dict[str, Any]]:
        return roster_rows(
            self.bridgectl.status_payload(
                why="a roster row is matched by session_id else pid, and no rendered line "
                "carries either"
            )
        )
