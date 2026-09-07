"""This engine's client of the shared Codex app-server. Join-only.

**Why this exists at all.** #82 proved that the shared server is the only source
that knows a thread's id, its name and what it has been doing, and #83 installed
the login job that starts it — but nothing in this engine ever dialled it, so
every Codex row came off the process table and no thread could be read. Progress
on the Codex lane was `None` by construction (#76, advisor ruling Q1). This is
the dial, and only the dial.

**Join-only, and that is a rule rather than a scope note.** This product starts
a server the user's Sessions will join and never stops one they are attached to
(#83, ADR 0012): by the time this engine shuts down, the user's `codex` TUIs are
thin clients of that server, and closing it would end their Sessions. So nothing
here spawns, bootstraps or boots out anything — it opens a connection, keeps it,
and lets go of its own end.

**Where the socket is, is derived — #272, ADR 0022.** Until then this ran `codex
app-server daemon version` and read a `socketPath` out of its answer, on the
reasoning that a running server is the one address that cannot go stale. That
reasoning did not survive being measured: the path is
`$CODEX_HOME/app-server-control/app-server-control.sock`, upstream computes it
that way with no variable and no flag over it, and asking cost a 139 ms
subprocess plus a dependency on the `daemon` subcommands this product no longer
has. So the address comes from `installation/codex_runtime.py`, which is where
the three Codex Runtime facts live and the one place that spells this path. That
is the single `adapters -> installation` direction ADR 0012 allows; installation
never imports back into the engine.

**No version pin, and now no version to pin.** #67's ruling was that a CLI and a
server whose versions disagree still get dialled, with the disagreement riding
out as a note on `LaneDiscovery.degraded`. #272 does not reopen that ruling, it
**dissolves** it: with one codex on the machine there is no pair of versions to
disagree, so there is nothing to report and nothing to compare. Everything else
`degraded` carries — a socket that is not there, one nothing is listening on,
one that refused the connection — stays, because those are still facts about
this engine's reach and the user still deserves to see them.

**Locating is re-tried, not remembered.** The address is looked up only when
there is no live connection, so a healthy engine does no work per tick; an
engine whose server is down probes once per discovery instead, which is the only
honest way to notice it came back.

**Against legacy** (ADR 0010, `CLAUDE.md`): **dropped, because gen 1 had no
shared server to join.** It drove a launched, wrapped, per-Session app-server it
owned — `legacy@1d32845:bridge/codex.py:1319-1347` opened a client per read
against a socket its own runtime had spawned — and #82 recorded that whole route
as dropped from porting. What survives from it is the *shape* of the read side:
locate, connect, ask, and never fall back to another source when this one cannot
answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, attach
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.codex_app_server.wire import AppServerConnection, WireError
from gpt_voicecoding.installation import codex_runtime

_log = logging.getLogger(__name__)


def default_control_socket() -> Path:
    """Where the shared app-server is looked for when nobody says.

    Read from the real environment when it is *called* rather than captured at
    import: `CODEX_HOME` is the user's to move, and a module-level copy would
    pin whatever it happened to be when this module was first imported. It is
    also what `tests/conftest.py` compares against to keep the whole suite off
    the machine's own control socket.
    """
    return codex_runtime.control_socket(codex_runtime.default_codex_home(os.environ))


@dataclass(frozen=True, slots=True)
class DaemonAddress:
    """Where the shared app-server is.

    One field, and it used to have three. `cli_version` and `app_server_version`
    came out of `daemon version`'s answer and existed to be compared with each
    other; #272 left them with no source and nothing to say. The comparison went
    with them — see the module note on why that dissolves #67 rather than
    reopening it.
    """

    socket_path: Path


def locate(control_socket: Path) -> tuple[DaemonAddress | None, str]:
    """Where the shared app-server is, or the reason nothing could be found.

    Never raises, and spawns nothing. This runs inside a five-second discovery
    tick, and a lane that threw because a path was unreadable would take the
    roster down with it — the honest answer is a reason the rows can carry
    (`LaneDiscovery.degraded`, #74).

    **A socket that is not there and a socket nothing is listening on are told
    apart, and the second one is not detected here.** A stale socket file is
    what a server that was killed rather than stopped leaves behind, and the
    only thing that proves nobody is behind it is trying to connect — so that
    reason comes from the dial in `client`, with this path named in it, rather
    than from a guess made here. What this settles is the one question it can
    answer without touching anything: is there a socket at all.

    Synchronous, because everything it does is one `stat`. It was a coroutine
    while it shelled out to `codex`; keeping the `async` after the subprocess
    went would be an await that never yields, dressed as one that might.
    """
    if not control_socket.exists():
        return None, (
            f"the shared Codex app-server is not answering: there is no socket at {control_socket}"
        )
    return DaemonAddress(socket_path=control_socket), ""


class SharedDaemon:
    """One connection to the app-server somebody else owns, held while it lives."""

    def __init__(
        self,
        *,
        settings: CodexSettings,
        version: str,
        control_socket: Path | None = None,
        locate: Callable[[Path], tuple[DaemonAddress | None, str]] = locate,
        attach: Callable[..., Awaitable[Any]] = attach,
    ) -> None:
        self._settings = settings
        self._version = version
        #: Resolved once per instance rather than per dial: an engine that moved
        #: `CODEX_HOME` under itself would be a different machine, and a dial
        #: that quietly followed it would make "where is this lane reading from"
        #: unanswerable at any moment a person could ask.
        self._control_socket = control_socket or default_control_socket()
        self._locate = locate
        self._attach = attach
        self._connection: AppServerConnection | None = None
        self._note = ""
        #: Where the last accepted dial landed. A caller that has to name the
        #: wire one thread rides on asks here rather than locating again.
        self._socket_path: Path | None = None
        #: Where this connection's inbound traffic goes. **The server's
        #: notifications and its permission requests are what make Relay and
        #: Approval possible on this lane at all** (#77), and this is the one
        #: connection to it — so they are routed through here rather than by a
        #: second dial the server would have to hold and nobody would close.
        self._on_notification: Callable[[Any], None] | None = None
        self._on_server_request: Callable[[Any], Any] | None = None
        self._on_closed: Callable[[str], None] | None = None
        #: Held across the whole dial, because the dial is where the race is.
        #: The engine has two callers that arrive independently — the five-second
        #: discovery cadence and a control-plane `progress` ask — and the check
        #: for a live connection is separated from writing the new one by two
        #: awaits, which is room enough for both to find none and both to attach.
        self._dialling = asyncio.Lock()
        #: Bumped by every `aclose`. A dial that finishes on an older number is
        #: one the engine has already said goodbye to, so it closes what it made
        #: rather than writing it back. This is what lets `aclose` skip the lock
        #: instead of waiting behind a dial — see its docstring for why it must.
        self._generation = 0

    def route_to(
        self,
        *,
        notifications: Callable[[Any], None] | None = None,
        requests: Callable[[Any], Any] | None = None,
        closed: Callable[[str], None] | None = None,
    ) -> None:
        """Say where this connection's inbound traffic goes, before it is dialled.

        **Set rather than taken in the constructor**, so that injecting a
        `SharedDaemon` and wiring its traffic stay separable: a test that only
        wants to watch the dial does not have to supply three handlers, and the
        adapter wires the daemon it was handed exactly as it wires the one it
        made. It is called once, from the adapter's constructor, before anything
        has asked for a client — so "takes effect on the next dial" is every
        dial there will be.
        """
        self._on_notification = notifications
        self._on_server_request = requests
        self._on_closed = closed

    @property
    def socket_path(self) -> Path | None:
        """Where the last accepted dial landed, or `None` if none has been."""
        return self._socket_path

    @property
    def note(self) -> str:
        """What the lane should say about rows read through this, if anything.

        Empty when there is nothing to say, and after #272 a joined server has
        nothing to say: the one caveat that rode a live connection was a version
        disagreement, and with one codex on the machine there is no second
        version to disagree with. What remains are the reasons there is *no*
        connection — no socket, nothing listening, a refused dial — which is a
        caveat with nothing behind it rather than beside it.
        """
        return self._note

    async def client(self) -> AppServerConnection | None:
        """A live connection to the daemon, or `None` with `note` saying why not.

        A connection the far side dropped is not reused: the shared server can
        be restarted under a running engine — a login, or the user upgrading
        their codex, does exactly that — and an engine that kept a dead handle
        would report an empty roster for the rest of its life.

        **One dial at a time, and the answer is asked for twice.** A live
        connection is answered without taking the lock, which is the ordinary
        case and stays free; a caller that finds none waits, and then asks again
        — because what it was waiting on is very likely the dial that answers its
        own question. Without this, two callers arriving together both attached,
        the daemon held two clients of an engine that is meant to be one of them,
        and the loser was dropped with nothing left to close it.
        """
        held = self._connection
        if held is not None and held.is_open:
            return held

        async with self._dialling:
            held = self._connection
            if held is not None and held.is_open:
                return held
            self._connection = None
            began = self._generation

            address, reason = self._locate(self._control_socket)
            if address is None:
                self._note = reason
                return None
            try:
                connection = await self._attach(
                    address.socket_path,
                    version=self._version,
                    settings=self._settings,
                    on_notification=self._on_notification,
                    on_server_request=self._on_server_request,
                    on_closed=self._on_closed,
                )
            except (WireError, AppServerError, OSError) as unreachable:
                self._note = (
                    f"the shared Codex app-server at {address.socket_path} did not "
                    f"accept a connection: {unreachable}"
                )
                return None
            if self._generation != began:
                # Let go of while this was in flight. A dial does not get to
                # resurrect a connection the engine has already said goodbye to,
                # and what it made is closed here rather than left for nobody.
                await connection.aclose()
                return None
            _log.info("joined the shared Codex app-server at %s", address.socket_path)
            self._connection = connection
            self._socket_path = address.socket_path
            self._note = ""
            return connection

    async def aclose(self) -> None:
        """Let go of this engine's end. The server and its Sessions carry on.

        **It never waits for a dial in flight, and that is #96's arithmetic
        rather than a preference.** `runner.SHUTDOWN_SECONDS` is sixteen seconds
        because the phases it bounds sum to 13.2 and it must exceed them, and a
        test computes that sum from the constants themselves. A shutdown phase
        that could sit out a ten-second lookup is not one that sum has
        room for — and an overrun does not buy a tidier stop, it buys a SIGKILL
        with nothing written down, which is the failure #96 exists to end.

        So the dial is **invalidated instead of waited on**: the generation
        moves, and a dial that finishes afterwards closes what it made rather
        than writing it back. Clearing the field alone would have left exactly
        that client behind, with nothing holding it and nothing to close it.
        """
        self._generation += 1
        connection, self._connection = self._connection, None
        self._note = ""
        self._socket_path = None
        if connection is not None:
            await connection.aclose()
