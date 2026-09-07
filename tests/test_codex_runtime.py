"""The one codex on this machine, as three facts — #272, ADR 0022.

Every claim here was measured on #271's prototype before it was written down;
the flow document that recorded it is `docs/codex-runtime-flow.md`, and the
`ProgramArguments`, `PATH` and status assertions in
`tests/test_codex_launch_agent.py` are the other half of the same ticket.

**Nothing here reaches the machine's own codex or its own control socket.** The
resolver is handed a `PATH` the test composed, and the handshake is run against
a socket bound in this test's own directory — the rule `tests/conftest.py` holds
the whole suite to, applied to the module that gave that rule its sharpest
edge: the lookup is a `stat` now, not a subprocess, so nothing can intercept it
after the fact.
"""

from __future__ import annotations

import json
import socket as sockets
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from gpt_voicecoding import websocket
from gpt_voicecoding.installation import codex_runtime


def on_path(root: Path, *, name: str = "codex", executable: bool = True) -> Path:
    directory = root / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / name
    file.write_text("#!/bin/sh\n", encoding="utf-8")
    file.chmod(0o755 if executable else 0o644)
    return directory


class TestResolvingTheUsersCodex:
    """An ordinary `which` over the `PATH` this process was given — option (A)."""

    def test_the_codex_on_the_path_is_the_one(self, tmp_path: Path) -> None:
        directory = on_path(tmp_path)

        found = codex_runtime.resolve({"PATH": str(directory)}, tmp_path)

        assert found.runtime is not None
        assert found.runtime.executable == directory / "codex"
        assert found.reason == ""

    def test_the_first_codex_on_the_path_wins_as_the_shell_would_have_it(
        self, tmp_path: Path
    ) -> None:
        """`which`'s own rule, and the user's shell's: earlier entries shadow later.

        This is the whole reason the reconcile is handed the login `PATH` rather
        than launchd's — the ordering *is* the answer to "which codex do I get
        when I type codex", and on the reference machine two different ones were
        reachable (#271: `~/.local/bin` 0.149.1 ahead of the npm 0.153.4 under a
        non-interactive login shell).
        """
        first = on_path(tmp_path / "first")
        second = on_path(tmp_path / "second")

        found = codex_runtime.resolve({"PATH": f"{first}:{second}"}, tmp_path)

        assert found.runtime is not None
        assert found.runtime.executable == first / "codex"

    def test_no_codex_anywhere_is_a_reason_and_not_an_exception(self, tmp_path: Path) -> None:
        """#272's first edge case: the Codex lane reports itself absent, with the reason."""
        empty = tmp_path / "empty"
        empty.mkdir()

        found = codex_runtime.resolve({"PATH": str(empty)}, tmp_path)

        assert found.runtime is None
        assert not found.found
        assert "there is no codex" in found.reason
        assert str(empty) in found.reason, "the reason must say where it looked"

    def test_no_path_at_all_says_that_rather_than_looking_nowhere(self, tmp_path: Path) -> None:
        """The state a Finder-opened app would be in if the shell handed it nothing.

        Told apart from "there is a `PATH` and no codex on it", because the two
        send a person to different places: one to install codex, one to the
        `LoginShellPath` panel.
        """
        found = codex_runtime.resolve({}, tmp_path)

        assert found.runtime is None
        assert "no PATH" in found.reason

    @pytest.mark.parametrize("stated", ["", "   "])
    def test_a_blank_path_is_no_path(self, tmp_path: Path, stated: str) -> None:
        assert codex_runtime.resolve({"PATH": stated}, tmp_path).runtime is None

    def test_a_codex_that_cannot_be_run_is_no_codex(self, tmp_path: Path) -> None:
        """#272's second edge case, in the form that survives option (A).

        A shell function or alias is unreachable here by construction — `which`
        over a `PATH` only ever answers a file it found, never a name — so what
        is left to pin is a file that is there and is not executable.
        """
        directory = on_path(tmp_path, executable=False)

        found = codex_runtime.resolve({"PATH": str(directory)}, tmp_path)

        assert found.runtime is None
        assert "there is no codex" in found.reason

    def test_a_directory_named_codex_is_not_a_codex(self, tmp_path: Path) -> None:
        directory = tmp_path / "bin"
        (directory / "codex").mkdir(parents=True)

        assert codex_runtime.resolve({"PATH": str(directory)}, tmp_path).runtime is None

    def test_resolution_is_repeated_and_never_remembered(self, tmp_path: Path) -> None:
        """The `nvm use` case, and why no `config.toml` key records this.

        `LoginShellPath.swift`'s ruling applies unchanged: a copy of something
        the user's shell already states goes stale the day they edit their
        profile. So nothing is written down, and a codex that moved is simply
        resolved again — which is what "re-resolve rather than fail" is, with no
        staleness check to write.
        """
        directory = on_path(tmp_path)
        environ = {"PATH": str(directory)}
        assert codex_runtime.resolve(environ, tmp_path).runtime is not None

        (directory / "codex").unlink()

        assert codex_runtime.resolve(environ, tmp_path).runtime is None


class TestTheControlSocket:
    """Derived, and never asked of a running process."""

    def test_it_is_where_codex_itself_computes_it(self, tmp_path: Path) -> None:
        assert codex_runtime.control_socket(tmp_path) == (
            tmp_path / "app-server-control" / "app-server-control.sock"
        )

    def test_a_moved_codex_home_moves_the_socket_with_it(self, tmp_path: Path) -> None:
        moved = tmp_path / "elsewhere"

        found = codex_runtime.resolve(
            {"PATH": str(on_path(tmp_path)), "CODEX_HOME": str(moved)}, tmp_path
        )

        assert found.runtime is not None
        assert found.runtime.control_socket == (
            moved / "app-server-control" / "app-server-control.sock"
        )

    def test_the_home_comes_from_the_environment_when_it_is_stated(self, tmp_path: Path) -> None:
        assert codex_runtime.default_codex_home({"CODEX_HOME": str(tmp_path)}, tmp_path) == tmp_path

    @pytest.mark.parametrize("stated", ["", "   "])
    def test_a_blank_home_is_no_home(self, tmp_path: Path, stated: str) -> None:
        assert (
            codex_runtime.default_codex_home({"CODEX_HOME": stated}, tmp_path)
            == tmp_path / ".codex"
        )

    def test_the_server_the_job_starts_listens_on_exactly_that_path(self, tmp_path: Path) -> None:
        found = codex_runtime.resolve({"PATH": str(on_path(tmp_path))}, tmp_path)
        assert found.runtime is not None

        assert found.runtime.server_arguments == [
            "app-server",
            "--listen",
            f"unix://{found.runtime.control_socket}",
        ]


class TestTheLaunchEnvironment:
    """The third fact, which #271 did not anticipate and found by failing."""

    def test_the_path_is_the_one_this_process_was_given(self, tmp_path: Path) -> None:
        """#38: nothing rendered may be a constant standing in for the environment.

        launchd's own `/usr/bin:/bin:/usr/sbin:/sbin` written into a plist would
        be exactly that. What goes in is the `PATH` the reconcile ran on.
        """
        stated = f"{on_path(tmp_path)}:/usr/bin:/bin"

        found = codex_runtime.resolve({"PATH": stated}, tmp_path)

        assert found.runtime is not None
        assert found.runtime.launch_environment["PATH"] == stated

    def test_the_executables_own_directory_is_on_it(self, tmp_path: Path) -> None:
        """The finding: an npm codex is `codex.js` behind `#!/usr/bin/env node`.

        Under launchd's `PATH` there is no `node`, so the job died with `env:
        node: No such file or directory` and never held the socket. nvm and npm
        put `node` in the same directory as the shim, so the directory `which`
        found the executable in is the one that has to be reachable — and it is
        on this `PATH` by construction, because that is where it was found.
        """
        directory = on_path(tmp_path)

        found = codex_runtime.resolve({"PATH": f"/usr/bin:{directory}"}, tmp_path)

        assert found.runtime is not None
        carried = found.runtime.launch_environment["PATH"].split(":")
        assert str(found.runtime.executable.parent) in carried

    def test_the_codex_home_rides_along_even_when_it_is_the_default(self, tmp_path: Path) -> None:
        """launchd hands a job none of the user's shell environment.

        Without this, a user who moved their Codex home gets a server on one
        home and TUIs on another, and an empty roster nothing explains.
        """
        found = codex_runtime.resolve({"PATH": str(on_path(tmp_path))}, tmp_path)

        assert found.runtime is not None
        assert found.runtime.launch_environment["CODEX_HOME"] == str(tmp_path / ".codex")

    def test_nothing_else_is_put_in_the_jobs_environment(self, tmp_path: Path) -> None:
        """A login shell can set anything, and a job that inherited it would be a
        second, invisible configuration file — `LoginShellPath`'s own rule."""
        found = codex_runtime.resolve(
            {"PATH": str(on_path(tmp_path)), "OPENAI_API_KEY": "sk-not-this-jobs-business"},
            tmp_path,
        )

        assert found.runtime is not None
        assert sorted(found.runtime.launch_environment) == ["CODEX_HOME", "PATH"]


#: A socket path short enough to bind. Darwin caps an `AF_UNIX` path at 103
#: bytes and a pytest temporary directory is already longer than that, which is
#: why `tests/test_codex_wire.py` reaches for `/tmp` too.
@pytest.fixture
def socket_path() -> Iterator[Path]:
    path = Path("/tmp") / f"vc-runtime-{id(object())}.sock"
    yield path
    path.unlink(missing_ok=True)


class FakeAppServer:
    """One connection's worth of a codex app-server, on a real socket.

    The `daemon version` subprocess this replaces was stubbed with a callable;
    a handshake cannot be, so the stub is a socket that speaks the protocol —
    which is also what makes this a test of the protocol and not of a mock.
    Serves exactly one client and stops, because `answering` opens exactly one.
    """

    def __init__(
        self, path: Path, *, answer: Callable[[dict[str, Any]], Any] | None = None
    ) -> None:
        self._path = path
        self._answer = answer or (lambda _: {"userAgent": "gpt-voicecoding/0.153.4"})
        self._listener = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.upgraded = False
        self.asked: list[dict[str, Any]] = []

    def __enter__(self) -> FakeAppServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._listener.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        try:
            client, _ = self._listener.accept()
        except OSError:
            return
        with client:
            header = bytearray()
            while not header.endswith(websocket.UPGRADE_TERMINATOR):
                chunk = client.recv(1)
                if not chunk:
                    return
                header.extend(chunk)
            _, headers = websocket.upgrade_answer(bytes(header))
            key = headers.get("sec-websocket-key", "")
            client.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {websocket.accept_key(key)}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            self.upgraded = True
            while True:
                message = self._read(client)
                if message is None:
                    return
                self.asked.append(message)
                if message.get("id") is None:
                    continue
                answered = self._answer(message)
                if answered is None:
                    return
                self._write(client, {"id": message["id"], **answered})

    def _read(self, client: sockets.socket) -> dict[str, Any] | None:
        payload = bytearray()
        while True:
            head = self._exactly(client, 2)
            if head is None:
                return None
            prefix = websocket.frame_prefix(head[0], head[1])
            length = prefix.length
            if prefix.extended_length_bytes:
                extended = self._exactly(client, prefix.extended_length_bytes)
                if extended is None:
                    return None
                length = websocket.extended_length(extended)
            mask = self._exactly(client, websocket.MASK_BYTES) if prefix.masked else b""
            body = self._exactly(client, length) if length else b""
            if mask is None or body is None:
                return None
            payload.extend(websocket.unmask(body, mask) if prefix.masked else body)
            if prefix.opcode == websocket.OPCODE_CLOSE:
                return None
            if prefix.final:
                break
        return json.loads(bytes(payload))

    @staticmethod
    def _exactly(client: sockets.socket, count: int) -> bytes | None:
        collected = bytearray()
        while len(collected) < count:
            chunk = client.recv(count - len(collected))
            if not chunk:
                return None
            collected.extend(chunk)
        return bytes(collected)

    @staticmethod
    def _write(client: sockets.socket, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode("utf-8")
        # Unmasked: a server never masks, and a client that accepted a masked
        # frame from one would be accepting something no app-server sends.
        header = bytearray((0x80 | websocket.OPCODE_TEXT, len(payload)))
        client.sendall(bytes(header) + payload)


class TestWhatAStatusRunSaysAboutTheSharedServer:
    """What replaced `daemon version` — one connect-and-`initialize`, #272.

    Three outcomes, and they are the three states the socket can be in. The
    version is deliberately absent from every one of them: with one codex on the
    machine there is no second version for the first to disagree with, so #67's
    no-pin ruling is dissolved rather than reopened, and the running server's
    version is only available as a substring of `userAgent` anyway.
    """

    def test_a_server_that_completes_the_handshake_is_answering(self, socket_path: Path) -> None:
        with FakeAppServer(socket_path) as server:
            said = codex_runtime.answering(socket_path)

        assert "answered initialize" in said
        assert str(socket_path) in said
        assert server.upgraded
        assert [message["method"] for message in server.asked] == ["initialize", "initialized"]

    def test_it_says_no_version(self, socket_path: Path) -> None:
        with FakeAppServer(
            socket_path, answer=lambda _: {"result": {"userAgent": "gpt-voicecoding/0.153.4"}}
        ):
            said = codex_runtime.answering(socket_path)

        assert "0.153.4" not in said

    def test_it_names_the_codex_home_the_server_answered_with(self, socket_path: Path) -> None:
        """The one fact from `initialize` worth printing: *which* home this is.

        A user who moved `CODEX_HOME` and a job still on the old one is exactly
        the empty-roster-nothing-explains case, and this is where a person
        typing `status` would see it.
        """
        with FakeAppServer(
            socket_path, answer=lambda _: {"result": {"codexHome": "/somewhere/.codex"}}
        ):
            said = codex_runtime.answering(socket_path)

        assert "/somewhere/.codex" in said

    def test_it_says_what_it_calls_itself(self, socket_path: Path) -> None:
        """The far side records this, so a person reading the server's own logs
        can see which software held the connection."""
        with FakeAppServer(socket_path) as server:
            codex_runtime.answering(socket_path)

        client = server.asked[0]["params"]["clientInfo"]
        assert client["name"] == "gpt-voicecoding"
        assert client["version"]

    def test_no_socket_is_reported_rather_than_raised(self, tmp_path: Path) -> None:
        """The ordinary case: the server runs only once the user has logged in
        since the job was installed. A person typed `status` to find that out."""
        said = codex_runtime.answering(tmp_path / "nothing-here.sock")

        assert "is not answering" in said
        assert "there is no socket" in said

    def test_a_stale_socket_file_is_the_same_answer_with_a_different_reason(
        self, socket_path: Path
    ) -> None:
        """What a server that was killed rather than stopped leaves behind.

        Told apart from a missing socket because the two send a person to
        different places: one to log in, one to delete a file.
        """
        socket_path.touch()

        said = codex_runtime.answering(socket_path)

        assert "is not answering" in said
        assert "there is no socket" not in said
        assert str(socket_path) in said

    def test_a_listener_that_is_not_a_codex_app_server_is_refused(self, socket_path: Path) -> None:
        """Something is there and it does not speak this protocol.

        Reported, and not mistaken for a server: the whole point of doing the
        real `initialize` rather than a bare `connect` is that "answering" means
        a codex answered.
        """
        listener = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)

        def refuse() -> None:
            client, _ = listener.accept()
            with client:
                client.recv(4096)
                client.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

        thread = threading.Thread(target=refuse, daemon=True)
        thread.start()
        try:
            said = codex_runtime.answering(socket_path)
        finally:
            thread.join(timeout=2)
            listener.close()

        assert "is not answering" in said
        assert "not a codex app-server" in said

    def test_a_server_that_errors_the_handshake_is_not_answering(self, socket_path: Path) -> None:
        with FakeAppServer(
            socket_path, answer=lambda _: {"error": {"code": -32600, "message": "no"}}
        ):
            said = codex_runtime.answering(socket_path)

        assert "is not answering" in said
        assert "answered initialize with an error" in said

    def test_a_server_that_hangs_up_mid_handshake_is_not_answering(self, socket_path: Path) -> None:
        with FakeAppServer(socket_path, answer=lambda _: None):
            said = codex_runtime.answering(socket_path)

        assert "is not answering" in said
