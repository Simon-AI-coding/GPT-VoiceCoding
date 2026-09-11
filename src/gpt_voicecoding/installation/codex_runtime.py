"""The one codex on this machine, as the three facts everything here needs — #272.

Everything on this side that needs codex is handed these and nothing else:

======================  ====================================================
``executable``          the codex the user gets when they type ``codex``
``control_socket``      ``$CODEX_HOME/app-server-control/app-server-control
                        .sock``, **derived**, never asked of a running process
``launch_environment``  ``CODEX_HOME`` and a ``PATH`` that can find the
                        executable's interpreter
======================  ====================================================

**Superseding #82's managed standalone.** That ticket chose
``$CODEX_HOME/packages/standalone/current/codex`` over the user's ``PATH``
deliberately, so the LaunchAgent and the adapter would derive the same file from
the same ``CODEX_HOME``. What it bought was agreement between two parts of this
product; what it cost was agreement with the user. On the reference machine that
tree held 0.149.1, untouched since 2026-08-25, while the terminal the user typed
in ran the npm 0.153.4 — so this product's shared server and the user's own
`codex` were different programs, and the version disagreement rode out as a
`degraded` note every day (#271, #272). ADR 0022 is the ruling.

**The third fact is here because a prototype failed without it.** The npm codex
is ``@openai/codex/bin/codex.js`` behind ``#!/usr/bin/env node``. Started under
launchd — whose whole ``PATH`` is ``/usr/bin:/bin:/usr/sbin:/sbin`` — it died
with ``env: node: No such file or directory`` and never held the socket. An
``executable`` alone is not a runnable job. `daemon start` never met this
because the managed standalone was a native binary.

**Where the resolution happens: one point on the machine** (Simon's ruling on
#272, option A). The shell already reads the user's login ``PATH`` for the
engine — ``LoginShellPath.swift``, login **and interactive**, sentinel-delimited
— and #272 makes it hand that reading to the installation subprocess. So
resolution here is an ordinary ``which``, and there is no second login-shell
read and no second implementation of the interactive-shell lesson to get wrong.

**Over the machine's ``PATH``, which is stated and never inherited** — #327.
"The ``PATH`` this process was given" was the rule until a terminal turned out
to be a process too: the same render that #275 accepted a terminal *misreading*
was also being written to disk from one, carrying that terminal's own entries.
``machine_path`` is the rule now — what the caller states, else what the
standing job records, and never the ambient ``PATH``.

**Nothing is written down.** ``LoginShellPath.swift``'s own ruling applies
unchanged: a copy of something the user's shell already states goes stale the
day they edit their profile. There is no ``config.toml`` key for the resolved
path, and every reconcile resolves again — which is also, for free, the
re-resolve a `nvm use` or an uninstall needs.

**A shell function or alias cannot reach this.** #82 met one on this product's
author's machine, and `command -v` — what a login shell answers with — reports a
name rather than a path for it. ``which`` over a ``PATH`` cannot: it only ever
answers a file it found in a directory, and only one with the executable bit.
The case is dissolved by where resolution happens rather than guarded against.

**Legacy: dropped.** Generation 1 had no shared app-server to find —
``legacy@1d32845:bridge/codex.py`` drove a launched, wrapped, per-Session
app-server whose path it owned outright.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding import __version__, websocket

#: Codex's own home, and the variable that moves it.
CODEX_HOME_VARIABLE: Final = "CODEX_HOME"
DEFAULT_CODEX_HOME_NAME: Final = ".codex"

#: The variable this resolves over, and the one the job is given.
PATH_VARIABLE: Final = "PATH"

#: How a caller **states** the `PATH` this machine's job is rendered from — #327.
#:
#: Deliberately not `PATH` itself, and that is the whole of the fix. A value read
#: out of `PATH` is one every process inherits, so the render was a function of
#: whoever typed the verb: the shell handed down the login-shell reading and a
#: terminal handed down its own, and `reconcile` wrote whichever it got. #275 met
#: the comparing half of that and accepted it for `status`; the writing half is
#: what put an agent session's own directories — two of them version-pinned
#: plugin caches that a plugin upgrade deletes — into a real `LaunchAgent`.
#:
#: A variable of this product's own cannot be arrived at by inheritance from a
#: login shell, so stating it is an act and never an accident. The shell sets it
#: on the installation subprocess, and only when the reading actually succeeded;
#: `Installation.swift` is the one writer and `tests/test_codex_launch_agent.py`
#: holds the two spellings together, which is the guard #47 records as missing
#: wherever a constant is spelled in two languages.
LOGIN_PATH_VARIABLE: Final = "GPT_VOICECODING_LOGIN_PATH"

#: What the user types. Resolved, never assumed to be anywhere in particular.
EXECUTABLE_NAME: Final = "codex"

#: How codex itself computes the control socket:
#: ``$CODEX_HOME/app-server-control/app-server-control.sock``. Fixed upstream,
#: with no variable and no flag over it — which is exactly why this side may
#: derive it instead of asking a running process where it is listening. The
#: seam is the socket, not the `daemon` subcommand: the server on the other end
#: is nothing but ``codex app-server --listen``, observed on the reference
#: machine's process table (#271).
CONTROL_SOCKET_PARTS: Final = ("app-server-control", "app-server-control.sock")

#: What the job runs. ``--listen`` rather than ``daemon start``, and the
#: difference is a lifecycle: `start` waited for the server's initialize and
#: then **exited**, leaving a server that belonged to no job (`state = not
#: running`), while `--listen` makes the job *be* the server (`state = running,
#: active count = 1`). There is still no `KeepAlive`, so this is not a
#: supervisor — but `launchctl print` now answers "is the shared server up"
#: truthfully, which it never did before (#271 step 2).
SERVER_SUBCOMMAND: Final = "app-server"
LISTEN_OPTION: Final = "--listen"
LISTEN_SCHEME: Final = "unix://"

#: What this client calls itself when it completes the status handshake. The far
#: side records it, so a person reading the server's own logs can see which
#: software held the connection. Deliberately the same name the engine's client
#: uses (`adapters/codex_app_server/process.py`), because to the server they are
#: the same product — and deliberately not imported from it, which ADR 0012
#: forbids and which would make installation need an engine to be constructible.
CLIENT_NAME: Final = "gpt-voicecoding"

#: How long the status handshake gets, end to end — one deadline from the moment
#: the socket is opened, not a fresh one per `recv`. Measured at **1 ms**
#: against a live server on the reference machine (#271 step 7), against 139 ms
#: for the `daemon version` subprocess it replaces — so this is not a budget the
#: ordinary case spends. It is the bound on the case that has no ordinary one: a
#: socket something is listening on that never answers. A person typed `status`
#: and is waiting at a terminal for it.
#:
#: **Per-`recv` was not that bound** (#276). `settimeout` bounds each call, so a
#: peer that sent one byte every four seconds — or a header block with no
#: terminator — renewed the budget for as long as it liked and held `status`
#: open with it. What bounds such a peer is a start time and the remaining time
#: armed before every read, which is what `_Budget` is.
HANDSHAKE_TIMEOUT_SECONDS: Final = 5.0

#: The most of an upgrade's header block this will hold. A `101 Switching
#: Protocols` answer is a handful of headers; a few KiB is already generous, and
#: the ceiling is what stops a peer that sends a valid-looking header block for
#: ever from being bounded only by the clock. Exceeding it is a refusal, the
#: same as any other peer that is not an app-server.
MAX_UPGRADE_HEADER_BYTES: Final = 8 * 1024

#: How much of an unreadable answer is quoted back in a reason. Long enough to
#: recognise what came out, short enough that a peer answering megabytes cannot
#: put them in a status line.
UNREADABLE_ANSWER_QUOTED_CHARS: Final = 120

#: The most this read will hold from one message. An `initialize` answer is a
#: short document; anything past this is a peer this status line has no business
#: buffering, and the bound is what keeps a `bridge-install status` from growing
#: to whatever a peer feels like sending. Deliberately far below the engine
#: client's 32 MiB, which is sized for a whole thread readback and reads
#: nothing of the sort here.
MAX_MESSAGE_BYTES: Final = 1024 * 1024

#: The id this connection's one request carries. A whole number, because the
#: answer is matched on it and JSON-RPC lets a peer echo an id of any shape.
INITIALIZE_ID: Final = 1

#: How many messages this will read past before it gives up looking for that
#: answer. A server may notify on the way — the engine's own client routes such
#: things to a handler — and a status read has nowhere to put them, so it skips
#: them. Bounded so a peer that only ever notifies cannot hold the read open to
#: the socket timeout, once per message, for as long as it likes.
MESSAGES_BEFORE_THE_ANSWER: Final = 32


def default_codex_home(environ: Mapping[str, str], home: Path | None = None) -> Path:
    """The Codex home this run is about."""
    stated = environ.get(CODEX_HOME_VARIABLE)
    if stated and stated.strip():
        return Path(stated.strip()).expanduser()
    return (home or Path.home()) / DEFAULT_CODEX_HOME_NAME


def control_socket(codex_home: Path) -> Path:
    """Where the shared app-server listens. Derived, and never asked of anyone."""
    return codex_home.joinpath(*CONTROL_SOCKET_PARTS)


def executable_on(path: str) -> Path | None:
    """The codex on this ``PATH``, or nothing at all.

    Nothing at all is an answer and not a failure: a machine with no codex is a
    machine whose Codex lane reports itself absent, with the reason. This never
    raises and never invents a path.
    """
    searched = path.strip()
    if not searched:
        return None
    found = shutil.which(EXECUTABLE_NAME, path=searched)
    return Path(found) if found else None


def resolve_executable(environ: Mapping[str, str]) -> Path | None:
    """The codex on this environment's own ``PATH``.

    The engine's, not the installation's. Inside the engine the ambient ``PATH``
    *is* the machine's — the shell spawns it with the login-shell reading — so
    defaulting the adapter's executable from it is the same answer by a shorter
    road. The installation side may not take that road, because it also runs
    where nobody handed it that reading; see :func:`machine_path`.
    """
    return executable_on(environ.get(PATH_VARIABLE) or "")


def machine_path(environ: Mapping[str, str], recorded: str | None = None) -> str | None:
    """The ``PATH`` a job on this machine is rendered from, or nothing — #327.

    Two sources and no third, in order:

    1. **What the caller states**, under `LOGIN_PATH_VARIABLE`. That is the
       login-shell reading, which ADR 0022 keeps to one implementation on this
       machine and puts in Swift; this side is handed the answer and reads no
       shell of its own.
    2. **What the standing job records.** Its `PATH` is what that one reading
       last wrote, so deferring to it defers to the machine's existing record
       instead of making a second one — which is exactly the distinction
       `LoginShellPath`'s "never record a copy of the profile" rule turns on.

    ``None`` when neither answers, and the caller's own ``PATH`` is never the
    third source. A render built from it is a render that is true of one launch,
    which is #38's defect with the environment standing in for the constant.
    """
    stated = (environ.get(LOGIN_PATH_VARIABLE) or "").strip()
    if stated:
        return stated
    return (recorded or "").strip() or None


@dataclass(frozen=True, slots=True)
class CodexRuntime:
    """The one codex on this machine, and how to start and reach its server."""

    executable: Path
    codex_home: Path
    control_socket: Path

    #: The ``PATH`` a job started from this executable needs. See the module
    #: note: without one, an npm codex cannot start under launchd at all.
    path: str

    @property
    def server_arguments(self) -> list[str]:
        """What the LaunchAgent runs after the executable."""
        return [SERVER_SUBCOMMAND, LISTEN_OPTION, f"{LISTEN_SCHEME}{self.control_socket}"]

    @property
    def launch_environment(self) -> dict[str, str]:
        """The whole environment the job is given, and nothing beyond it.

        ``CODEX_HOME`` is written even when it is the default, because launchd
        hands a job none of the user's shell environment: without it a user who
        moved their Codex home would get a server on one home and TUIs on
        another, and an empty roster that nothing explains.

        **The ``PATH`` is this machine's, not one composed here and not the
        caller's** (:func:`machine_path`, #327). #38 forbids a rendered artifact
        naming a path that was true only on the machine that rendered it — and a
        `PATH` inherited from whichever process typed the verb is that defect
        with the environment standing in for the constant, which is how an agent
        session's plugin-cache directories reached a real job. launchd's own
        ``/usr/bin:/bin:/usr/sbin:/sbin`` written into a plist would be exactly
        that — a constant standing in for something the environment already
        states. The directory holding the executable is on this ``PATH`` by
        construction, because that is where ``which`` found it, and nvm and npm
        put ``node`` in that same directory beside the shim. A Homebrew or
        standalone codex is a native binary and needs none of this; the entry is
        harmless there, so there is no branch on install kind.
        """
        return {CODEX_HOME_VARIABLE: str(self.codex_home), PATH_VARIABLE: self.path}


#: Why there is no runtime when nothing answered :func:`machine_path` — #327.
#:
#: Told apart from "there is a `PATH` and no codex on it" because the two send a
#: person to different places: one to install codex, one to the app. It names no
#: value, for `_PATH_PROVENANCE`'s reason — there is no value to name, which is
#: the whole content of the sentence.
NO_MACHINE_PATH: Final = (
    f"nothing states this machine's PATH and no {EXECUTABLE_NAME} job on disk records one"
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """What looking for this machine's codex came to: one of these, never both.

    A ``runtime`` and a ``reason`` rather than an optional and a string passed
    side by side, because the two travel together through every caller and a
    pair that can be split is a pair that will be — an item handed a ``None``
    runtime and somebody else's reason would report the wrong sentence with
    nothing to catch it.
    """

    runtime: CodexRuntime | None
    #: Why there is none. Empty exactly when ``runtime`` is not.
    reason: str
    #: The machine ``PATH`` this looked on, or ``None`` when nothing answered
    #: :func:`machine_path`. Carried rather than left to be read back out of
    #: ``reason``, because the two absences it tells apart are acted on
    #: differently and a caller matching on a sentence would go quietly wrong the
    #: day the sentence is reworded.
    searched: str | None = None

    @property
    def found(self) -> bool:
        return self.runtime is not None

    @property
    def path_unknown(self) -> bool:
        """Nothing stated this machine's ``PATH`` and nothing recorded one — #327.

        The difference between *this machine has no codex* — which is a fact
        about the machine, said out loud while the run carries on (#276) — and
        *this caller has no standing to say what the machine's `PATH` is*, which
        is nothing to report and everything to refuse over: rendering anyway
        would put a `PATH` that is true of one launch into a login job.
        """
        return self.runtime is None and self.searched is None


def resolve(
    environ: Mapping[str, str],
    home: Path | None = None,
    recorded_path: str | None = None,
) -> Resolution:
    """This machine's Codex Runtime, or the reason there is none.

    Never raises. This runs on the reconcile that precedes every engine start,
    from a shell with nowhere to put a traceback.

    **Both answers are the machine's** — #327. The executable is resolved over
    :func:`machine_path` and so is the rendered ``PATH``, because a `which` run
    over the caller's own ``PATH`` decides which codex goes into
    ``ProgramArguments`` and is the same defect one key across.
    """
    codex_home = default_codex_home(environ, home)
    path = machine_path(environ, recorded_path)
    if path is None:
        return Resolution(None, NO_MACHINE_PATH)
    executable = executable_on(path)
    if executable is None:
        return Resolution(None, f"there is no {EXECUTABLE_NAME} on PATH ({path})", path)
    return Resolution(
        CodexRuntime(
            executable=executable,
            codex_home=codex_home,
            control_socket=control_socket(codex_home),
            path=path,
        ),
        "",
        path,
    )


def answering(socket_path: Path, *, budget: float = HANDSHAKE_TIMEOUT_SECONDS) -> str:
    """One sentence about the shared app-server, for a status run to print.

    **Never called on the install path.** A reconcile runs before the engine at
    every launch and has no business dialling anything; a person typing `status`
    is asking exactly this question.

    Three answers, and they are the three states the socket can be in: it is not
    there, it is there with nothing behind it, or something behind it completed
    a codex `initialize`. The middle one is a stale file, which a server that
    was killed rather than stopped leaves behind, and it is worth telling apart
    from the first — the same path with two different reasons.

    `budget` is a parameter for one reason, and it is ``deadline``'s in
    ``InstallationRunner.run``: a test that proved the ceiling by waiting out the
    real one would take five seconds per case. No shipping caller passes it.
    """
    started = time.monotonic()
    if not socket_path.exists():
        return f"the shared app-server is not answering: there is no socket at {socket_path}"
    try:
        answer = _handshake(socket_path, budget)
    except OSError as unreachable:
        return (
            f"the shared app-server is not answering: {socket_path} is there and "
            f"{unreachable.strerror or unreachable}"
        )
    except _HandshakeRefused as refusal:
        return f"the shared app-server is not answering: {refusal}"
    elapsed_ms = (time.monotonic() - started) * 1000
    home = answer.get("codexHome")
    on_home = f", on {home}" if isinstance(home, str) and home else ""
    return (
        f"the shared app-server answered initialize on {socket_path} in "
        f"{elapsed_ms:.0f} ms{on_home}"
    )


class _HandshakeRefused(Exception):
    """Something is listening on the socket, and it is not a codex app-server."""


class _Budget:
    """The handshake's one deadline, armed before each read that can block.

    A peer that never sends is bounded by any timeout at all. A peer that keeps
    sending — one header byte every four seconds, a header block with no
    terminator, a frame arriving a byte at a time — is bounded only by a
    deadline that does not move, which is why the start time is taken once here
    and every ``settimeout`` after it is the *remaining* time (#276).

    Running out is the same ``_HandshakeRefused`` a hung peer produces, because
    to the person who typed `status` they are one answer: whatever is on that
    socket did not complete a handshake.
    """

    __slots__ = ("_seconds", "_started")

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._started = time.monotonic()

    def arm(self, connection: socket.socket) -> None:
        """Give the socket what is left, or refuse because nothing is."""
        remaining = self._seconds - (time.monotonic() - self._started)
        if remaining <= 0:
            raise _HandshakeRefused(self.exhausted)
        connection.settimeout(remaining)

    @property
    def exhausted(self) -> str:
        """The one sentence for running out, wherever the read gave up."""
        return f"it did not finish the handshake within {self._seconds:g} seconds"


def _handshake(socket_path: Path, budget: float) -> dict[str, Any]:
    """Connect, upgrade, `initialize`, and give back what the server said.

    Blocking, and on a socket of its own, because this runs before any event
    loop exists — `bridge-install` is a console script the shell runs, not a
    part of the engine. The protocol it speaks is the engine's, from the one
    place that holds it (`gpt_voicecoding.websocket`); what is written here is
    only how the bytes get on and off this socket.

    `initialized` is sent after the answer because the protocol asks for it and
    a server told nothing would be entitled to wait. Nothing is read after it:
    this connection exists to prove the server is there, and it closes.

    One ``_Budget`` covers all of it, from the socket being opened to the last
    byte read, and a read that runs out of it refuses like any other peer that
    is not an app-server. ``TimeoutError`` is caught here rather than left to
    ``answering``'s ``OSError`` arm for the same reason: a socket that timed out
    on the remaining budget *is* the budget running out, and saying so as an
    unreachable-socket ``strerror`` would name the wrong thing.
    """
    budgeted = _Budget(budget)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            budgeted.arm(connection)
            connection.connect(str(socket_path))
            request, key = websocket.upgrade_request()
            connection.sendall(request)
            header = _read_until(connection, websocket.UPGRADE_TERMINATOR, budgeted)
            if not websocket.accepted(header, key):
                status, _ = websocket.upgrade_answer(header)
                raise _HandshakeRefused(
                    f"whatever is listening on {socket_path} is not a codex app-server "
                    f"(it answered {status!r} to a WebSocket upgrade)"
                )
            _send(
                connection,
                {"id": INITIALIZE_ID, "method": "initialize", "params": _initialize_params()},
            )
            answer = _await_initialize(connection, socket_path, budgeted)
            _send(connection, {"method": "initialized", "params": {}})
    except TimeoutError:
        raise _HandshakeRefused(budgeted.exhausted) from None
    error = answer.get("error")
    if error is not None:
        raise _HandshakeRefused(f"{socket_path} answered initialize with an error: {error}")
    result = answer.get("result")
    if not isinstance(result, dict):
        # Neither a result nor an error is not an answer. Taken as an empty
        # result it would report a peer that said `{}` — or said nothing this
        # request asked for — as a server that answered, which is the one thing
        # a status line must not do.
        raise _HandshakeRefused(
            f"{socket_path} answered initialize with neither a result nor an error"
        )
    return result


def _await_initialize(
    connection: socket.socket, socket_path: Path, budget: _Budget
) -> dict[str, Any]:
    """The answer to *this* request, told apart from anything else on the wire.

    Matched on the id rather than taken as the first message to arrive. The
    server may notify before it answers — the engine's own client exists partly
    to route such things — and a first-message read would take a notification,
    find no `result` in it, and report whatever it decided that meant. Skipped
    here rather than handled: a status read has nowhere to put a notification
    and no reason to want one.
    """
    for _ in range(MESSAGES_BEFORE_THE_ANSWER):
        message = _receive(connection, budget)
        if message.get("id") == INITIALIZE_ID and message.get("method") is None:
            return message
    raise _HandshakeRefused(
        f"{socket_path} sent {MESSAGES_BEFORE_THE_ANSWER} messages without answering initialize"
    )


def _initialize_params() -> dict[str, Any]:
    """What this client says about itself. It asks for no capability it will not use."""
    return {
        "clientInfo": {"name": CLIENT_NAME, "title": CLIENT_NAME, "version": __version__},
        "capabilities": {"experimentalApi": False},
    }


def _send(connection: socket.socket, message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    connection.sendall(websocket.client_frame(payload, websocket.OPCODE_TEXT))


def _receive(connection: socket.socket, budget: _Budget) -> dict[str, Any]:
    """One whole JSON-RPC message, read a frame at a time and blocking.

    What each opcode means, and how fragments become a message, is
    `websocket.Reassembly`'s — the same one the engine's client uses, so a
    protocol change is one edit rather than two that can drift (#47). What is
    here is the blocking read, because this runs before any event loop exists.
    """
    gathering = websocket.Reassembly(MAX_MESSAGE_BYTES)
    while True:
        step = gathering.take(*_read_frame(connection, budget))
        if step.refuse is not None:
            raise _HandshakeRefused(f"it {step.refuse}")
        if step.pong_with is not None:
            connection.sendall(step.pong_with)
        if step.message is not None:
            break
    try:
        message = json.loads(step.message)
    except (UnicodeDecodeError, json.JSONDecodeError):
        quoted = step.message[:UNREADABLE_ANSWER_QUOTED_CHARS].decode("utf-8", "replace")
        raise _HandshakeRefused(f"it answered initialize with unreadable JSON: {quoted}") from None
    if not isinstance(message, dict):
        raise _HandshakeRefused("it answered initialize with something that is not an object")
    return message


def _read_frame(connection: socket.socket, budget: _Budget) -> tuple[int, bool, bytes]:
    first, second = _read_exactly(connection, 2, budget)
    prefix = websocket.frame_prefix(first, second)
    if prefix.reserved:
        raise _HandshakeRefused("it set reserved WebSocket bits")
    length = prefix.length
    if prefix.extended_length_bytes:
        length = websocket.extended_length(
            _read_exactly(connection, prefix.extended_length_bytes, budget)
        )
    if length > MAX_MESSAGE_BYTES:
        raise _HandshakeRefused("it sent a frame larger than this read will hold")
    mask = _read_exactly(connection, websocket.MASK_BYTES, budget) if prefix.masked else b""
    payload = _read_exactly(connection, length, budget) if length else b""
    if prefix.masked:
        payload = websocket.unmask(payload, mask)
    return prefix.opcode, prefix.final, payload


def _read_exactly(connection: socket.socket, count: int, budget: _Budget) -> bytes:
    """Exactly this many bytes, or the connection ended before they arrived.

    Or the handshake's budget did: the count is a peer's own number, so a peer
    that announces a long frame and then dribbles it is bounded by the deadline
    and by nothing else.
    """
    collected = bytearray()
    while len(collected) < count:
        budget.arm(connection)
        chunk = connection.recv(count - len(collected))
        if not chunk:
            raise _HandshakeRefused("the shared app-server ended the connection mid-frame")
        collected.extend(chunk)
    return bytes(collected)


def _read_until(connection: socket.socket, terminator: bytes, budget: _Budget) -> bytes:
    """The upgrade's header block, bounded three ways: terminator, budget, size.

    Byte at a time, and that is affordable exactly here: this reads one HTTP
    header block on a local socket, once, and reading in chunks would need a
    buffer the frame reader after it would have to be handed.

    **All three bounds are needed, and the timeout alone was none of them**
    (#276). A socket timeout bounds a peer that sends *nothing*; a peer that
    sends one byte at a time and no terminator was bounded by neither the clock
    — each `recv` renewed its own timeout — nor by any count, because this loop
    had none. So the deadline is armed per byte and the block has a ceiling.
    """
    collected = bytearray()
    while not collected.endswith(terminator):
        if len(collected) >= MAX_UPGRADE_HEADER_BYTES:
            raise _HandshakeRefused(
                f"it sent more than {MAX_UPGRADE_HEADER_BYTES} bytes of WebSocket upgrade "
                f"answer without ending the header block"
            )
        budget.arm(connection)
        chunk = connection.recv(1)
        if not chunk:
            raise _HandshakeRefused("the connection ended during the WebSocket upgrade")
        collected.extend(chunk)
    return bytes(collected)
