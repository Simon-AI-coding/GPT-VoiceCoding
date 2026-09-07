"""Joining the shared Codex app-server, and admitting when it cannot be.

#82 established that the shared server is the only route to a thread's own
truth, and #83 installed the login job that starts it. Nothing in this engine
ever dialled it: `CodexAgentAdapter._shared_daemon()` returned `None`
unconditionally, so every Codex row came from the process table and no thread
could be read at all. This is the join (advisor ruling on #76, Q1).

Join-only, and the tests say so: nothing here starts a server, stops one, or
outlives one. By the time this engine shuts down the user's `codex` TUIs are
thin clients of that server, and a product that stopped it would end their
Sessions (ADR 0012, #83's written rule).

**#272 took the subprocess out of the lookup**, so the tests that bounded one
are gone with it: there is no `codex app-server daemon version` to hang, to
answer unreadable JSON, or to name a socket this side can derive. What replaced
them is the pair of questions a derived path can actually be wrong about —
there is no socket, or there is one and nothing is behind it.
"""

from __future__ import annotations

import asyncio
import os
import socket as sockets
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.codex import shared_daemon
from gpt_voicecoding.adapters.agent.codex.shared_daemon import (
    DaemonAddress,
    SharedDaemon,
    locate,
)
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.installation import codex_runtime


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    """A control socket that is *there*. Not bound: `locate` only asks whether
    the path exists, and what is behind it is the dial's question."""
    path = tmp_path / "app-server-control.sock"
    path.touch()
    return path


class TestFindingIt:
    """One derivation, and the two ways a derived path can be wrong."""

    def test_the_socket_is_derived_from_the_codex_home(self, tmp_path: Path) -> None:
        """Where `SharedDaemon` looks when nobody tells it — #272, ADR 0022.

        Until then this ran `codex app-server daemon version` and read a
        `socketPath` out of the answer, on the reasoning that a running server
        is the one address that cannot go stale. Upstream fixes the path, so it
        can be worked out instead, and the subprocess is what went.
        """
        home = tmp_path / "moved-codex-home"
        home.mkdir()
        looked: list[Path] = []

        def record(control_socket: Path) -> tuple[None, str]:
            looked.append(control_socket)
            return None, "no server"

        daemon = SharedDaemon(
            settings=CodexSettings(executable="codex"),
            version="test",
            control_socket=codex_runtime.control_socket(home),
            locate=record,
        )

        asyncio.run(daemon.client())

        assert looked == [home / "app-server-control" / "app-server-control.sock"]

    def test_the_default_is_the_one_installation_renders_into_the_job(
        self, socket_path: Path
    ) -> None:
        """One spelling of this path on the machine, and #47 is why.

        The LaunchAgent renders `--listen unix://<this>` and the engine dials
        it. Two derivations would be two constants with nothing holding them
        together — so both come from `codex_runtime`, and this pins that they do.
        """
        assert shared_daemon.default_control_socket() == codex_runtime.control_socket(
            codex_runtime.default_codex_home(os.environ)
        )

    def test_a_socket_that_is_there_is_the_address(self, socket_path: Path) -> None:
        address, reason = locate(socket_path)

        assert address == DaemonAddress(socket_path=socket_path)
        assert reason == ""

    def test_no_socket_is_a_reason_and_not_an_exception(self, tmp_path: Path) -> None:
        """The lane keeps working from the process table; it just says why (#74)."""
        missing = tmp_path / "nothing-here.sock"

        address, reason = locate(missing)

        assert address is None
        assert "not answering" in reason
        assert str(missing) in reason, "the reason must name the path it looked at"

    def test_the_lookup_never_raises_whatever_the_path_is(self, tmp_path: Path) -> None:
        """This runs inside a five-second discovery tick, and a lane that threw
        would take the roster down with it — the Claude lane's too, because the
        cadence is one loop over the adapters."""
        for path in (tmp_path / "a" / "b" / "c.sock", Path("/"), tmp_path):
            address, reason = locate(path)
            assert (address is None) == (not path.exists())
            assert address is not None or reason


class TestWhatAJoinedServerHasToSayAboutItself:
    """Nothing — and #272 dissolves #67 rather than reopening it.

    The one caveat a live connection used to carry was a version disagreement
    between the CLI and the running app-server. With one codex on the machine
    there is no pair of versions to disagree, so `DaemonAddress` has no version
    fields and a joined server contributes no `degraded` note. What `degraded`
    still carries is every reason there is *no* connection.
    """

    def test_an_address_is_a_path_and_nothing_else(self, socket_path: Path) -> None:
        assert [field for field in DaemonAddress.__dataclass_fields__] == ["socket_path"]

    def test_a_joined_server_leaves_the_lane_with_nothing_to_caveat(
        self, socket_path: Path
    ) -> None:
        daemon = self.joined(socket_path)

        assert asyncio.run(daemon.client()) is not None
        assert daemon.note == ""

    @staticmethod
    def joined(socket_path: Path) -> SharedDaemon:
        async def attach(path: Path, **_: object) -> object:
            class _Connection:
                is_open = True

                async def aclose(self) -> None:
                    pass

            return _Connection()

        return SharedDaemon(
            settings=CodexSettings(executable="codex"),
            version="test",
            control_socket=socket_path,
            attach=attach,
        )


class TestTheConnection:
    """Attached once, kept, and re-attached when the far side goes away."""

    def daemon(
        self,
        attached: list[Path],
        socket_path: Path,
        *,
        fails: Exception | None = None,
        slow: bool = False,
        made: list[object] | None = None,
        order: list[str] | None = None,
    ) -> SharedDaemon:
        class _Connection:
            def __init__(self) -> None:
                self.is_open = True
                self.closed = 0

            async def aclose(self) -> None:
                self.is_open = False
                self.closed += 1

        async def attach(path: Path, **_: object) -> object:
            attached.append(path)
            if slow:
                # A real dial does I/O, so it yields. A fake that returns
                # without ever awaiting hands the loop back to nobody, and a
                # race between two callers cannot happen in a test that never
                # lets the second one start.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            if fails is not None:
                raise fails
            if order is not None:
                order.append("dial finished")
            one = _Connection()
            if made is not None:
                made.append(one)
            return one

        return SharedDaemon(
            settings=CodexSettings(executable="codex"),
            version="test",
            control_socket=socket_path,
            attach=attach,
        )

    def test_one_connection_serves_every_tick(self, socket_path: Path) -> None:
        """Dialling per tick would open a client per five seconds, forever."""
        attached: list[Path] = []
        daemon = self.daemon(attached, socket_path)

        first = asyncio.run(daemon.client())
        second = asyncio.run(daemon.client())

        assert first is second
        assert attached == [socket_path]

    def test_a_connection_that_went_away_is_dialled_again(self, socket_path: Path) -> None:
        """The daemon can be restarted under us; the next tick must find it."""
        attached: list[Path] = []
        daemon = self.daemon(attached, socket_path)

        first = asyncio.run(daemon.client())
        first.is_open = False  # type: ignore[union-attr]
        second = asyncio.run(daemon.client())

        assert second is not first
        assert attached == [socket_path, socket_path]

    def test_a_daemon_that_refuses_the_dial_leaves_the_lane_working(
        self, socket_path: Path
    ) -> None:
        """`None` plus a note: the rows come from the process table, as before."""
        attached: list[Path] = []
        daemon = self.daemon(attached, socket_path, fails=OSError("connection refused"))

        assert asyncio.run(daemon.client()) is None
        assert "connection refused" in daemon.note

    def test_a_server_that_cannot_be_found_says_so_without_dialling(self, tmp_path: Path) -> None:
        attached: list[Path] = []
        daemon = SharedDaemon(
            settings=CodexSettings(executable="codex"),
            version="test",
            control_socket=tmp_path / "nothing-here.sock",
            attach=lambda path, **_: attached.append(path),  # type: ignore[misc,return-value]
        )

        assert asyncio.run(daemon.client()) is None
        assert attached == []
        assert "not answering" in daemon.note

    def test_two_callers_at_once_leave_this_engine_holding_one_client(
        self, socket_path: Path
    ) -> None:
        """The cadence and a `progress` ask really do arrive together.

        `client()` read the connection, awaited the dial, and only then wrote
        what it had made — so two callers that arrived while there was none both
        saw none and both attached. The daemon then held two clients of an engine
        that is supposed to be one of its clients, and the loser was dropped on
        the floor with nothing left to close it.
        """
        attached: list[Path] = []
        daemon = self.daemon(attached, socket_path, slow=True)

        first, second = asyncio.run(_both(daemon))

        assert first is second
        assert attached == [socket_path]

    def test_letting_go_while_a_dial_is_in_flight_leaves_nothing_attached(
        self, socket_path: Path
    ) -> None:
        """Shutdown invalidates the dial rather than waiting for it.

        Waiting would be the obvious answer and it is the wrong one: #96 derives
        `runner.SHUTDOWN_SECONDS` from the phases it bounds, and a phase that
        could sit out a ten-second daemon lookup is not one that sum has room
        for. So `aclose` returns at once and the dial finds its generation stale
        — and closes what it made, because clearing the field alone would leave
        a client nothing holds and nothing closes.
        """
        attached: list[Path] = []
        made: list[object] = []
        daemon = self.daemon(attached, socket_path, slow=True, made=made)

        async def dial_and_let_go() -> object:
            dialling = asyncio.ensure_future(daemon.client())
            await asyncio.sleep(0)
            await daemon.aclose()
            return await dialling

        answered = asyncio.run(dial_and_let_go())

        assert answered is None
        assert daemon._connection is None  # noqa: SLF001 - the field is the leak
        assert [one.closed for one in made] == [1]  # type: ignore[attr-defined]

    def test_letting_go_does_not_wait_behind_a_dial(self, socket_path: Path) -> None:
        """The budget, asserted rather than described (#96, `runner.py:95-117`).

        A `aclose` that took the dial's lock would finish only after the dial
        did. Held here as an ordering fact, so a later change that reaches for
        the obvious lock fails rather than quietly spending the shutdown budget.
        """
        attached: list[Path] = []
        order: list[str] = []
        daemon = self.daemon(attached, socket_path, slow=True, order=order)

        async def dial_and_let_go() -> None:
            dialling = asyncio.ensure_future(daemon.client())
            await asyncio.sleep(0)
            await daemon.aclose()
            order.append("let go")
            await dialling

        asyncio.run(dial_and_let_go())

        assert order.index("let go") < order.index("dial finished")

    def test_letting_go_closes_this_engine_s_client_and_nothing_else(
        self, socket_path: Path
    ) -> None:
        """The daemon lives on: the user's TUIs are attached to it (#83's rule)."""
        attached: list[Path] = []
        daemon = self.daemon(attached, socket_path)
        connection = asyncio.run(daemon.client())

        asyncio.run(daemon.aclose())

        assert connection.closed == 1  # type: ignore[union-attr]
        assert asyncio.run(daemon.client()) is not connection


class TestItNeverOwnsTheServer:
    """ADR 0012 and #83: this product starts no app-server and stops none it joined.

    Asserted on what the module *does*, not on what its source says: a test that
    scanned the text would trip over the paragraph explaining the rule, and
    would still pass for a module that spelled the forbidden verb differently.
    """

    def test_finding_the_server_runs_no_process_at_all(
        self, socket_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The strongest form this claim has ever had — #272.

        It used to be "the only `daemon` subcommand asked for is the one that
        reports", which left a lifecycle verb one edit away. Now there is no
        subprocess in the lookup to make one out of: `locate` is a `stat`.
        """

        async def refuse(*_: object, **__: object) -> object:
            raise AssertionError("finding the shared app-server started a process")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)

        address, reason = locate(socket_path)

        assert address is not None and reason == ""

    def test_a_stale_socket_is_a_reason_from_the_dial_and_never_an_exception(
        self, socket_path: Path
    ) -> None:
        """The second of #272's two socket cases: the file is there, nobody is.

        A server that was killed rather than stopped leaves exactly this behind.
        `locate` cannot tell it from a live one — only connecting can — so the
        reason comes from the dial, and it names the path so the two answers
        read as the same place for different reasons.
        """
        daemon = SharedDaemon(
            settings=CodexSettings(executable="codex"),
            version="test",
            control_socket=socket_path,
        )

        assert asyncio.run(daemon.client()) is None
        assert str(socket_path) in daemon.note
        assert "did not accept a connection" in daemon.note

    def test_a_live_socket_nothing_is_listening_on_reads_the_same_way(self) -> None:
        """A real `AF_UNIX` path with no listener, so the refusal is the OS's own.

        Under `/tmp` rather than `tmp_path`, for the reason the wire suite is:
        Darwin caps an `AF_UNIX` path at 103 bytes and a pytest temporary
        directory is already longer than that.
        """
        path = Path("/tmp") / f"vc-unlistened-{os.getpid()}.sock"
        path.unlink(missing_ok=True)
        try:
            with sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM) as bound:
                bound.bind(str(path))
                daemon = SharedDaemon(
                    settings=CodexSettings(executable="codex"),
                    version="test",
                    control_socket=path,
                )

                assert asyncio.run(daemon.client()) is None
                assert str(path) in daemon.note
        finally:
            path.unlink(missing_ok=True)


async def _both(daemon: SharedDaemon) -> tuple[object, object]:
    """Two asks for the client, started before either has answered."""
    first, second = await asyncio.gather(daemon.client(), daemon.client())
    return first, second
