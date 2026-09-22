"""An executable protocol peer and an isolated launchd command boundary."""

import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

from gpt_voicecoding.installation import codex_launch_agent as agent


class UpdateMachine:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.executable = root / "codex"
        self.version = root / "version"
        self.version.write_text("1.2.3")
        self.threads = root / "threads.json"
        self.threads.write_text("[]")
        self.refuse_start = False
        self.removing = 0
        self.removed_output = ""
        import_paths = [str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")]
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import asyncio, sys, json\nfrom pathlib import Path\n"
            f"sys.path[:0] = {import_paths!r}\n"
            "from codex_fake import FakeAppServer\n"
            f"version = Path({str(self.version)!r}).read_text()\n"
            "if '--version' in sys.argv:\n print('codex-cli '+version); sys.exit(0)\n"
            "async def main():\n"
            " path = Path(sys.argv[sys.argv.index('--listen')+1].removeprefix('unix://'))\n"
            " path.parent.mkdir(parents=True, exist_ok=True)\n"
            f" threads = json.loads(Path({str(self.threads)!r}).read_text())\n"
            " async with FakeAppServer(path) as server:\n"
            "  server.answers('initialize', {'userAgent':'gpt-voicecoding/'+version+' (test)'})\n"
            "  server.answers('thread/loaded/list', {'data': [t['id'] for t in threads]})\n"
            "  server.answers('thread/read', lambda p: {'thread': "
            "next(t for t in threads if t['id']==p['threadId'])})\n"
            "  await asyncio.Event().wait()\n"
            "asyncio.run(main())\n"
        )
        self.executable.chmod(0o700)
        self.process: subprocess.Popen | None = None
        self.arguments: list[str] = []
        self.commands: list[str] = []
        self.launchd = agent.Launchd("gui/test", self.run, lambda: "test-boot")

    def run(self, argv):
        if argv[0] == "/bin/kill":
            assert self.process is not None and argv[-1] == f"-{self.process.pid}"
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            return 0, ""
        verb = argv[1]
        self.commands.append(verb)
        if verb == "print":
            if self.removing:
                self.removing -= 1
                return 0, self.removed_output
            if self.process is None:
                return 1, "not loaded"
            return 0, (
                f"program = {self.arguments[0]}\nasid = 1\npid = {self.process.pid}\n"
                "arguments = {\n" + "\n".join(self.arguments) + "\n}\n"
            )
        if verb == "bootout":
            _, self.removed_output = self.run(["launchctl", "print"])
            self.close()
            self.removing = 2
            return 0, ""
        if verb == "bootstrap":
            if self.refuse_start:
                return 1, "candidate refused to start"
            document = plistlib.loads(Path(argv[-1]).read_bytes())
            self.arguments = document["ProgramArguments"]
            Path(self.arguments[-1].removeprefix("unix://")).unlink(missing_ok=True)
            self.process = subprocess.Popen(
                self.arguments,
                env={**os.environ, **document["EnvironmentVariables"]},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return 0, ""
        raise AssertionError(argv)

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            self.process = None
