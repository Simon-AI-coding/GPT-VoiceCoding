"""A launchd that answers what a test told it to, and the Codex home beside it.

**Why this is not in `conftest.py`, which is where it started.** `conftest.py` is
pytest's own file: pytest loads it *by path*, but `from conftest import …` is an
ordinary import and goes through `sys.path` — where the name `conftest` is not
unique, because `tests/acceptance/` has one too. With only `[dev]` installed the
acceptance conftest is `importorskip`ped, its directory never reaches `sys.path`,
and the bare name resolves here. Install `[acceptance]` and it resolves *there*
instead, and the whole unit suite dies at collection ([#93](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/93)).

So the rule this file exists to keep: **a test module never imports a
`conftest`.** Anything two test files share lives in a module with a name of its
own, the way `fakes.py` and `hub.py` already do. `conftest.py` keeps only what
pytest asks it for — the fixtures — and reaches *here* for the pieces they need.
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.installation import codex_launch_agent, codex_runtime

#: The domain a fake launchd answers in. A real one is `gui/<uid>`; this is that
#: shape and no user's, so a command built from it could not work by accident.
DOMAIN = "gui/501"


class FakeLaunchd:
    """A launchd that records what it was asked and answers what it was told to.

    It holds a *program* and not just a flag, because that is the thing real
    launchd holds: a job loaded from one render keeps running that render's
    program after the file on disk has been rewritten, and a fake that answered a
    bare yes could not tell the two apart. `bootstrap` reads the program out of
    the plist it is handed, exactly as launchd does, and never re-reads it after.

    Nothing here ever *unloads* a job, and that is the point: this product has no
    verb that does, and any test that made one pass would be testing a product
    that stops daemons its user's Sessions are attached to.
    """

    def __init__(self) -> None:
        #: Set by a test that wants launchd to turn the job down.
        self.refuses = False
        self.commands: list[list[str]] = []
        #: `None` is "launchd holds nothing". A test sets it to stand a job up or
        #: to kill one; `bootstrap` sets it the way launchd would.
        self.program: str | None = None
        #: A GUI login's audit session identifier. A kickstart leaves it alone;
        #: a new login changes it, which is #132's reload evidence.
        self.login_asid = 100_016
        #: The kernel's boot session UUID, which is the other half of #275's
        #: login pair. Separate from the asid, and moved separately, because the
        #: defect is a real machine where the asid repeated and only this
        #: changed: a fake that moved them together could not stage it.
        self.boot_session: str | None = "8B0F0000-1111-4222-8333-000000000001"
        #: The next one `begin_login(reboot=True)` will hand out.
        self.next_boot = 2

    @property
    def held(self) -> bool:
        return self.program is not None

    def begin_login(self, path: Path, *, reboot: bool = False) -> None:
        """Start a new fake GUI login and load the plist then on disk.

        `reboot=True` is the login macOS gives the *same* asid as the last boot's
        first one — measured, and #275's defect — so it moves the boot session
        and leaves the asid where it is.
        """
        if reboot:
            self.boot_session = f"8B0F0000-1111-4222-8333-{self.next_boot:012d}"
            self.next_boot += 1
        else:
            self.login_asid += 1
        if not path.exists():
            self.program = None
            return
        self.program = plistlib.loads(path.read_bytes())["ProgramArguments"][0]

    def __call__(self, arguments: Sequence[str]) -> tuple[int, str]:
        self.commands.append(list(arguments))
        verb = arguments[1]
        if verb == "print":
            if self.program is None:
                return (113, "Could not find service")
            # The shape real launchd answers in, kept to the two lines this reads.
            return (
                0,
                f"{arguments[2]} = {{\n\tstate = running\n"
                f"\tprogram = {self.program}\n\tasid = {self.login_asid}\n}}",
            )
        if verb == "bootstrap":
            if self.refuses:
                return (5, "Input/output error")
            self.program = plistlib.loads(Path(arguments[3]).read_bytes())["ProgramArguments"][0]
            return (0, "")
        raise AssertionError(f"this item may not run `launchctl {verb}`")

    @property
    def launchd(self) -> codex_launch_agent.Launchd:
        return codex_launch_agent.Launchd(
            domain=DOMAIN, run=self, boot_session=lambda: self.boot_session
        )

    @property
    def verbs(self) -> list[str]:
        return [command[1] for command in self.commands]


def codex_home(root: Path) -> Path:
    """A `CODEX_HOME` under `root`. Nothing this product owns lives in it.

    Since #272 the Codex item derives one thing from this directory — the
    control socket — and takes the executable off the `PATH` instead. So a home
    is now just a directory, and what makes the item a real participant rather
    than a permanent `ABSENT` is :func:`codex_on_path`.
    """
    home = root / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    return home


def codex_on_path(root: Path) -> Path:
    """A directory holding an executable `codex`, for a `PATH` to be built from.

    Shared, because both the item's own tests and the boundary's build the same
    thing, and two spellings of it would drift the moment resolution does. The
    file carries the executable bit because that is what `which` looks for — a
    `codex` without it is a machine with no codex on it, which is a case of its
    own and has a test of its own.
    """
    directory = root / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / codex_runtime.EXECUTABLE_NAME
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return directory


def codex(root: Path, *, home: Path | None = None) -> codex_runtime.Resolution:
    """What a test machine rooted at `root` resolves its one codex to.

    Built by running the real :func:`codex_runtime.resolve` over an environment
    the test composed, rather than by constructing a `CodexRuntime` outright: a
    fake that skipped resolution would let the item's tests pass over a runtime
    the resolver could never produce.

    The `PATH` is **stated**, under `LOGIN_PATH_VARIABLE`, because that is what
    the shell does and since #327 it is the only thing that answers. A fixture
    that set `PATH` here would be composing the one input the resolver is
    required to ignore, and every test built on it would pass over a machine.
    """
    return codex_runtime.resolve(
        {
            codex_runtime.LOGIN_PATH_VARIABLE: str(codex_on_path(root)),
            codex_runtime.CODEX_HOME_VARIABLE: str(home if home is not None else codex_home(root)),
        }
    )


def no_codex(root: Path) -> codex_runtime.Resolution:
    """A machine with a stated `PATH` and no codex anywhere on it."""
    empty = root / "empty-bin"
    empty.mkdir(parents=True, exist_ok=True)
    return codex_runtime.resolve(
        {
            codex_runtime.LOGIN_PATH_VARIABLE: str(empty),
            codex_runtime.CODEX_HOME_VARIABLE: str(codex_home(root)),
        }
    )
