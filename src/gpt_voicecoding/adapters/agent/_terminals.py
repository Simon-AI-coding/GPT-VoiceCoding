"""Whether a running process has a controlling terminal, read from the process table.

**One fact, one reader, both lanes** (#319, ADR 0020 as amended). A Session has
somewhere a person can type and a Headless Run has not, and the difference is the
controlling terminal `ps -o tty=` prints — `??` for a process that has none. The
tier itself is Bridge Core's (`core/sessions.py::Session.is_headless_run`); this
module is only the reading, and it is shared so that "what `??` means" cannot
come to mean two things on two lanes.

**Batched, never one `ps` per pid.** The Claude lane asks about every pid on its
roster once per discovery pass, which is one subprocess beside the one that pass
already runs. The Reply Window sweep asks about one pid, but it sweeps every
watched Session every second (`claude/settings.py`'s poll interval), so it asks
through `TerminalMemo` — a process keeps the controlling terminal it started
with, so the answer is read once and remembered.

**A read that failed is `None`, and `None` is not `False`.** A pid that left the
table between the lane's own reading and this one has no answer here, and
neither does a `ps` that could not run. Reporting either as "no terminal" would
silence a run on the strength of not having looked; the merge rule in Bridge
Core (`core/sessions.py::climbed_to`) is written against that distinction.

**Legacy citation** (ADR 0010): none. The reference implementation launched and
wrapped the Sessions it knew about (`legacy@1d32845:bridge/daemon.py:1192-1257`),
so a run it had not started did not exist for it and there was no tier to tell —
**new**, for the same reason the process readers around it are.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Awaitable, Callable, Iterable
from typing import Final

_log = logging.getLogger(__name__)

#: macOS `ps`'s explicit answer that a process has no controlling terminal.
#: #144 captured this value on a detached `codex` process whose argv and cwd
#: otherwise looked like a TUI, and 2026-09-09's measurement captured it on
#: every one of the seventeen Witness runs started with `start_new_session=True`.
NO_CONTROLLING_TERMINAL: Final = "??"

#: The whole table, pid and terminal only. `-a` and `-x` together are what make
#: it every user's processes rather than this one's own — the same pair
#: `codex/processes.py` reads its candidates with.
TABLE_COMMAND: Final = ("/bin/ps", "-axo", "pid=,tty=")


def one_command(pid: int) -> list[str]:
    """The argv that asks the table about exactly one process."""
    return ["/bin/ps", "-o", "tty=", "-p", str(pid)]


#: How long either read gets. A ceiling rather than a guess at how fast `ps` is:
#: a discovery pass that blocks on the process table is a discovery loop that
#: stops, and not knowing is an answer this module is allowed to give.
COMMAND_TIMEOUT_SECONDS: Final = 10.0

#: Runs one command and returns its stdout, or raises. Injected for tests, and
#: the same shape `codex/processes.py::Runner` has.
Runner = Callable[[list[str]], Awaitable[str]]


def reads_as(column: str) -> bool | None:
    """What one `tty` column says about a controlling terminal.

    `??` is macOS's own word for "none" and is the only `False` here. A column
    naming anything at all — `ttys004`, `s004` — is a terminal. An empty column
    is neither: `ps` printed no row, because the pid was gone by the time it
    looked, so there is nothing to conclude.
    """
    named = column.strip()
    if not named:
        return None
    return named != NO_CONTROLLING_TERMINAL


async def by_pid(pids: Iterable[int], *, run: Runner) -> dict[int, bool | None]:
    """Which of those pids have a controlling terminal, in one read of the table.

    Every pid asked about is answered, so a caller never has to know whether a
    missing key means "no terminal" or "not read": a pid the table does not
    hold, and every pid at all when the read fails, comes back `None`.
    """
    wanted = list(dict.fromkeys(pids))
    if not wanted:
        return {}
    found: dict[int, bool | None] = dict.fromkeys(wanted, None)
    try:
        listing = await run(list(TABLE_COMMAND))
    except (OSError, TimeoutError) as unreadable:
        # Not a lane error: the lane looked and found its Sessions, and this is
        # one fact about them it could not add. Every row stays a Session.
        _log.info("could not read controlling terminals from the process table: %s", unreadable)
        return found
    for line in listing.splitlines():
        head, _, column = line.strip().partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        if pid in found:
            found[pid] = reads_as(column)
    return found


def of_pid(pid: int) -> bool | None:
    """One process's controlling terminal, read now. `None` when it cannot be read.

    Synchronous because its caller is: the Reply Window sweep is a sync method
    driven by an async poll, and it already reads the registry from disk on the
    same line. Guarded by `TerminalMemo` so that the sweep's own cadence does
    not turn this into a subprocess per Session per second.
    """
    if pid <= 0:
        return None
    try:
        listed = subprocess.run(
            one_command(pid),
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as unreadable:
        _log.info("could not read the controlling terminal of pid %s: %s", pid, unreadable)
        return None
    return reads_as(listed.stdout)


class TerminalMemo:
    """One answer per pid, kept for as long as that pid is watched.

    **A process keeps the controlling terminal it started with**, so re-reading
    it is a subprocess spent to be told the same thing. What is *not* kept is a
    failure: a `None` is the absence of an answer, so it is retried on the next
    sweep, which is how a run misread once climbs to Session on a later pass
    rather than staying silent for its whole life.

    The reader is injected so a test can drive both halves — the answer and the
    number of times it was asked for — without a real process table.
    """

    def __init__(self, *, read: Callable[[int], bool | None] = of_pid) -> None:
        self._read = read
        self._known: dict[int, bool] = {}

    def of(self, pid: int) -> bool | None:
        """That pid's answer, read once and remembered; retried while it is unknown."""
        known = self._known.get(pid)
        if known is not None:
            return known
        answer = self._read(pid)
        if answer is not None:
            self._known[pid] = answer
        return answer

    def forget(self, pid: int) -> None:
        """Drop one pid's answer. Called where the watch on that Session is dropped."""
        self._known.pop(pid, None)
