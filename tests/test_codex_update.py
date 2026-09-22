"""The app's single update entry point, with isolated files and protocol peers."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from codex_fake import FakeAppServer
from codex_update_fake import UpdateMachine
from gpt_voicecoding.adapters.agent.codex.processes import Candidate
from gpt_voicecoding.codex_update import CodexUpdate
from gpt_voicecoding.installation import codex_launch_agent as agent
from gpt_voicecoding.installation.codex_runtime import CodexRuntime


def test_matching_version_does_not_restart(socket_root: Path) -> None:
    async def scenario() -> None:
        executable = socket_root / "codex"
        executable.write_text("#!/bin/sh\nprintf 'codex-cli 1.2.3\\n'\n")
        executable.chmod(0o700)
        runtime = CodexRuntime(
            executable, socket_root, socket_root / "server.sock", "/usr/bin:/bin"
        )
        async with FakeAppServer(runtime.control_socket) as server:
            server.answers("initialize", {"userAgent": "codex_cli_rs/1.2.3 (Mac OS)"})
            updater = CodexUpdate(runtime=runtime)
            result = await updater.check()
            assert result.status == "unchanged"
            assert not result.failure

    asyncio.run(scenario())


def test_valid_upgrade_switches_once_and_reads_new_server(socket_root: Path) -> None:
    async def scenario() -> None:
        machine = UpdateMachine(socket_root)
        runtime = CodexRuntime(
            machine.executable, socket_root, socket_root / "s.sock", os.environ["PATH"]
        )
        path = agent.plist_path(socket_root)
        path.write_text(agent.render(agent.job(runtime, socket_root / "server.log")))
        machine.launchd.bootstrap(path)
        try:
            for _ in range(100):
                if runtime.control_socket.exists():
                    break
                await asyncio.sleep(0.01)
            machine.version.write_text("1.2.4")
            updater = CodexUpdate(
                runtime=runtime,
                launchd=machine.launchd,
                directory=socket_root,
                record_path=socket_root / "record.json",
                log_path=socket_root / "server.log",
                list_sessions=lambda: empty_sessions(),
            )
            first, second = await asyncio.gather(updater.check(), updater.check())
            assert [first.status, second.status] == ["updated", "unchanged"]
            assert machine.commands.count("bootout") == 1
        finally:
            machine.close()

    asyncio.run(scenario())


async def empty_sessions():
    return ()


def test_incomplete_installation_keeps_old_server(socket_root: Path) -> None:
    async def scenario() -> None:
        runtime = CodexRuntime(
            socket_root / "missing", socket_root, socket_root / "s.sock", "/usr/bin:/bin"
        )
        async with FakeAppServer(runtime.control_socket) as server:
            server.answers("initialize", {"userAgent": "codex_cli_rs/1.2.3 (Mac OS)"})
            result = await CodexUpdate(runtime=runtime).check()
            assert result.status == "failed"
            assert result.failure
            assert server.path.exists()
            assert not server.calls_to("turn/start")

    asyncio.run(scenario())


def test_version_output_alone_does_not_prove_candidate_ready(socket_root: Path) -> None:
    async def scenario() -> None:
        executable = socket_root / "codex"
        executable.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "codex-cli 1.2.4"; '
            'else echo "missing runtime dependency" >&2; exit 1; fi\n'
        )
        executable.chmod(0o700)
        runtime = CodexRuntime(executable, socket_root, socket_root / "s.sock", "/usr/bin:/bin")
        async with FakeAppServer(runtime.control_socket) as server:
            server.answers("initialize", {"userAgent": "codex_cli_rs/1.2.3 (Mac OS)"})
            result = await CodexUpdate(runtime=runtime).check()
            assert result.status == "failed"
            assert "candidate" in result.failure.lower()
            assert server.path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("reading", ["steady", "missing", "drifting"])
def test_recovery_requires_original_live_session(
    socket_root: Path, closed: bool, reading: str
) -> None:
    async def scenario():
        machine = UpdateMachine(socket_root)
        machine.threads.write_text(
            json.dumps(
                [
                    {
                        "id": "original",
                        "threadSource": "user",
                        "cwd": str(socket_root),
                        "createdAt": 100,
                        "status": {"type": "active"},
                    }
                ]
            )
        )
        runtime = CodexRuntime(
            machine.executable, socket_root, socket_root / "s.sock", os.environ["PATH"]
        )
        path = agent.plist_path(socket_root)
        path.write_text(agent.render(agent.job(runtime, socket_root / "server.log")))
        machine.launchd.bootstrap(path)

        async def sessions():
            if (closed or reading == "missing") and "bootout" in machine.commands:
                return ()
            started = 200 if reading == "drifting" and "bootout" in machine.commands else 99
            return (
                Candidate(
                    pid=123, workspace=socket_root, session_id="original", started_at=started
                ),
            )

        async def identity(pid):
            return None if closed and "bootout" in machine.commands else "same live process"

        try:
            while not runtime.control_socket.exists():
                await asyncio.sleep(0.01)
            machine.version.write_text("1.2.4")
            # The terminal never resumes on the new server. Only closing it
            # legitimately removes it from the required recovery set.
            machine.threads.write_text("[]")
            updater = CodexUpdate(
                runtime=runtime,
                launchd=machine.launchd,
                directory=socket_root,
                record_path=socket_root / "record.json",
                log_path=socket_root / "log",
                list_sessions=sessions,
                process_identity=identity,
                recovery_timeout=0.5,
            )
            result = await updater.check()
            assert result.status == ("updated" if closed else "failed"), result
            assert machine.commands.count("bootout") == 1
            again = await updater.check()
            assert again.status == "unchanged"
            assert machine.commands.count("bootout") == 1
        finally:
            machine.close()

    asyncio.run(scenario())


def test_failed_start_without_old_binary_does_not_retry(socket_root: Path):
    async def scenario():
        machine = UpdateMachine(socket_root)
        runtime = CodexRuntime(
            machine.executable, socket_root, socket_root / "s.sock", os.environ["PATH"]
        )
        path = agent.plist_path(socket_root)
        path.write_text(agent.render(agent.job(runtime, socket_root / "log")))
        machine.launchd.bootstrap(path)
        try:
            while not runtime.control_socket.exists():
                await asyncio.sleep(0.01)
            machine.version.write_text("1.2.4")
            machine.refuse_start = True
            updater = CodexUpdate(
                runtime=runtime,
                launchd=machine.launchd,
                directory=socket_root,
                record_path=socket_root / "record.json",
                log_path=socket_root / "log",
                list_sessions=empty_sessions,
            )
            result = await updater.check()
            assert result.status == "failed"
            assert "old executable is no longer available" in result.failure
            assert machine.commands.count("bootstrap") == 2
            assert result.attempted
        finally:
            machine.close()

    asyncio.run(scenario())


def test_failed_candidate_restores_only_a_still_runnable_old_version(socket_root: Path):
    async def scenario():
        old_root, new_root = socket_root / "old", socket_root / "new"
        old_root.mkdir()
        new_root.mkdir()
        machine, candidate = UpdateMachine(old_root), UpdateMachine(new_root)
        candidate.version.write_text("1.2.4")
        runtime = CodexRuntime(
            candidate.executable, socket_root, socket_root / "s.sock", os.environ["PATH"]
        )
        old_runtime = CodexRuntime(
            machine.executable, socket_root, runtime.control_socket, runtime.path
        )
        path = agent.plist_path(socket_root)
        path.write_text(agent.render(agent.job(old_runtime, socket_root / "log")))
        machine.launchd.bootstrap(path)
        path.write_text(agent.render(agent.job(runtime, socket_root / "log")))

        def launchctl(arguments):
            if arguments[1] == "bootstrap":
                import plistlib

                program = plistlib.loads(Path(arguments[-1]).read_bytes())["ProgramArguments"][0]
                if program == str(candidate.executable):
                    return 1, "new candidate cannot start in the real environment"
            return machine.run(arguments)

        launchd = agent.Launchd("gui/test", launchctl, lambda: "test-boot")
        try:
            while not runtime.control_socket.exists():
                await asyncio.sleep(0.01)
            result = await CodexUpdate(
                runtime=runtime,
                launchd=launchd,
                directory=socket_root,
                record_path=socket_root / "record.json",
                log_path=socket_root / "log",
                list_sessions=empty_sessions,
            ).check()
            assert result.status == "failed"
            assert "existing old version was restored" in result.failure, result
            assert machine.arguments[0] == str(machine.executable)
        finally:
            machine.close()

    asyncio.run(scenario())


def test_foreign_socket_peer_is_never_stopped(socket_root: Path):
    async def scenario():
        machine = UpdateMachine(socket_root)
        runtime = CodexRuntime(
            machine.executable, socket_root, socket_root / "s.sock", os.environ["PATH"]
        )
        path = agent.plist_path(socket_root)
        path.write_text(agent.render(agent.job(runtime, socket_root / "log")))
        machine.launchd.bootstrap(path)

        def launchctl(arguments):
            code, output = machine.run(arguments)
            if arguments[1] == "print":
                output = output.replace(f"pid = {machine.process.pid}", f"pid = {os.getpid()}")
            return code, output

        try:
            while not runtime.control_socket.exists():
                await asyncio.sleep(0.01)
            machine.version.write_text("1.2.4")
            updater = CodexUpdate(
                runtime=runtime,
                launchd=agent.Launchd("gui/test", launchctl, lambda: "test-boot"),
                directory=socket_root,
                record_path=socket_root / "record.json",
                log_path=socket_root / "log",
                list_sessions=empty_sessions,
            )
            result = await updater.check()
            assert result.status == "failed"
            assert "socket peer does not belong" in result.failure
            assert machine.process.poll() is None
            assert "bootout" not in machine.commands
        finally:
            machine.close()

    asyncio.run(scenario())
