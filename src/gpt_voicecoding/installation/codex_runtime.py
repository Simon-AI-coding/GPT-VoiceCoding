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
— and #272 makes it hand that same environment to the installation subprocess.
So resolution here is an ordinary ``which`` over the ``PATH`` this process was
given, and there is no second login-shell read and no second implementation of
the interactive-shell lesson to get wrong.

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

#: How long the status handshake gets, end to end. Measured at **1 ms** against
#: a live server on the reference machine (#271 step 7), against 139 ms for the
#: `daemon version` subprocess it replaces — so this is not a budget the
#: ordinary case spends. It is the bound on the case that has no ordinary one: a
#: socket something is listening on that never answers. A person typed `status`
#: and is waiting at a terminal for it.
HANDSHAKE_TIMEOUT_SECONDS: Final = 5.0

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


def resolve_executable(environ: Mapping[str, str]) -> Path | None:
    """The codex on this environment's ``PATH``, or nothing at all.

    Nothing at all is an answer and not a failure: a machine with no codex is a
    machine whose Codex lane reports itself absent, with the reason. This never
    raises and never invents a path.
    """
    stated = environ.get(PATH_VARIABLE)
    if not stated or not stated.strip():
        return None
    found = shutil.which(EXECUTABLE_NAME, path=stated)
    return Path(found) if found else None


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

        **The ``PATH`` is the one this process was given, not one composed
        here.** #38 forbids a rendered artifact naming a path that was true only
        on the machine that rendered it, and launchd's own
        ``/usr/bin:/bin:/usr/sbin:/sbin`` written into a plist would be exactly
        that — a constant standing in for something the environment already
        states. The directory holding the executable is on this ``PATH`` by
        construction, because that is where ``which`` found it, and nvm and npm
        put ``node`` in that same directory beside the shim. A Homebrew or
        standalone codex is a native binary and needs none of this; the entry is
        harmless there, so there is no branch on install kind.
        """
        return {CODEX_HOME_VARIABLE: str(self.codex_home), PATH_VARIABLE: self.path}


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

    @property
    def found(self) -> bool:
        return self.runtime is not None


def resolve(environ: Mapping[str, str], home: Path | None = None) -> Resolution:
    """This machine's Codex Runtime, or the reason there is none.

    Never raises. This runs on the reconcile that precedes every engine start,
    from a shell with nowhere to put a traceback.
    """
    codex_home = default_codex_home(environ, home)
    executable = resolve_executable(environ)
    if executable is None:
        stated = (environ.get(PATH_VARIABLE) or "").strip()
        where = f"on PATH ({stated})" if stated else "and this process was given no PATH"
        return Resolution(None, f"there is no {EXECUTABLE_NAME} {where}")
    return Resolution(
        CodexRuntime(
            executable=executable,
            codex_home=codex_home,
            control_socket=control_socket(codex_home),
            path=(environ.get(PATH_VARIABLE) or "").strip(),
        ),
        "",
    )


def answering(socket_path: Path) -> str:
    """One sentence about the shared app-server, for a status run to print.

    **Never called on the install path.** A reconcile runs before the engine at
    every launch and has no business dialling anything; a person typing `status`
    is asking exactly this question.

    Three answers, and they are the three states the socket can be in: it is not
    there, it is there with nothing behind it, or something behind it completed
    a codex `initialize`. The middle one is a stale file, which a server that
    was killed rather than stopped leaves behind, and it is worth telling apart
    from the first — the same path with two different reasons.
    """
    started = time.monotonic()
    if not socket_path.exists():
        return f"the shared app-server is not answering: there is no socket at {socket_path}"
    try:
        answer = _handshake(socket_path)
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


def _handshake(socket_path: Path) -> dict[str, Any]:
    """Connect, upgrade, `initialize`, and give back what the server said.

    Blocking, and on a socket of its own, because this runs before any event
    loop exists — `bridge-install` is a console script the shell runs, not a
    part of the engine. The protocol it speaks is the engine's, from the one
    place that holds it (`gpt_voicecoding.websocket`); what is written here is
    only how the bytes get on and off this socket.

    `initialized` is sent after the answer because the protocol asks for it and
    a server told nothing would be entitled to wait. Nothing is read after it:
    this connection exists to prove the server is there, and it closes.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        connection.connect(str(socket_path))
        request, key = websocket.upgrade_request()
        connection.sendall(request)
        header = _read_until(connection, websocket.UPGRADE_TERMINATOR)
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
        answer = _await_initialize(connection, socket_path)
        _send(connection, {"method": "initialized", "params": {}})
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


def _await_initialize(connection: socket.socket, socket_path: Path) -> dict[str, Any]:
    """The answer to *this* request, told apart from anything else on the wire.

    Matched on the id rather than taken as the first message to arrive. The
    server may notify before it answers — the engine's own client exists partly
    to route such things — and a first-message read would take a notification,
    find no `result` in it, and report whatever it decided that meant. Skipped
    here rather than handled: a status read has nowhere to put a notification
    and no reason to want one.
    """
    for _ in range(MESSAGES_BEFORE_THE_ANSWER):
        message = _receive(connection)
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


def _receive(connection: socket.socket) -> dict[str, Any]:
    """One whole JSON-RPC message, read a frame at a time and blocking.

    What each opcode means, and how fragments become a message, is
    `websocket.Reassembly`'s — the same one the engine's client uses, so a
    protocol change is one edit rather than two that can drift (#47). What is
    here is the blocking read, because this runs before any event loop exists.
    """
    gathering = websocket.Reassembly(MAX_MESSAGE_BYTES)
    while True:
        step = gathering.take(*_read_frame(connection))
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


def _read_frame(connection: socket.socket) -> tuple[int, bool, bytes]:
    first, second = _read_exactly(connection, 2)
    prefix = websocket.frame_prefix(first, second)
    if prefix.reserved:
        raise _HandshakeRefused("it set reserved WebSocket bits")
    length = prefix.length
    if prefix.extended_length_bytes:
        length = websocket.extended_length(_read_exactly(connection, prefix.extended_length_bytes))
    if length > MAX_MESSAGE_BYTES:
        raise _HandshakeRefused("it sent a frame larger than this read will hold")
    mask = _read_exactly(connection, websocket.MASK_BYTES) if prefix.masked else b""
    payload = _read_exactly(connection, length) if length else b""
    if prefix.masked:
        payload = websocket.unmask(payload, mask)
    return prefix.opcode, prefix.final, payload


def _read_exactly(connection: socket.socket, count: int) -> bytes:
    """Exactly this many bytes, or the connection ended before they arrived."""
    collected = bytearray()
    while len(collected) < count:
        chunk = connection.recv(count - len(collected))
        if not chunk:
            raise _HandshakeRefused("the shared app-server ended the connection mid-frame")
        collected.extend(chunk)
    return bytes(collected)


def _read_until(connection: socket.socket, terminator: bytes) -> bytes:
    """The upgrade's header block, bounded by the terminator and by the timeout.

    Byte at a time, and that is affordable exactly here: this reads one HTTP
    header block on a local socket, once, and reading in chunks would need a
    buffer the frame reader after it would have to be handed. The socket's own
    timeout bounds a peer that never sends the terminator.
    """
    collected = bytearray()
    while not collected.endswith(terminator):
        chunk = connection.recv(1)
        if not chunk:
            raise _HandshakeRefused("the connection ended during the WebSocket upgrade")
        collected.extend(chunk)
    return bytes(collected)
