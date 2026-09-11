"""The pty: start a Session by hand, type into it, stop it (#350, #352).

**Responsibilities held here** (§9's `hand_started.py`): starting the ordinary
`claude` / `codex` binary on a **controlling terminal**, typing a turn into it,
stopping it, and the Codex boot turn of §3.

The launch rules are §4.4's, and they are launch rules rather than any step's
(#73): the binary resolved on the login shell's PATH and never a shell function;
`login_tty` in a separately exec'd shim and never `preexec_fn`, because the
harness is threaded; the environment scrubbed of `CLAUDE_CODE_*`, `CLAUDECODE`,
`CLAUDE_PID` and `CLAUDE_EFFORT`; `HOME` and `PATH` extended and never replaced;
`start_new_session=True` even though the shim calls `setsid()`; never waiting on
a Codex rollout file before typing; and a settle between the text and the submit
(`deadlines.SUBMIT_SETTLE_SECONDS`).

**The screen is never parsed** to judge anything. Both TUIs redraw with cursor
addressing, and `codex`'s output in a pty interleaves to roughly one glyph per
line once the escapes are stripped. `screen_tail` exists so a failure message
can quote something a human can read; nothing in this harness decides anything
from it, and the pty log is written to the run directory as evidence only.

Three of these were measured rather than remembered (2026-08-26 and 2026-09-02,
against `claude` 2.1.246 and `codex-cli` 0.149.1):

* **the environment scrub** — a `claude` started with Claude Code's own markers
  inherited is treated as one of its children rather than as a Session:
  transcript off, and absent from `claude agents --json` altogether (#73). This
  harness is itself run from inside a Claude Code session, so the markers are
  always there to inherit.
* **the shell function is not the command** — `~/.zshrc` on this machine
  redefines `claude` and `codex` as functions routing into another product. The
  launch resolves and execs the **binary**, and there is no shell in it at all.
* **a pty is not a controlling terminal** — `ps -o tty=` names only the latter,
  the engine's Codex roster is built on that column, and run `20260902T041923Z`
  measured a red `roster` against a composition rule that was correct (#208).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import deadlines

#: Everything Claude Code exports into a process it spawned. Scrubbed so the
#: Session the harness starts is a **main** Session — the only kind this
#: acceptance covers.
AGENT_MARKER_PREFIX = "CLAUDE_CODE_"
AGENT_MARKER_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")

#: A terminal the size a person's would be. A pty opens at 0×0, and a TUI given
#: no room lays out against a width it never has — deterministic geometry costs
#: one ioctl and removes a whole class of "it rendered differently that time".
TERMINAL_ROWS = 40
TERMINAL_COLUMNS = 120

#: How much of the raw stream a failure message may quote. The whole stream goes
#: to disk regardless; this is the tail.
SCREEN_TAIL_BYTES = 8000

#: How much is **held in memory** to cut that tail out of. More than the tail
#: because the escapes are stripped afterwards and a redrawing TUI is mostly
#: escapes: four times was measured to leave a readable 8000 characters on both
#: TUIs, and it bounds what a five-minute run can accumulate.
TAIL_BUFFER_BYTES = 4 * SCREEN_TAIL_BYTES

#: One read of the pty. A page-aligned chunk; the reader loops, so this is a
#: buffer size and not a limit on anything.
READ_CHUNK_BYTES = 65536

#: The one line that stands between a pty and a Session the product can see.
#:
#: `os.login_tty` is the standard spelling: `setsid()`, then `TIOCSCTTY` on the
#: pty. It runs in a **separately exec'd shim** rather than in a `preexec_fn`,
#: and that is the threading rule rather than a preference — `preexec_fn` runs
#: between `fork()` and `exec()` in *this* process, which is threaded by design
#: (two lanes at once, each with a pty reader thread), exactly the case Python
#: documents it as unsafe for. Its failure mode is a child that deadlocks before
#: exec: a hung acceptance run, hours in, with nothing to attribute it to. The
#: shim pays one interpreter start instead, in a process with one thread.
#:
#: **What `execv` keeps**, measured on this machine on 2026-09-02: the pid (so
#: the roster's row and the Session's pid are the TUI's own), the process group
#: (`pgid == pid`, which is what `stop()` signals), the cwd and the environment.
#: What it replaces is the process image: after the exec, `ps -o args=` shows the
#: ordinary command, so the shim is invisible to everything downstream.
TAKE_THE_TERMINAL = "import os, sys; os.login_tty(0); os.execv(sys.argv[1], sys.argv[1:])"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]")


def extended_path(path_value: str, inherited: str) -> str:
    """The login shell's PATH first, then whatever this process already had.

    §4.4 says PATH is **extended, never replaced**, and both halves matter: the
    login shell's is what the engine and the agents are found on (a launchd PATH
    finds neither), and dropping an entry this process was started with would be
    the harness quietly changing the environment it is accepting.
    """
    entries = [*path_value.split(os.pathsep), *inherited.split(os.pathsep)]
    return os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))


@dataclass(frozen=True)
class TerminalEnvironment:
    """What a launch hands the child, and the markers it took away to get there.

    The two are one answer and travel as one. `scrubbed` is the evidence for
    `environment`: it is the set this call removed, computed where the removal
    happened. Anyone who recomputes it later has to read some environment to do
    it, and the only one they have is their own process's — which is a fact
    about the harness's machine and not about the launch (§4.4, #362).
    """

    environment: dict[str, str]
    scrubbed: list[str]


def terminal_environment(
    path_value: str,
    *,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> TerminalEnvironment:
    """The environment a terminal the operator opened would carry, not this agent's.

    `extra` is how a lane adds what only it needs — the Claude lane's
    `CLAUDE_CONFIG_DIR` (§4.1), which has to reach the Session *and* the lane's
    engine, so it is passed in rather than decided here.
    """
    inherited = dict(os.environ if base is None else base)
    markers = set(agent_markers(inherited))
    environment = {name: value for name, value in inherited.items() if name not in markers}
    environment["PATH"] = extended_path(path_value, inherited.get("PATH", ""))
    environment["TERM"] = "xterm-256color"
    environment.update(extra or {})
    # Read off the finished environment rather than off `markers`: a lane's
    # `extra` is applied last and could put one back, and a name the child is
    # holding is not one this launch took away.
    return TerminalEnvironment(
        environment=environment,
        scrubbed=[name for name in sorted(markers) if name not in environment],
    )


def agent_markers(environment: Mapping[str, str]) -> list[str]:
    """Every Claude Code marker in an environment, named once for both readers."""
    return sorted(
        name
        for name in environment
        if name.startswith(AGENT_MARKER_PREFIX) or name in AGENT_MARKER_NAMES
    )


def resolve(binary: str, path_value: str) -> Path | None:
    """Where the ordinary command really is — the binary, never the shell function."""
    found = shutil.which(binary, path=path_value)
    return Path(found) if found else None


def launch_arguments(flags: tuple[str, ...], boot_words: str | None) -> tuple[str, ...]:
    """A lane's argv: its flags, then its boot prompt — last, and never empty.

    Both rules are load-bearing and neither is visible in a tuple literal.
    **Last**, because both agents take the prompt as a positional
    (`codex [OPTIONS] [PROMPT]`), so a prompt written before a flag is read as
    that flag's value. **Never empty**, because emptiness is what the update gate
    tests: `codex` skips it when handed a non-empty `PROMPT`
    (`let skip_update_prompt = cli.prompt.as_ref().is_some_and(|p| !p.is_empty())`,
    read at `tui/src/lib.rs:995` on #107), so `""` is a launch that stops at the
    gate with nothing to show for it — silently, and only in the weeks after a
    release, which is the worst way for a harness to be wrong (#110).
    """
    if boot_words is None:
        return flags
    if not boot_words.strip():
        raise ValueError(
            "a boot prompt must be non-empty: an empty PROMPT is not a skipped update gate, "
            "it is a launch that stops at the gate with nothing to show for it (#110)"
        )
    return (*flags, boot_words)


class SessionRefused(RuntimeError):
    """The command would not start, so there is no Session to read anything on."""


class Session:
    """A Session the product will list, started by hand in a pty (§4.4)."""

    def __init__(
        self,
        *,
        lane: str,
        binary: Path,
        arguments: tuple[str, ...],
        workspace: Path,
        environment: TerminalEnvironment,
        journal: Any,  # support.Journal — imported nowhere here, to stay a leaf
        transcript: Path,
    ) -> None:
        self.lane = lane
        self.binary = binary
        self.arguments = arguments
        self.workspace = workspace
        self.environment = dict(environment.environment)
        #: What `terminal_environment` took away to build the above. Carried
        #: rather than recomputed — see `TerminalEnvironment`.
        self.scrubbed = list(environment.scrubbed)
        self.journal = journal
        self.transcript = transcript
        self._master: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._tail = bytearray()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0),
        )
        command = [str(self.binary), *self.arguments]
        try:
            self._process = subprocess.Popen(
                # The shim, not the command: it takes the pty as its controlling
                # terminal — and with it a new session and its own process group,
                # so a stop still reaches the TUI and everything it spawned — and
                # then *becomes* the command. See TAKE_THE_TERMINAL.
                [sys.executable, "-c", TAKE_THE_TERMINAL, *command],
                cwd=str(self.workspace),
                env=self.environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                # Kept beside the shim, not replaced by it, and `stop()` is why.
                # `Popen` returns once the child has exec'd *the shim*, so this
                # is what makes the Session its own process group before any stop
                # can read one; the shim's `login_tty` would not, an interpreter
                # start later, and a stop landing in that window would take
                # `os.getpgid` to be this process's group and `killpg` the whole
                # run. Measured on 2026-09-02: dropping this line killed a real
                # pytest run outright. `login_tty` then setsids again, a session
                # leader's kernel refuses that, `login_tty` ignores the refusal
                # and goes on to `TIOCSCTTY` — so the terminal is acquired either
                # way.
                start_new_session=True,
            )
        except OSError as unstartable:
            os.close(master)
            os.close(slave)
            raise SessionRefused(f"{command[0]} would not start: {unstartable}") from None
        os.close(slave)
        self._master = master
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self._reader = threading.Thread(target=self._drain, name=f"pty-{self.lane}", daemon=True)
        self._reader.start()
        self.journal(
            "session.hand_started",
            lane=self.lane,
            command=command,
            workspace=str(self.workspace),
            pid=self._process.pid,
            pty_log=str(self.transcript),
            # About the **child**, not about this process: the row that rests on
            # this line is a claim that the Session was started clean, and a
            # count of the harness's own markers is not that claim. `markers`
            # must be empty; `scrubbed` is what this launch took away, as the
            # launch itself reported it — never a second reading of `os.environ`
            # (#362).
            markers=agent_markers(self.environment),
            scrubbed=self.scrubbed,
        )

    def stop(self) -> None:
        """Ask the whole process group to go, then insist; always close the pty."""
        process = self._process
        self._stop.set()
        if process is not None and process.poll() is None:
            for insistence in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(process.pid), insistence)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    process.wait(timeout=deadlines.SESSION_STOP_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self._reader is not None:
            self._reader.join(timeout=deadlines.SESSION_STOP_SECONDS)
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
        self.journal(
            "session.stopped",
            lane=self.lane,
            pid=process.pid if process else None,
            returncode=process.poll() if process else None,
        )

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # --- driving ------------------------------------------------------------

    def submit(self, words: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        """Type one turn and press return, the way a person does (§4.4).

        The settle is the whole method: `\\r` arriving in the same read as the
        text is taken by both composers as a **newline** rather than a submit,
        and `deadlines.SUBMIT_SETTLE_SECONDS` is the smallest gap measured to
        submit on both TUIs. Nothing is read back — a turn's *end* is read on the
        far side (the agent's own record, the engine's log), never off the
        screen, and never by waiting on a record the first turn is what creates.

        `sleep` is injectable so this rule can be tested at CI speed. Nothing in
        the run passes it.
        """
        self.write(words)
        sleep(deadlines.SUBMIT_SETTLE_SECONDS)
        self.write("\r")
        self.journal("session.submitted", lane=self.lane, words=words)

    def write(self, text: str) -> None:
        if self._master is None:
            raise SessionRefused("nothing is running to type into")
        os.write(self._master, text.encode())

    # --- evidence -----------------------------------------------------------

    def screen_tail(self) -> str:
        """The recent raw stream, escapes stripped — evidence only, never parsed."""
        joined = bytes(self._tail).decode("utf-8", "replace")
        return _ANSI.sub("", joined).replace("\r", "")[-SCREEN_TAIL_BYTES:]

    def _drain(self) -> None:
        """Keep the pty drained, and put every byte in the run's own transcript.

        A reader thread rather than a read on demand, because a pty whose buffer
        nobody empties blocks the TUI writing into it — and a blocked TUI is a
        Session that stops answering for a reason no row would name.
        """
        assert self._master is not None
        with self.transcript.open("wb") as sink:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([self._master], [], [], deadlines.POLL_SECONDS)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    data = os.read(self._master, READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                except OSError as closed:
                    if closed.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not data:
                    break
                sink.write(data)
                sink.flush()
                self._tail += data
                del self._tail[:-TAIL_BUFFER_BYTES]


# --- the Codex boot turn (§3) -----------------------------------------------

#: How Codex brackets a turn in its own record. Measured 2026-08-27 against a
#: real rollout on this machine (codex-cli 0.150.0): two turns, two
#: `task_started`, two `task_complete`, and the file's last record is the second
#: `task_complete`.
TURN_STARTED = "task_started"
TURN_COMPLETE = "task_complete"


def codex_turn_over(rollout: Path | None) -> bool:
    """Whether Codex's own record shows no turn in flight: all started, all complete.

    **Not** "the record stopped growing for a while": a turn waiting on the model
    appends nothing, so silence is a turn that may be over and may be thinking.
    For the boot turn that ambiguity costs the run its meaning — a boot turn
    wrongly called over is a Stop landing where a later item is looking for a
    different one. Codex says which it is, so this asks Codex (#110).
    """
    if rollout is None:
        return False
    started = complete = 0
    try:
        with rollout.open() as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line half-written while this read it
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                kind = payload.get("type") if isinstance(payload, dict) else None
                started += kind == TURN_STARTED
                complete += kind == TURN_COMPLETE
    except OSError:
        return False
    return started > 0 and complete >= started


def codex_rollout(codex_home: Path, workspace: Path, since: float) -> Path | None:
    """This Session's rollout, if `codex` has written one yet — re-located, never cached.

    Written when the first **turn** starts, not when the Session starts
    (measured 2026-08-26), which is why nothing in this harness waits on it
    before typing: the harness would be waiting for a file the typing creates.

    The first line is `session_meta`, carrying `session_id` and `cwd`. The
    workspace is compared by realpath because `session_meta.cwd` is resolved.
    """
    root = codex_home / "sessions"
    if not root.exists():
        return None
    wanted = os.path.realpath(workspace)
    for rollout in sorted(root.rglob("rollout-*.jsonl"), key=lambda one: one.stat().st_mtime):
        if rollout.stat().st_mtime < since:
            continue
        meta = first_session_meta(rollout)
        if meta is not None and os.path.realpath(str(meta.get("cwd", ""))) == wanted:
            return rollout
    return None


def first_session_meta(rollout: Path) -> dict[str, Any] | None:
    """The rollout's opening record, or nothing at all."""
    try:
        with rollout.open() as lines:
            for line in lines:
                record = json.loads(line)
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return payload if isinstance(payload, dict) else None
                return None
    except (OSError, json.JSONDecodeError):
        return None
    return None


# --- ground truth -----------------------------------------------------------
#
# What the harness knows about the Session it started, independently of the
# product, so `roster` is judged rather than taken on the engine's word.


@dataclass(frozen=True)
class GroundTruth:
    """Who the harness started, according to the agent itself.

    `session_id` is empty until the agent has one — `codex` writes the rollout
    that names it when the first *turn* starts — which is why §2 item 1 matches a
    roster row by `session_id` **where the agent has one, else by pid**.
    """

    session_id: str
    pid: int
    workspace: Path
    record: Path | None = None

    def describe(self) -> str:
        return (
            f"{self.session_id or '<no session id yet>'} (pid {self.pid}, "
            f"workspace {self.workspace}, "
            f"record {self.record.name if self.record else None})"
        )


def claude_ground_truth(pid: int, environment: Mapping[str, str]) -> GroundTruth | None:
    """The official roster, filtered to one pid: `claude agents --json`.

    Read by the harness as an **oracle**, never as a substitute for the product:
    what `roster` asserts is that the engine reports what this returns. Run with
    the Session's own environment, because `CLAUDE_CONFIG_DIR` is what decides
    which registry is being listed (§4.1).
    """
    for row in claude_rows(environment):
        if row.get("pid") != pid:
            continue
        return GroundTruth(
            session_id=str(row.get("sessionId", "")),
            pid=pid,
            workspace=Path(str(row.get("cwd", ""))),
        )
    return None


def claude_rows(environment: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every row `claude agents --json` shows. Empty when it cannot be read."""
    binary = resolve("claude", environment.get("PATH", os.environ["PATH"]))
    if binary is None:
        return []
    try:
        finished = subprocess.run(
            [str(binary), "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=deadlines.AGENT_ROSTER_SECONDS,
            env=dict(environment),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        rows = json.loads(finished.stdout)
    except json.JSONDecodeError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def codex_ground_truth(
    started: int, codex_home: Path, workspace: Path, since: float
) -> GroundTruth:
    """What the harness knows about the `codex` it started — and it always knows something.

    The oracle for this lane is **the process the harness itself started**, which
    is not a workaround but the same evidence the product's own Codex discovery
    has: it enumerates running `codex` TUIs by pid and cwd. The session id joins
    later, from the rollout, once there is one.
    """
    rollout = codex_rollout(codex_home, workspace, since)
    meta = first_session_meta(rollout) if rollout else None
    return GroundTruth(
        session_id=str(meta.get("session_id", "")) if meta else "",
        pid=tui_pid(started),
        workspace=workspace,
        record=rollout,
    )


def tui_pid(started: int) -> int:
    """The process that *is* the Session, starting from the one the harness ran.

    **Measured on 2026-08-26: `codex` on this machine is an npm shim.** The thing
    on PATH is a node script that spawns the real binary as a child, so the pid
    the harness holds is the shim's and the TUI — the process that draws the
    interface and writes the rollout — is one level down. The product reports the
    **native** one, and that is the right answer rather than a discrepancy to
    paper over: a Homebrew or direct install has no shim at all.

    **The join is ancestry, and only then the argv**: the search is restricted to
    descendants of the pid this harness started, which is what makes the answer
    *this* Session rather than a coincidence. Deliberately not shared with the
    product's own classifier — an oracle that imported the code it is checking
    would turn `roster` into the product agreeing with itself.
    """
    if _is_native_codex(started):
        return started
    for pid in _descendants(started):
        if _is_native_codex(pid):
            return pid
    return started


def _is_native_codex(pid: int) -> bool:
    argv = _argv_of(pid)
    return bool(argv) and Path(argv[0]).name == "codex" and Path(argv[0]).suffix == ""


def _descendants(pid: int) -> list[int]:
    """Every process below this one, breadth first. Empty if `ps` cannot say."""
    listing = _ps(["-axo", "pid=,ppid="])
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    queue = list(children.get(pid, ()))
    while queue:
        current = queue.pop(0)
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def _argv_of(pid: int) -> list[str]:
    return _ps(["-p", str(pid), "-o", "args="]).strip().split()


def _ps(arguments: list[str]) -> str:
    try:
        return subprocess.run(
            ["/bin/ps", *arguments],
            capture_output=True,
            text=True,
            timeout=deadlines.PROCESS_TABLE_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
