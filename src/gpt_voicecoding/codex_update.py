"""App-owned Codex upgrade checks; installation never imports an agent adapter.

The shell calls ``check`` at launch and on installation changes. This module
owns the decision and reports the result; it never submits or resumes a turn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import plistlib
import re
import signal
import socket
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.agent.codex import discovery
from gpt_voicecoding.adapters.agent.codex.processes import enumerate_runs
from gpt_voicecoding.adapters.codex_app_server.process import initialise
from gpt_voicecoding.adapters.codex_app_server.wire import AppServerConnection, WireError
from gpt_voicecoding.installation import codex_launch_agent as agent
from gpt_voicecoding.installation import codex_runtime, read_intent
from gpt_voicecoding.installation.codex_runtime import (
    HANDSHAKE_TIMEOUT_SECONDS,
    CodexRuntime,
    Resolution,
)
from gpt_voicecoding.locations import codex_daemon_log_path, installation_path


@dataclass(frozen=True)
class UpdateResult:
    status: str
    failure: str = ""
    attempted: str = ""
    watch_paths: tuple[str, ...] = ()


class CodexUpdate:
    def __init__(
        self,
        *,
        runtime: CodexRuntime,
        launchd: agent.Launchd | None = None,
        directory: Path | None = None,
        record_path: Path | None = None,
        log_path: Path | None = None,
        list_sessions=enumerate_runs,
        process_identity=None,
        attempted: str = "",
        recovery_timeout: float = agent.SHELL_RECONCILE_DEADLINE_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.launchd = launchd or agent.default_launchd()
        self.directory = directory or agent.default_launch_agents_directory()
        self.record_path = record_path or installation_path()
        self.log_path = log_path or codex_daemon_log_path()
        self.list_sessions = list_sessions
        self.process_identity = process_identity or _process_identity
        self.recovery_timeout = recovery_timeout
        self._lock = asyncio.Lock()
        self.attempted = attempted

    async def check(self) -> UpdateResult:
        async with self._lock:
            paths = _watch_paths(self.runtime)
            try:
                async with asyncio.timeout(3 * agent.SHELL_RECONCILE_DEADLINE_SECONDS):
                    result = await self._check()
            except (OSError, ValueError, TimeoutError, WireError) as error:
                result = UpdateResult(
                    "failed", f"Codex update failed: {error or 'operation timed out'}"
                )
            return replace(result, attempted=self.attempted, watch_paths=paths)

    async def _check(self) -> UpdateResult:
        try:
            candidate = await _version(self.runtime)
        except (OSError, ValueError, TimeoutError) as error:
            return UpdateResult("failed", f"Codex installation is not ready: {error}")
        client = AppServerConnection(self.runtime.control_socket)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_SECONDS):
                await client.connect()
                answer = await initialise(client, experimental=False, version=__version__)
            running = _server_version(answer)
            if running == candidate:
                return UpdateResult("unchanged")
            fingerprint = _fingerprint(self.runtime, candidate)
            if self.attempted == fingerprint:
                return UpdateResult("unchanged")
            try:
                await _probe(self.runtime, candidate)
            except (OSError, ValueError, TimeoutError, WireError) as error:
                return UpdateResult("failed", f"Codex candidate is not ready: {error}")
            held = await self._owned_job()
            before = await self._sessions(client)
            # Validation may have overlapped another package replacement.
            if (
                await _version(self.runtime) != candidate
                or _fingerprint(self.runtime, candidate) != fingerprint
            ):
                raise ValueError("Codex installation changed during validation")
            self.attempted = fingerprint
            try:
                await self._switch(held, self.runtime)
                await client.aclose()
                await self._recovered(candidate, before)
                return UpdateResult("updated")
            except (OSError, ValueError, TimeoutError, WireError) as error:
                recovery = await self._restore(held, running, before)
                return UpdateResult(
                    "failed", f"Codex update failed: {error or 'recovery timed out'}. {recovery}"
                )
        except (OSError, ValueError, TimeoutError, WireError) as error:
            return UpdateResult("failed", f"Codex update failed: {error}")
        finally:
            await client.aclose()

    async def _restore(self, previous: agent.HeldJob, version: str, before) -> str:
        client = AppServerConnection(self.runtime.control_socket)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_SECONDS):
                await client.connect()
                await initialise(client, experimental=False, version=__version__)
            return "The server responds, but Session recovery was not confirmed."
        except (OSError, WireError, TimeoutError):
            pass
        finally:
            await client.aclose()
        old = replace(self.runtime, executable=Path(previous.program))
        try:
            if await _version(old) != version:
                return "The old executable is no longer available; no rollback was attempted."
            await _probe(old, version)
            # A failed new job may still be loaded, but an unresponsive process
            # cannot prove socket ownership: refuse to stop it on a guess.
            held = await asyncio.to_thread(self.launchd.held_job)
            if held is not None:
                if (
                    held.program != str(self.runtime.executable)
                    or held.arguments != (held.program, *self.runtime.server_arguments)
                    or held.login != previous.login
                ):
                    return "The login job identity changed; recovery was refused."
                if held.pid is not None and await _group_alive(held.pid):
                    return "The new job is still present; automatic recovery was refused."
                code, reason = await asyncio.to_thread(
                    self.launchd.ask,
                    [str(agent.LAUNCHCTL), "bootout", f"{self.launchd.domain}/{agent.LABEL}"],
                )
                if code:
                    return f"The failed job could not be removed: {reason}"
            outcome = await asyncio.to_thread(
                agent.install,
                self.directory,
                Resolution(runtime=old, reason=""),
                self.log_path,
                self.record_path,
                self.launchd,
            )
            if not outcome.ok:
                return f"Old-version recovery failed: {outcome.note}"
            await self._recovered(version, before, executable=old.executable)
            return "The existing old version was restored."
        except (OSError, ValueError, WireError, TimeoutError) as error:
            return f"No runnable old version could be restored: {error}"

    async def _owned_job(self, executable: Path | None = None) -> agent.HeldJob:
        held = await asyncio.to_thread(self.launchd.held_job)
        document = plistlib.loads(agent.plist_path(self.directory).read_bytes())
        if document.get("Label") != agent.LABEL or document.get("ProgramArguments") != [
            str(executable or self.runtime.executable),
            *self.runtime.server_arguments,
        ]:
            raise ValueError("the shared login job file is not the expected Codex installation")
        if (
            held is None
            or held.pid is None
            or held.login is None
            or held.arguments != (held.program, *self.runtime.server_arguments)
        ):
            raise ValueError("the shared server is not an identified VoiceCoding login job")
        # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2. This asks the kernel
        # which process owns the connected endpoint, not which file was written.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            peer.connect(str(self.runtime.control_socket))
            peer_pid = peer.getsockopt(0, 2)
        if os.getpgid(peer_pid) != held.pid or os.getpgid(held.pid) != held.pid:
            raise ValueError("the socket peer does not belong to the identified login job")
        return held

    async def _switch(self, held: agent.HeldJob, runtime: CodexRuntime) -> None:
        if await self._owned_job() != held:
            raise ValueError("the login job changed before the switch")
        # bootout returns before all processes exit; a single SIGTERM can wait
        # on a business turn. The kernel-proven job group is the interruption
        # boundary, including the npm shim's native child, never a name match.
        code, reason = await asyncio.to_thread(
            self.launchd.ask, ["/bin/kill", "-KILL", "--", f"-{held.pid}"]
        )
        if code:
            raise ValueError(f"the identified process group could not be stopped: {reason}")
        code, reason = await asyncio.to_thread(
            self.launchd.ask,
            [str(agent.LAUNCHCTL), "bootout", f"{self.launchd.domain}/{agent.LABEL}"],
        )
        if code:
            raise ValueError(f"launchd did not remove the old job: {reason}")
        # Removal is asynchronous even after bootout returns. Wait for the
        # definition and its group to disappear, not for business work to end.
        async with asyncio.timeout(agent.COMMAND_TIMEOUT_SECONDS):
            while await asyncio.to_thread(self.launchd.held_job) is not None or await _group_alive(
                held.pid
            ):
                await asyncio.sleep(0.05)
        outcome = await asyncio.to_thread(
            agent.install,
            self.directory,
            Resolution(runtime=runtime, reason=""),
            self.log_path,
            self.record_path,
            self.launchd,
        )
        if not outcome.ok:
            raise ValueError(outcome.note)

    async def _sessions(self, client: AppServerConnection):
        candidates = await self.list_sessions()

        async def listed():
            return candidates

        report = await discovery.discover(
            client,
            evidence=discovery.ProcessEvidence(list_sessions=listed, home=self.runtime.codex_home),
        )
        if report.error:
            raise ValueError(report.error)
        loaded = await client.request("thread/loaded/list", {})
        if not isinstance(loaded.get("data"), list):
            raise ValueError("the server did not return its loaded threads")
        found = {}
        for row in report.rows:
            if (
                row.target.pid is not None
                and row.child.is_main
                and row.has_controlling_terminal is True
                and row.target.session_id in loaded["data"]
            ):
                identity = await self.process_identity(row.target.pid)
                if identity is not None:
                    found[row.target] = identity
        return found

    async def _recovered(self, expected: str, before, *, executable: Path | None = None) -> None:
        async with asyncio.timeout(self.recovery_timeout):
            while True:
                client = AppServerConnection(self.runtime.control_socket)
                try:
                    await client.connect()
                    answer = await initialise(client, experimental=False, version=__version__)
                    if _server_version(answer) != expected:
                        raise ValueError("the shared server is not running the validated version")
                    await self._owned_job(executable)
                    remaining = set()
                    for target, original in before.items():
                        # Discovery may omit a living terminal when lsof fails.
                        # Only a direct process identity reading proves exit or
                        # PID reuse; an elapsed-time estimate cannot do that.
                        identity = await self.process_identity(target.pid)
                        if identity == original:
                            remaining.add(target)
                    recovered = await self._sessions(client)
                    if remaining.issubset(recovered):
                        return
                except (WireError, OSError):
                    pass
                finally:
                    await client.aclose()
                await asyncio.sleep(0.05)


async def _process_identity(pid: int) -> str | None:
    return await _ps(["-p", str(pid), "-o", "lstart="], missing_ok=True)


async def _group_alive(pid: int | None) -> bool:
    if pid is None:
        raise ValueError("the old process group was not identified")
    output = await _ps(["-axo", "pgid=,stat="])
    return any(
        fields[0] == str(pid) and not fields[1].startswith("Z")
        for line in output.splitlines()
        if len(fields := line.split()) == 2
    )


async def _ps(arguments: list[str], *, missing_ok: bool = False) -> str | None:
    process = await asyncio.create_subprocess_exec(
        "/bin/ps",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), HANDSHAKE_TIMEOUT_SECONDS)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if missing_ok and process.returncode == 1 and not output.strip() and not error.strip():
        return None
    if process.returncode or not output.strip():
        raise OSError("could not read process identity: " + error.decode("utf-8", "replace"))
    return output.decode().strip()


def _server_version(answer: dict) -> str:
    user_agent = answer.get("userAgent")
    match = re.match(r"[^/]+/(\S+) \(", user_agent) if isinstance(user_agent, str) else None
    if match is None:
        raise ValueError("the server did not identify its running version")
    return match[1]


async def _version(runtime: CodexRuntime) -> str:
    process = await asyncio.create_subprocess_exec(
        str(runtime.executable),
        "--version",
        env={**os.environ, **runtime.launch_environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), HANDSHAKE_TIMEOUT_SECONDS)
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    match = re.fullmatch(r"codex-cli (\S+)\s*", output.decode("utf-8", "replace"))
    if process.returncode or match is None:
        raise ValueError(error.decode("utf-8", "replace")[:200] or "unreadable Codex version")
    return match[1]


async def _probe(runtime: CodexRuntime, expected: str) -> None:
    # No credentials, user config, model calls, or persistent copies. The short
    # socket root is required by Darwin's sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="vc-codex-", dir="/tmp") as directory:
        root = Path(directory)
        socket_path = root / "probe.sock"
        environment = {"HOME": directory, "CODEX_HOME": directory, "PATH": runtime.path}
        process = await asyncio.create_subprocess_exec(
            str(runtime.executable),
            "app-server",
            "--listen",
            f"unix://{socket_path}",
            env=environment,
            cwd=directory,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_SECONDS):
                while True:
                    if process.returncode is not None:
                        raise ValueError(f"candidate app-server exited with {process.returncode}")
                    client = AppServerConnection(socket_path)
                    try:
                        await client.connect()
                        answer = await initialise(client, experimental=False, version=__version__)
                        if _server_version(answer) != expected:
                            raise ValueError("candidate app-server version disagrees with CLI")
                        return
                    except WireError:
                        await asyncio.sleep(0.05)
                    finally:
                        await client.aclose()
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


def _watch_paths(runtime: CodexRuntime) -> tuple[str, ...]:
    paths = {runtime.executable.absolute(), runtime.executable.resolve()}
    # The npm shim lives under the package whose dependencies it starts. Watch
    # that package, not only the symlink or shim, which can remain unchanged.
    for parent in runtime.executable.resolve().parents:
        if (parent / "package.json").is_file():
            paths.add(parent)
            break
    return tuple(sorted(map(str, paths)))


def _fingerprint(runtime: CodexRuntime, version: str) -> str:
    digest = hashlib.sha256(version.encode())
    for named in _watch_paths(runtime):
        path = Path(named)
        entries = [path, *path.rglob("*")] if path.is_dir() else [path]
        for entry in sorted(entries):
            stat = entry.lstat()
            digest.update(f"{entry}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check and follow a user's Codex upgrade.")
    parser.add_argument("--attempted", default="")
    args = parser.parse_args()
    directory = agent.default_launch_agents_directory()
    resolved = codex_runtime.resolve(os.environ, recorded_path=agent.recorded_path(directory))
    if not read_intent().install_wanted:
        result = UpdateResult("unchanged")
    elif resolved.runtime is None:
        path = codex_runtime.machine_path(os.environ, agent.recorded_path(directory)) or ""
        result = UpdateResult(
            "unchanged",
            watch_paths=tuple(
                str(Path(part) / codex_runtime.EXECUTABLE_NAME)
                for part in path.split(os.pathsep)
                if part
            ),
        )
    else:
        result = asyncio.run(
            CodexUpdate(runtime=resolved.runtime, attempted=args.attempted).check()
        )
    print(json.dumps(asdict(result)))
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
