"""What Claude Sessions are running, asked of Claude Code's own roster.

`claude agents --json` is the **official** answer to "what is running", and it is
launch-independent: it lists Sessions this engine never started, which is what
makes a bridge over the user's own Sessions possible at all (#70). Nothing here
reads a transcript or a lock file — one command, one JSON document, mapped onto
the seam field for field.

**One fact the roster cannot state is read beside it** (#319): whether each
listed pid has a controlling terminal, which is what tells a Session from a
Headless Run (ADR 0020 as amended). It is one `ps` for the whole pass, joined
against the pids the roster already named, and it decides nothing — the tier is
Bridge Core's, from the fact both lanes carry.

**Coverage is per `CLAUDE_CONFIG_DIR`, and that is a decision rather than a
limit** (#71). Claude Code keeps its Session registry inside the config
directory, so this command answers for one directory and installation writes to
the same one. Simon scoped v1.0 to the main account on 2026-08-26, so that is
one whole universe and not a gap.

**A child Session is absent from this roster, and the roster is right.** A
`claude` that inherits `CLAUDE_CODE_*` / `CLAUDECODE` / `CLAUDE_PID` from a
parent agent runs as a child: transcript saving off, and not listed here (#73,
measured). So every row this returns is a main Session, and #79 owns finding the
children some other way.

**A `waiting` row carries `waitingFor`**, the same label the registry record
carries, and it is read the same way — through `waiting_labels.py`, so this
reader and the Reply Window sweep cannot disagree about what a wait is (#150).

**No version pin.** #71's decision, taken knowingly: this rides surface that may
move, and the safeguard is honest failure rather than a gate that would refuse
every Session on the machine the day after an upgrade. `PROVEN_AGAINST_VERSION`
is documentation for the next re-probe.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent import _terminals
from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.claude import waiting_labels
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    ProgressObservation,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

_log = logging.getLogger(__name__)

#: The command, and the flag that makes it answerable by a machine. `--all` is
#: deliberately not passed: it adds completed background agents, and a roster of
#: Sessions the user can be told about is a roster of ones that are running.
ROSTER_COMMAND: Final = ("claude", "agents", "--json")

#: The build every shape below was read off. Documentation for the next
#: re-probe, never a gate — see this module's docstring. First read on Simon's
#: machine on 2026-08-26 against 2.1.246; re-probed there on 2026-08-31 against
#: 2.1.251, when the roster's answer for a `shell` Session was measured (#154,
#: below), which is the build this reader is now recorded against.
PROVEN_AGAINST_VERSION: Final = "2.1.251"

#: How long the roster command is given before it is treated as unavailable. A
#: discovery that hangs is a discovery loop that stops, so this is a ceiling on
#: the whole lane rather than a guess at how fast the command is.
COMMAND_TIMEOUT_SECONDS: Final = 15.0

#: What Claude Code calls a Session the user is sitting in front of. Other kinds
#: exist behind `--all` and are not Sessions in this product's sense.
INTERACTIVE_KIND: Final = "interactive"

#: `status` walks these across one turn (#73, measured). Anything else is a
#: Session doing something this build has not seen a word for, which is
#: `RUNNING` — the reading that keeps a Relay waiting rather than delivering it
#: into a state nobody has looked at.
#:
#: **Three words, and `shell` is deliberately not a fourth** (#154). The
#: registry has a fourth status — `idle` with a background task still running —
#: but this command does not publish it: measured on 2.1.251, `claude agents
#: --json` reported a pid `busy` at the same moment that pid's registry record
#: read `status: "shell"`. So the roster projection reports what the roster
#: itself says, and adding `shell` here would document a row this command has
#: never produced. The measurement is beside `registry.PROVEN_AGAINST_VERSION`,
#: and the readers that act on it read the record rather than the roster:
#: `window.py` for the Reply Window, and the adapter's own per-row registry
#: overlay for the state word on the roster row (#325) — which is where a
#: `shell` Session stops reading `running` beside a Stop Notice that reads
#: `waiting on`, without this projection claiming a row it never saw.
STATUS_WORDS: Final = {
    "idle": SessionState.IDLE,
    "busy": SessionState.RUNNING,
    "waiting": SessionState.WAITING,
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What running the roster command produced."""

    code: int
    stdout: str
    stderr: str


#: How the roster command is run. Injected so the mapping can be tested against
#: measured bytes rather than against whatever `claude` this machine has.
Runner = Callable[[list[str]], Awaitable[CommandResult]]


async def run_command(argv: list[str]) -> CommandResult:
    """Run one command and collect it. The only place this lane touches a process."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return CommandResult(
        code=process.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )


async def discover(
    *, run: Runner = run_command, projects: ProjectNames | None = None
) -> LaneDiscovery:
    """Every Claude Session running under this config directory, or why none.

    **A lane that could not look says so; it never reports an empty machine.**
    The two are the same shape and opposite facts, and Bridge Core acts on the
    difference: an error leaves the roster's Claude rows exactly as they were,
    while an empty answer ends them.
    """
    try:
        result = await run(list(ROSTER_COMMAND))
    except (OSError, TimeoutError) as unreachable:
        return LaneDiscovery(error=f"could not run `{' '.join(ROSTER_COMMAND)}`: {unreachable}")

    if result.code != 0:
        said = (result.stderr or result.stdout).strip() or "no output"
        return LaneDiscovery(
            error=f"`{' '.join(ROSTER_COMMAND)}` exited {result.code}: {said[:400]}"
        )

    try:
        document: Any = json.loads(result.stdout)
    except json.JSONDecodeError as unreadable:
        return LaneDiscovery(
            error=f"`{' '.join(ROSTER_COMMAND)}` did not answer with JSON: {unreadable}"
        )
    if not isinstance(document, list):
        return LaneDiscovery(
            error=(
                f"`{' '.join(ROSTER_COMMAND)}` answered with "
                f"{type(document).__name__}, not a list of Sessions"
            )
        )

    rows = await _rows(document, projects or ProjectNames())
    return LaneDiscovery(rows=tuple(await _with_terminals(rows, run)))


async def _with_terminals(rows: list[SessionInspection], run: Runner) -> list[SessionInspection]:
    """Say of every row whether a person can type into it (#319, ADR 0020 amended).

    **One `ps` for the whole pass, never one per pid.** The roster names every
    Session's pid, so the process table is asked once and joined against them —
    a second subprocess beside the roster command this pass already ran, on a
    five-second cadence, whatever the machine holds.

    **The lane reports and decides nothing.** A row with no controlling terminal
    is carried exactly like one that has it; which tier that makes it is Bridge
    Core's rule for both lanes (`core/sessions.py::Session.is_headless_run`). A
    pid the table no longer holds, and every pid when the read fails, is `None`
    — not read, and therefore not a claim that nobody is there.
    """
    terminals = await _terminals.by_pid(
        [row.target.pid for row in rows if row.target.pid is not None],
        run=lambda argv: _stdout(run, argv),
    )
    return [
        replace(row, has_controlling_terminal=terminals.get(row.target.pid))
        if row.target.pid is not None
        else row
        for row in rows
    ]


async def _stdout(run: Runner, argv: list[str]) -> str:
    """This lane's runner, as the shape the shared terminal reader asks for."""
    return (await run(argv)).stdout


async def _rows(document: list[Any], projects: ProjectNames) -> list[SessionInspection]:
    """Every row that can be read, skipping the ones that cannot.

    One unreadable row is not a broken roster, and refusing the whole document
    over it would hide every healthy Session on the machine. The skip is logged
    so it is a thing somebody can find rather than a silence.
    """
    found: list[SessionInspection] = []
    for row in document:
        if not isinstance(row, dict):
            continue
        inspection = _inspection(row)
        if inspection is None:
            _log.info("skipped a roster row this build cannot address: %r", row)
            continue
        found.append(await _named(inspection, row, projects))
    return found


async def _named(
    inspection: SessionInspection, row: dict[str, Any], projects: ProjectNames
) -> SessionInspection:
    """Carry project resolution and the roster's raw name; Core composes."""
    return replace(
        inspection, project_name=await projects.of(inspection.workspace), derived_name=_name(row)
    )


def _inspection(row: dict[str, Any]) -> SessionInspection | None:
    """One roster row as the seam holds it, or `None` if it is not addressable."""
    session_id = row.get("sessionId")
    pid = row.get("pid")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        # A Claude target without a pid is ambiguous by construction: `--resume`
        # forks a second process under the same session id.
        return None
    kind = row.get("kind")
    if isinstance(kind, str) and kind.strip() and kind != INTERACTIVE_KIND:
        # Only a stated non-interactive kind is skipped. A row that does not say
        # is kept: this command is not asked for the other kinds, so a missing
        # field is far more likely to be a field that moved than a Session that
        # is not one — and blanking the roster over it is the worse mistake.
        return None

    state = STATUS_WORDS.get(str(row.get("status", "")), SessionState.RUNNING)
    cwd = row.get("cwd")
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id.strip(), pid=pid),
        # Already a realpath when Claude Code writes it (#73). Kept as given, so
        # a join against it compares what the agent itself believes.
        workspace=Path(str(cwd)) if isinstance(cwd, str) and cwd.strip() else Path(),
        lifecycle=SessionLifecycle.LIVE,
        state=state,
        waiting_for=_waiting_for(state, row.get("waitingFor")),
        # `progress` and `last_activity` are transcript facts (#76). `startedAt`
        # is on this row and is deliberately not read as either: when a Session
        # began is not when it last did anything.
        progress=ProgressObservation(),
        last_activity=None,
    )


def _waiting_for(state: SessionState, label: Any) -> WaitingFor:
    """What the roster alone can honestly say a Session is waiting for.

    A `waiting` row returns the shared classifier's `WaitingFor` unchanged
    (#150, #155). That keeps its named, never-a-stop and catch-up answers equal
    to the Reply Window sweep's (`waiting_labels.py`).

    This is only the base for `_overlay` (`adapter.py:618-691`), where a parked
    dialog attaches its handle. Bridge Core reads that current handle rather
    than a delivered-wait ledger (`core/bridge.py:765-793`, #161).

    The classifier's whitelist is adapted from legacy's hook-event rule
    (`legacy@1d32845:bridge/daemon.py:130-142,1470-1479`). Projecting it onto a
    hook-less Session is new ground: legacy's roster was its own registration
    store (`legacy@1d32845:bridge/store.py:1644,1930`), so such a Session did not
    exist there to be handled.
    """
    if state is not SessionState.WAITING:
        return WaitingFor()
    return waiting_labels.classify(label if isinstance(label, str) else None).waiting_for


def _name(row: dict[str, Any]) -> str | None:
    """The task half of this Session's name, straight off the official roster."""
    name = row.get("name")
    return name if isinstance(name, str) else None
