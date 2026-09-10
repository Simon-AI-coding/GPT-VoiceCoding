"""Every wait the harness makes, as one named number each (#350).

**Responsibilities held here:** the harness's deadlines, and the one way to wait
on one.

`docs/acceptance-design.md` §7 fixes the *rule* and no number: every wait is a
**deadline on something ending**, never a window an event is watched in; every
one is a named configurable constant kept in one place; a deadline hit is FAIL
with the verdict naming what never ended; and there is no per-item tuning. The
numbers below are derived from the measurements the spec cites, each with its
reason written beside it, so raising one is an argument rather than a nudge.

`wait` looks its budget up in `DEADLINES` **by name**, which is what keeps the
rule enforceable: a call site cannot pass a number, only choose one of these.
`tests/test_harness_contract.py` reads the other modules and fails if any of
them spells a number for a wait.

This module is a **leaf** — it imports one product constant and nothing of the
harness — so every other module can reach it without a cycle. §9 suggests the
deadlines live in `conftest.py`; they do not, because a conftest is loaded by
path and is not an importable name (`tests/test_layout.py`), and the fast suite
has to be able to read them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from gpt_voicecoding.adapters.agent.claude import settings
from gpt_voicecoding.control_plane import client

#: The whole two-lane run, §1 rule 3 and §7. The one number the spec *does* fix.
#: Not a deadline anything waits on — the run is measured against it and a run
#: that drifts past it is visible without being red — so it is stated here with
#: the rest rather than left for a reader to find in prose.
RUN_BUDGET_SECONDS = 300.0

#: One real agent turn ending. Measured at 4–26 s across both lanes at `9694b1e`
#: (§7), so this is roughly three and a half times the slowest reading: headroom
#: for a cold model or a slow machine, and still short enough that three turns on
#: each of two parallel lanes fit inside `RUN_BUDGET_SECONDS`.
TURN_SECONDS = 90.0

#: The Codex lane's boot turn (§3). It waits out a TUI's **boot and** its first
#: turn, and boot alone has been measured at a whole turn's length on this
#: machine (`codex` sat in `starting MCP servers` for tens of seconds). So two
#: turns, one for each half; a lane still unsettled after that is blocked rather
#: than typed into, because everything after it would be reading a Session with a
#: turn still running underneath.
BOOT_TURN_SECONDS = 2 * TURN_SECONDS

#: A Telegram reply reaching the Session and the turn it drives ending
#: (`companion inbound`, §2 item 5). One turn plus a round trip through
#: Telegram's servers, which is the only wait in this harness that crosses a
#: network the machine does not own.
TELEGRAM_ROUND_TRIP_SECONDS = 30.0
REPLY_SECONDS = TURN_SECONDS + TELEGRAM_ROUND_TRIP_SECONDS

#: `bridgectl relay` answering its receipt (§2 item 3). Derived from the
#: product's own proof of delivery rather than picked: the engine waits
#: `DEFAULT_ACK_TIMEOUT_SECONDS` for the Session to acknowledge, and the receipt
#: is emitted when that wait resolves — so the harness has to outlive it rather
#: than race it. `bridgectl`'s own 10 s default cannot reach the reply, which is
#: why this is passed explicitly wherever the relay is driven.
RELAY_RECEIPT_SECONDS = settings.DEFAULT_ACK_TIMEOUT_SECONDS + TELEGRAM_ROUND_TRIP_SECONDS

#: The engine noticing a Session that is already running, before an empty roster
#: is taken as the answer (`roster`, §2 item 1). Long enough that a polling
#: discovery has ticked at least once and a slow `codex` boot is not read as a
#: missing Session; short enough that an empty roster is an answer rather than a
#: wait.
ROSTER_SECONDS = 30.0

#: A fresh engine from the installed bundle becoming answerable on its socket.
ENGINE_START_SECONDS = 30.0

#: That engine stopping when the lane is done with it. Shorter than a start: it
#: has a socket to close and a log to flush, not adapters to build.
ENGINE_STOP_SECONDS = 20.0

#: A hand-started Session going when its lane is done with it, per signal (§4.4:
#: the whole process group is asked, then insisted). Shorter than the engine's:
#: a TUI has a screen to give back and no state to write, and a lane that has
#: finished reading is not waiting on anything it does.
SESSION_STOP_SECONDS = 10.0

#: `claude agents --json` answering — the harness's own oracle for who it
#: started (§2 item 1's "where the agent has one"). A read of a local registry,
#: so this is a *hang* budget rather than a wait on work: the answer is there or
#: the command is stuck.
AGENT_ROSTER_SECONDS = 30.0

#: One `ps` answering. The process table is read to find the native `codex`
#: under its npm shim, and a `ps` that has not answered in this long is not
#: going to.
PROCESS_TABLE_SECONDS = 10.0

#: What `bridgectl` itself gives an action that names no deadline — read off the
#: product's own client rather than copied, so the harness waits the surface's
#: wait rather than a second opinion about it.
DEFAULT_SURFACE_SECONDS = client.DEFAULT_TIMEOUT_SECONDS

#: How much longer than its own stated deadline a `bridgectl` call is given
#: before the *harness* gives up on it. Generous on purpose, so a timeout here
#: always means the surface hung and never that the action was merely slow —
#: which is the distinction #28 was about: the CLI carries its own deadline and
#: answers when it expires, so a harness racing it would report the race.
SURFACE_GRACE_SECONDS = 30.0

#: The engine-free realtime probe (§2 item 0b). The figure `docs/app-bundle.md`
#: gives for this probe; the script has no duration flag — it runs until
#: interrupted — so the harness runs it for this long and then SIGINTs it.
PROBE_SECONDS = 30.0

#: That SIGINT's own clean hang-up. The frame total is printed by the shutdown,
#: so a probe killed before it finishes reports nothing at all.
PROBE_GRACE_SECONDS = 20.0

#: The login shell's PATH read (§5: "engine PATH is launchd's, not the login
#: shell's"). **Follows the product's budget and is not a second opinion about
#: it**: the harness refuses a run when it cannot read a PATH, so a mirror that
#: gave up sooner than the shell does would refuse runs the product would have
#: served — which is how #118 was found. `tests/test_app_bundle.py` reads
#: `LoginShellPath.timeout` out of the Swift and fails if the two drift.
PATH_TIMEOUT_SECONDS = 10.0

#: The gap between typing a turn's text into a TUI and submitting it (§4.4). Not
#: a deadline on anything ending — it is the settle both TUIs need, and the
#: smallest gap measured to submit on both.
SUBMIT_SETTLE_SECONDS = 1.5

#: The user-account session lock's identity record appearing after the lock is
#: taken (§5). The two are a few syscalls apart — `flock`, then a truncate and a
#: write — so a second run that loses that race reads an empty file and would
#: report "unknown" for a holder that is about to name itself. This is the width
#: of that window, waited out rather than quoted around; a second is orders of
#: magnitude more than a local `open`, `write` and `flush`, deliberately, since
#: being generous costs a run that is refusing anyway and being tight costs the
#: refusal its whole reason.
HOLDER_RECORD_SECONDS = 1.0

#: How often a wait asks its question again. One cadence for every wait, because
#: per-item polling is per-item tuning by another name.
POLL_SECONDS = 0.5


#: Every deadline above, by name. `wait` resolves its budget through this, so a
#: call site chooses a deadline instead of carrying a number.
DEADLINES: dict[str, float] = {
    name: value
    for name, value in sorted(globals().items())
    if name.endswith("_SECONDS") and isinstance(value, float)
}


class DeadlineExpired(RuntimeError):
    """A wait ran out, and it says what never ended.

    §7: "A deadline hit is FAIL with the verdict naming what never ended." This
    carries the three facts that sentence needs — the thing, the deadline it ran
    out of, and how long that was — so the row and the journal line can both be
    written from it without either restating a number.
    """

    def __init__(self, what: str, *, deadline: str, seconds: float) -> None:
        super().__init__(f"{what} never ended within {seconds:g}s ({deadline})")
        self.what = what
        self.deadline = deadline
        self.seconds = seconds


def wait(
    name: str,
    question: Callable[[], Any],
    *,
    what: str,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Ask a question until it answers, or say what never ended.

    `name` is a key of `DEADLINES`; an unknown one is a `KeyError` at the call
    site rather than a wait with no budget. `what` is the sentence the failure
    reads with — write it as the thing that was supposed to end ("the
    acknowledge turn"), not as the observation that it did not.

    The clock and the sleep are injectable so the rules above can be tested at CI
    speed. Nothing in the run passes them.
    """
    budget = DEADLINES[name]
    expiry = now() + budget
    answer = question()
    while not answer:
        if now() >= expiry:
            raise DeadlineExpired(what, deadline=name, seconds=budget)
        sleep(POLL_SECONDS)
        answer = question()
    return answer
