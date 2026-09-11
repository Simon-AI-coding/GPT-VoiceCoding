"""The login `LaunchAgent` that starts the shared Codex app-server — #82, #83, #272.

The shared app-server has to be running **before** the user opens a `codex`, or
that TUI settles on its own embedded app-server and can never be adopted
afterwards (#82, proved again on #271's prototype: a server started later left
its loaded-thread roster at zero). Engine start is too late and first Relay is
later still, so the start is a macOS login item.

**It is the user's own codex, started as a plain server — #272, ADR 0022.** The
job runs `<the user's codex> app-server --listen unix://<control socket>`. There
is no `daemon` subcommand in it, no managed standalone tree, and nothing asks a
running process where it is listening: the three facts this needs are
`codex_runtime`'s, and the socket is derived. What replaced what, and why, is
that module's note and ADR 0022's.

**launchd owns the process now, and that is a change worth naming.** With
`daemon start` the job waited for the server's initialize and exited, leaving a
server that belonged to no job (`state = not running`); with `--listen` the job
*is* the server (`state = running, active count = 1`). `KeepAlive` is still
absent, so this is still not a supervisor — but `launchctl print` now answers
"is the shared server up" truthfully.

**No wrapper.** This job starts a server beside the user's `codex`; it does not
stand in front of it, rename it, or own any Session it serves (#68, #71, #82).

**One process action, and it is `bootstrap`.** ADR 0012's principle is *act, read
back, report*, not *write files only* — so this item renders the plist, writes it
through the boundary's atomic write, and then asks launchd to load the job now
rather than leaving the user's Codex half dead until they next log out. The
read-back is `launchctl print`, not the exit code: "already loaded" is not a
failure and launchd says so with a status nobody should have to interpret.

**Nothing here ever runs `bootout`, and that asymmetry is the rule.** *The
product starts a server the user's TUIs will join; it never stops one they are
attached to.* By the time an uninstall runs, the user's own `codex` sessions are
thin clients of this server, and a `bootout` would take every one of them down —
which is exactly what #83 forbids in the words "without stopping or deleting user
Sessions". So an uninstall removes the plist and lets the running server live out
the login session, and a changed render is written but not reloaded.

**That rule is also this ticket's migration, and it needs no branch.** A machine
that ran #82's job carries the same label with the managed standalone and
`daemon start` in it, and a live server under it. The reconcile re-renders *that
label* — one job, so two `RunAtLoad` plists can never race for one socket — and
does not stop what is running. The changed render is reported and takes effect
at the next login, which is already what a changed render does here. Codex's own
`~/.codex/packages/standalone/` is left alone: it is codex's directory, not this
product's.

**A reconcile is not a supervisor.** #83's scope forbids a polling supervisor, and
this is not one: `KeepAlive` is absent from the job, and the only thing that ever
re-bootstraps a job that died is the next app launch (ADR 0012), which is an
event and not a timer.

**The loaded render is this product's own read-back — #132.** `launchctl print`
exposes the program but not the whole loaded definition, so the same program can
hide an older resource limit or log path. When this item bootstraps a job it
records the exact plist SHA and the login it was loaded in in
`installation.json`. Status compares that SHA with the file on disk and keeps the
program comparison as the foreign-origin guard. A changed login proves a new
login loaded the bytes that were on disk before this reconcile; a second
reconcile in the same login proves no reload at all. Missing evidence is an
unknown render, never `current`.

**And a login is a pair, because the ASID alone repeats across reboots — #275.**
macOS hands the first GUI login of every boot the same audit session id, so a
record written two days and one reboot earlier compared equal to the running
login and was trusted as its evidence. The record and the read-back now carry
the kernel's `kern.bootsessionuuid` beside the ASID, and every "did the login
change?" compares the pair. A record carrying only an ASID cannot say which login
it was written in, so it is treated as one that carries none: the render is
unknown and it reports `stale` until the next login, never taken for a new login
outright — that is the direction that would report `current` over a job launchd
does not hold.
Legacy has no equivalent: `legacy@1d32845:install.sh:174-195` loaded its job and
never recorded or read back the loaded render, so this behavior is **not ported**.

**Nothing in the rendered job is hard-coded.** The user, their home,
`CODEX_HOME`, the executable and the `PATH` all come from the environment this
runs in. The updater the job follows is the user's own package manager: nothing
here pins a version, and nothing here names a path that was true only on the
machine that rendered it, which is #38.

**Legacy: adapted.** `legacy@1d32845:scripts/launch-agent.py:53-70` is the job
render (`plistlib`, `RunAtLoad`, `StandardOutPath`/`StandardErrorPath`) and
`legacy@1d32845:install.sh:174-195` is the launchd handling (`bootout` /
`bootstrap` in the per-login `gui/<uid>` domain). Two things are dropped on the
way across: legacy's `KeepAlive`, because that job was a supervised daemon and
this one is not supervised, and legacy's whole `stop_launch_agent` path, for the
reason above. The **shared Codex app-server itself is dropped from porting,
because** gen 1 had no such server — `legacy@1d32845:bridge/codex.py` drove a
launched, wrapped, per-Session app-server, and its launch marker is not adapted.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import plistlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from xml.parsers.expat import ExpatError

from gpt_voicecoding.installation import (
    BootstrappedRender,
    Outcome,
    State,
    read_bootstrapped_render,
    remove_file,
    replace_text,
    write_bootstrapped_render,
)
from gpt_voicecoding.installation.codex_runtime import (
    PATH_VARIABLE,
    CodexRuntime,
    Resolution,
)

#: How this item is named in a report.
NAME: Final = "codex-launch-agent"

#: The job's label, which is also its filename. Deliberately not the gen-1
#: `com.gpt-voicecoding.bridge` still sitting in real `~/Library/LaunchAgents`
#: directories: that one is #54's to dispose of, and a collision would have this
#: install silently replace a job it never wrote.
LABEL: Final = "com.gpt-voicecoding.codex-daemon"

#: Where the user's own login items live. macOS's directory, not Claude's — a
#: user who has never installed one simply does not have it yet, so unlike the
#: Claude config directory its absence is something to fix rather than to report.
LAUNCH_AGENTS_PARTS: Final = ("Library", "LaunchAgents")

#: #129 measured launchd's soft default at 256 and its hard limit as unlimited;
#: the shared app-server held 271 descriptors after two days. This is an install
#: invariant rather than a user setting, and both plist limits use this value.
OPEN_FILE_LIMIT: Final = 65_536

#: launchd's own path. Not resolved through `PATH`, because this is one of the
#: few binaries whose location is part of the operating system's contract.
LAUNCHCTL: Final = Path("/bin/launchctl")

#: How many subprocesses one run of this item may make: `launchctl print`,
#: `launchctl bootstrap`, and the `print` that reads the bootstrap back.
COMMANDS_PER_RUN: Final = 3

#: How long any one of them may take, and it is **derived, not chosen**. The only
#: measured ceiling in this picture is the shell's: it gives a whole reconcile
#: `Installation.deadline` seconds before it kills it, because this runs *before*
#: the engine is spawned and a wait without a ceiling is a product that never
#: starts. Three commands have to fit inside that with room for the rest of the
#: run, so each gets a third of it. `tests/test_codex_launch_agent.py` reads the
#: shell's number out of `Installation.swift` and holds the two together, which
#: is the guard #47 records as missing where a constant is spelled twice.
SHELL_RECONCILE_DEADLINE_SECONDS: Final = 30.0
COMMAND_TIMEOUT_SECONDS: Final = SHELL_RECONCILE_DEADLINE_SECONDS / COMMANDS_PER_RUN


def default_launch_agents_directory(home: Path | None = None) -> Path:
    return (home or Path.home()).joinpath(*LAUNCH_AGENTS_PARTS)


def plist_path(launch_agents_directory: Path) -> Path:
    return launch_agents_directory / f"{LABEL}.plist"


def _run(arguments: Sequence[str]) -> tuple[int, str]:
    """Run one command and come back with its status and whatever it said."""
    try:
        finished = subprocess.run(  # noqa: S603 - every argument here is this module's own
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except OSError as refusal:
        return (-1, str(refusal))
    except subprocess.TimeoutExpired:
        return (-1, f"{arguments[0]} did not answer within {COMMAND_TIMEOUT_SECONDS:.0f} seconds")
    return (finished.returncode, (finished.stdout + finished.stderr).strip())


#: The kernel's name for the boot this machine is in the middle of. Read rather
#: than derived, and read through `sysctlbyname` rather than through `/usr/sbin/
#: sysctl`, so that asking costs no subprocess and `COMMANDS_PER_RUN` — which the
#: command timeout is divided out of — stays the three launchd commands it names.
BOOT_SESSION_NAME: Final = "kern.bootsessionuuid"

#: A UUID string and its terminator. `sysctlbyname` is asked for the length it
#: wants, so this is only the buffer's ceiling, and a kernel that answered
#: something longer is one this does not pretend to understand.
BOOT_SESSION_SIZE: Final = 64


def _boot_session() -> str | None:
    """The kernel's boot session UUID, or `None` when it could not be read.

    `None` is a real answer and not an error: everywhere this is compared, an
    unknown boot session makes the login unknown, and an unknown login is
    reported rather than guessed at (`_loaded_identity_problem`). Failing closed
    here costs a `stale` line; failing open would restore exactly the false
    `current` this field exists to prevent (#275).
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        # Spelled out rather than left to ctypes' defaults: the third argument is
        # a `size_t *` that the kernel both reads and writes, and a machine where
        # an int-width guess and a pointer-width truth differ would corrupt the
        # stack rather than answer wrong.
        libc.sysctlbyname.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        libc.sysctlbyname.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(BOOT_SESSION_SIZE)
        size = ctypes.c_size_t(BOOT_SESSION_SIZE)
        answered = libc.sysctlbyname(
            BOOT_SESSION_NAME.encode("ascii"), buffer, ctypes.byref(size), None, 0
        )
    except (OSError, AttributeError, ValueError):
        return None
    if answered != 0:
        return None
    return buffer.value.decode("ascii", "replace") or None


@dataclass(frozen=True, slots=True)
class HeldJob:
    """The identity launchd exposes for one loaded job definition.

    ``login_asid`` and ``boot_session`` are one fact in two halves — see
    :attr:`login`, and :class:`BootstrappedRender` for the measurement that
    split them.
    """

    program: str
    login_asid: int | None
    boot_session: str | None

    @property
    def login(self) -> tuple[int, str] | None:
        """Which login this job is loaded in, or ``None`` when it cannot say."""
        if self.login_asid is None or self.boot_session is None:
            return None
        return (self.login_asid, self.boot_session)


@dataclass(frozen=True, slots=True)
class Launchd:
    """The user's own launchd domain, and the two questions this item asks it.

    A per-login-session `gui/<uid>` domain rather than the system one, ported from
    `legacy@1d32845:install.sh:207-208`: this job belongs to whoever is logged in,
    because the Codex app-server it starts is theirs and serves their terminals.

    Every entry point below takes a `Launchd` it cannot default, and `run` is
    resolved when it is *called* rather than when this class is defined. Both are
    guards against the same accident, which is not hypothetical: two drafts of
    this module reached the launchd of the machine running the tests — the first
    loaded a job naming a plist pytest deleted a second later, and the second
    installed the real login job and started the real shared server. So there is
    no default `Launchd` for the same reason `base_dir` runs through
    `locations`, and `_run` is late-bound so `tests/conftest.py` can take the
    real `launchctl` away from the whole suite at once.
    """

    domain: str
    #: ``None`` is the real ``launchctl``, looked up at call time.
    run: Callable[[Sequence[str]], tuple[int, str]] | None = None
    #: ``None`` is the real kernel, asked at call time. Here rather than at the
    #: call sites because a loaded job's login is one fact, and a test that could
    #: move only half of it could not stage a reboot at all.
    boot_session: Callable[[], str | None] | None = None

    def ask(self, arguments: Sequence[str]) -> tuple[int, str]:
        return (self.run or _run)(arguments)

    def ask_boot_session(self) -> str | None:
        return (self.boot_session or _boot_session)()

    def held_job(self) -> HeldJob | None:
        """The identity launchd currently holds for this job, if any.

        Three answers, and the third is the one that matters. `None` is *not
        loaded*. A non-empty ``program`` is the program in the job launchd holds,
        which is **not** necessarily the one in the file on disk: nothing here
        ever reloads a job, so after a render changes, the file and the loaded
        job disagree until the next login. Asking launchd rather than reading our
        own file back is the only way a status run can say that out loud.

        An empty ``program`` is *loaded, and launchd did not say what it runs*.

        ``login_asid`` is the GUI login's audit session identifier and
        ``boot_session`` is the kernel's boot session UUID, and it takes **both**
        to name the login this job is loaded in (#275). macOS changes the asid at
        a logout and login, not at a kickstart inside one login, which is what
        lets install tell a genuine reload opportunity from a second reconcile
        (#132) — but it hands the first GUI login of every boot the same asid, so
        across a reboot the asid alone says "same login" about two logins two
        days apart. The boot session is what tells reboots apart.
        """
        status, said = self.ask([str(LAUNCHCTL), "print", f"{self.domain}/{LABEL}"])
        if status != 0:
            return None
        found_program = re.search(r"^\s*program\s*=\s*(.+?)\s*$", said, re.MULTILINE)
        found_asid = re.search(r"^\s*asid\s*=\s*(\d+)\s*$", said, re.MULTILINE)
        return HeldJob(
            program=found_program.group(1) if found_program else "",
            login_asid=int(found_asid.group(1)) if found_asid else None,
            boot_session=self.ask_boot_session(),
        )

    def bootstrap(self, path: Path) -> tuple[HeldJob | None, str]:
        """Load the job now and return the identity read back from launchd.

        The exit status is not the answer: bootstrapping a job that is already
        loaded fails, and that is the state this is trying to reach. So it is
        attempted and then the identity read-back decides.
        """
        _, said = self.ask([str(LAUNCHCTL), "bootstrap", self.domain, str(path)])
        held = self.held_job()
        if held is not None:
            return (held, "")
        return (None, f"launchd did not load {LABEL}: {said or 'it said nothing'}")


def default_launchd() -> Launchd:
    return Launchd(domain=f"gui/{os.getuid()}")


def job(runtime: CodexRuntime, log_path: Path) -> dict[str, Any]:
    """The launchd job description, as a plist document.

    `RunAtLoad` and no `KeepAlive`: the job *is* the shared app-server now
    (#272), and a `KeepAlive` would make this a supervisor, which #83's scope
    forbids. What that costs is stated rather than hidden — a server that dies
    stays dead until the next login or the next app launch — and what it buys is
    that launchd never restarts a server the user's TUIs have just left.

    The environment is `codex_runtime`'s whole `launch_environment` and nothing
    composed here: `CODEX_HOME` because launchd hands a job none of the user's
    shell environment, and `PATH` because without one an npm codex cannot start
    under launchd at all. Both are that module's to explain.
    """
    return {
        "Label": LABEL,
        "ProgramArguments": [str(runtime.executable), *runtime.server_arguments],
        "RunAtLoad": True,
        "EnvironmentVariables": dict(runtime.launch_environment),
        "SoftResourceLimits": {"NumberOfFiles": OPEN_FILE_LIMIT},
        "HardResourceLimits": {"NumberOfFiles": OPEN_FILE_LIMIT},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def render(document: Mapping[str, Any]) -> str:
    """The plist's contents. Text, so it goes through the boundary's one write."""
    return plistlib.dumps(dict(document), sort_keys=True).decode("utf-8")


@dataclass(frozen=True, slots=True)
class JobFile:
    """One read of the plist, including the exact bytes launchd could load."""

    document: dict[str, Any]
    sha256: str


def _read(path: Path) -> JobFile | None | str:
    """The job that is there, `None` when there is none, or why neither."""
    try:
        contents = path.read_bytes()
        document: Any = plistlib.loads(contents)
    except FileNotFoundError:
        return None
    except OSError as refusal:
        return f"{path}: {refusal}"
    # `plistlib` raises three unrelated types for "this is not a plist": its own
    # for a binary one, `ExpatError` for XML that does not close, and `ValueError`
    # for XML that closes around something a plist cannot hold. All three mean the
    # same thing here, and none of them may reach a caller as an exception: this
    # runs before the engine, from a shell that has nowhere to put a traceback.
    except (plistlib.InvalidFileException, ExpatError, ValueError) as unreadable:
        return f"{path}: not a property list, so this install would destroy it: {unreadable}"
    if not isinstance(document, dict):
        return f"{path}: does not contain a property-list dictionary"
    if document.get("Label") != LABEL:
        return (
            f"{path}: carries the job {document.get('Label')!r}, which this product "
            f"never wrote. Nothing was changed."
        )
    return JobFile(document=document, sha256=hashlib.sha256(contents).hexdigest())


def recorded_path(launch_agents_directory: Path) -> str | None:
    """The ``PATH`` the standing job carries, or ``None`` when it carries none.

    **This machine's own record of a fact it cannot re-derive** — #327. The
    login `PATH` is read once, in Swift, and this side is handed the answer; a
    caller nobody handed it needs somewhere to get one that is not its own
    environment, and the plist is that somewhere. Not a second copy of the
    user's profile, which ``LoginShellPath``'s rule forbids: it is the copy
    launchd already loads, read back rather than kept beside.

    Every shape that is not one `PATH` string answers ``None``, and none of them
    raises. ``_read`` already refuses a file this product never wrote and one
    that is not a property list at all, and both arrive here as "no record" for
    the same reason the verbs treat them as "nothing to go on".
    """
    standing = _read(plist_path(launch_agents_directory))
    if not isinstance(standing, JobFile):
        return None
    environment = standing.document.get("EnvironmentVariables")
    if not isinstance(environment, Mapping):
        return None
    stated = environment.get(PATH_VARIABLE)
    if not isinstance(stated, str):
        return None
    return stated.strip() or None


#: Why the `PATH` in a no-codex reason may not be the user's own — #276, #327.
#:
#: The reason already names the `PATH` that was searched, where there was one.
#: What it does not say is where a searched `PATH` comes from, and that is the
#: difference between "this machine has no codex" and "this reconcile could not
#: see the one it has": the shell reads the user's login `PATH` and **states** it
#: to this subprocess, that read **fails open**, and a run nobody stated one to
#: has only what the standing job records — which on a machine that has never had
#: one is nothing at all.
#:
#: **Unconditional, and it names no value.** Deciding this wording by comparing
#: the searched `PATH` against a hard-coded launchd default is #38 exactly — a
#: constant standing in for something the environment already states, and one
#: that would go quietly wrong the day Apple changes it. So the sentence is
#: always said, and it points at the surface that knows: the menu bar reports
#: every login-shell read, failed or not (#118).
#:
#: **It points at nothing, which is what lets it be unconditional.**
#: `codex_runtime.resolve` composes two reasons, and the second is that nothing
#: stated a `PATH` and nothing recorded one — after which "that PATH" would refer
#: to something the same sentence had just said does not exist. So this states
#: the rule rather than the value, and reads as well after either.
_PATH_PROVENANCE: Final = (
    "A reconcile renders over the PATH the app states from its login-shell read, "
    "or else the one the standing job records; the menu bar reports the reading"
)


def _no_codex(reason: str) -> Outcome:
    """The Codex lane reporting itself absent, with the reason — never an error.

    A machine with no codex on it is a machine this product has nothing to start
    and nothing to install. That is a fact about the machine, so it is said and
    the run carries on: `ok` stays true, and the next reconcile after the user
    installs one puts the job there.

    **`ok` stays true even when the `PATH` is the reason** (Simon's ruling on
    #276). The other candidate was to report `STALE, ok=False` when the read
    failed, which is the honest state and costs the Python side an inherited
    fact about the shell. This line was chosen instead: the installation is
    still `ok`, nothing is written, and the note says the cause — see
    `_PATH_PROVENANCE`.
    """
    return Outcome(
        NAME,
        State.ABSENT,
        note=f"{reason} — nothing to start, so nothing to install. {_PATH_PROVENANCE}",
    )


def _no_machine_path(reason: str) -> Outcome:
    """A caller with no standing to say what this machine's `PATH` is — #327.

    **`ok` is false here, and that is the one place it differs from `_no_codex`**
    (Simon's ruling, 2026-09-11, on #327's review). The two look alike and are
    not: "there is no codex on this machine's PATH" is a fact about the machine,
    said while the run carries on, and #276 ruled it `ok`. This is a run that
    could not find out what the machine's `PATH` *is* — nothing stated one and no
    standing job records one — so there is no render to compare against and none
    to write, and a zero exit would report an installation that never happened.

    Nothing is written either way. What changes is that the run says so.
    """
    return Outcome(
        NAME,
        State.ABSENT,
        ok=False,
        note=(
            f"{reason} — nothing to render, so nothing was written. Open the app, "
            f"which states it. {_PATH_PROVENANCE}"
        ),
    )


def _previous_render_note(path: Path) -> str:
    return f"{path} — loaded job is a previous render; applies at the next login"


def _unknown_render_note(path: Path) -> str:
    return f"{path} — loaded job is of unknown render; applies at the next login"


def _unknown_identity_note(path: Path, reason: str) -> str:
    return f"{path} — the job is loaded, and launchd {reason}; loaded render is unknown"


def _unknown_boot_session_note(path: Path) -> str:
    """The other half of an unknown login, and it is **not** launchd's half.

    A login is an ASID and a boot session (#275), and the boot session is read
    from the kernel by this module rather than reported by launchd. Blaming
    launchd for it would send a reader to `launchctl print`, where the ASID is
    sitting in plain view and nothing is wrong.
    """
    return (
        f"{path} — the job is loaded, and this machine did not answer which boot "
        f"session it is in ({BOOT_SESSION_NAME}); loaded render is unknown"
    )


def _differing_keys(standing: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[str]:
    """Which keys of the job document the two disagree on, dotted one level deep.

    One level and no further, because that is where the answer stops being
    useful: `EnvironmentVariables.PATH` names a thing the reader can go and look
    at, and a path into a resource-limit dictionary's only value would not.

    This exists because the note it feeds was `is a job this build would write
    differently` and nothing else, which is true of a plist whose whole
    difference is a `PATH` the reporter's own terminal caused (#275) and equally
    true of one that names a different program. Sorted, so the same disagreement
    reads the same way twice.
    """
    names: list[str] = []
    for key in sorted(set(standing) | set(wanted)):
        mine, theirs = standing.get(key), wanted.get(key)
        if mine == theirs:
            continue
        if isinstance(mine, Mapping) and isinstance(theirs, Mapping):
            names.extend(f"{key}.{inner}" for inner in _differing_keys(mine, theirs))
        else:
            names.append(key)
    return names


def _program_in(standing: JobFile | None) -> str | None:
    if standing is None:
        return None
    arguments = standing.document.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or not isinstance(arguments[0], str):
        return None
    return arguments[0]


def _program_mismatch_note(path: Path, actual: str, expected: str) -> str:
    return (
        f"{path} is current, and the job launchd holds runs {actual}, not {expected}. "
        "It is not reloaded, because that would stop the app-server live Sessions are on; "
        "the file applies at the next login."
    )


def _loaded_identity_problem(path: Path, held: HeldJob | None, expected_program: str) -> str:
    if held is None or not held.program:
        return _unknown_identity_note(path, "did not say what it runs")
    if held.program != expected_program:
        return _program_mismatch_note(path, held.program, expected_program)
    if held.login_asid is None:
        return _unknown_identity_note(path, "did not say which login loaded it")
    if held.boot_session is None:
        return _unknown_boot_session_note(path)
    return ""


def inspect(
    launch_agents_directory: Path,
    codex: Resolution,
    log_path: Path,
    record_path: Path,
    launchd: Launchd,
) -> Outcome:
    """What is on this machine, without changing any of it.

    Three facts, and all are reported: whether the plist is the one this build
    renders, whether launchd currently holds the job, and whether the recorded
    loaded-render SHA matches the file. A plist that is current with no job, or
    whose loaded SHA differs, is a machine that is not current now.
    """
    # The file is read before the codex question, not after — #327. Reading the
    # standing job for its `PATH` gave every unreadable one a second, quieter
    # reading: `recorded_path` answers `None` for a file it refuses, the resolver
    # then has nothing to render over, and a `_no_codex` taken first would report
    # `absent, ok` about a plist this boundary had already refused to touch. So
    # "this install would destroy it" is said before "there is nothing to install".
    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=standing)
    if codex.path_unknown:
        return _no_machine_path(codex.reason)
    if codex.runtime is None:
        return _no_codex(codex.reason)
    runtime = codex.runtime

    # Asked once. Asked twice, the two answers can differ — launchd is a live
    # thing — and the report would then carry a `state` decided by one reading
    # and a sentence describing the other.
    running = launchd.held_job()
    held = "the job is not loaded" if running is None else "the job is loaded"
    if standing is None:
        if running is not None:
            return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
        return Outcome(NAME, State.ABSENT, note=f"no login job at {path} — {held}")
    wanted = job(runtime, log_path)
    if standing.document != wanted:
        differing = _differing_keys(standing.document, wanted)
        named = f": {', '.join(differing)}" if differing else ""
        return Outcome(
            NAME, State.STALE, note=f"{path} is a job this build would write differently{named}"
        )
    if running is None:
        return Outcome(NAME, State.STALE, note=f"{path} is current, and {held}")
    identity_problem = _loaded_identity_problem(path, running, str(runtime.executable))
    if identity_problem:
        return Outcome(NAME, State.STALE, note=identity_problem)
    loaded = read_bootstrapped_render(record_path)
    if loaded is None or loaded.render_sha256 is None:
        return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
    if loaded.login is None:
        return Outcome(
            NAME,
            State.STALE,
            note=_unknown_identity_note(path, "did not record which login loaded it"),
        )
    if loaded.render_sha256 != standing.sha256:
        return Outcome(NAME, State.STALE, note=_previous_render_note(path))
    return Outcome(NAME, State.CURRENT, note=f"{path} — {held}")


def install(
    launch_agents_directory: Path,
    codex: Resolution,
    log_path: Path,
    record_path: Path,
    launchd: Launchd,
) -> Outcome:
    """Put the job where launchd finds it, and have launchd hold it now. Idempotent.

    The file is read before the codex question for `inspect`'s reason, which is
    sharper here: this is the verb that would otherwise go on to write.
    """
    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=standing)
    if codex.path_unknown:
        return _no_machine_path(codex.reason)
    if codex.runtime is None:
        return _no_codex(codex.reason)
    runtime = codex.runtime

    wanted = job(runtime, log_path)
    wanted_text = render(wanted)
    wanted_sha256 = hashlib.sha256(wanted_text.encode("utf-8")).hexdigest()
    held = launchd.held_job()  # asked before the write, so the note below is true of it
    loaded = read_bootstrapped_render(record_path)
    disk_program = _program_in(standing if isinstance(standing, JobFile) else None)

    if held is not None:
        # A missing record proves nothing about the job already in launchd. Keep
        # that uncertainty for this login. If the login changed, launchd loaded
        # whatever bytes were on disk *before this reconcile writes*, so those
        # bytes become the authority for the new login (#132 advisor ruling).
        if loaded is None or loaded.login is None:
            # A record that cannot name the login it was written in proves
            # nothing about the job already in launchd — whether because there is
            # none, or because it predates #275 and carries only an asid, which
            # repeats across boots. Either way the render is unknown for this
            # login, and it is *not* taken for a new one: that is the false
            # `current` direction.
            loaded = BootstrappedRender(
                render_sha256=None,
                login_asid=held.login_asid,
                boot_session=held.boot_session,
            )
            failure = write_bootstrapped_render(record_path, loaded)
            if failure:
                return Outcome(NAME, State.STALE, ok=False, note=failure)
        elif held.login is not None and held.login != loaded.login:
            loaded = BootstrappedRender(
                render_sha256=(
                    standing.sha256
                    if isinstance(standing, JobFile)
                    and bool(held.program)
                    and held.program == disk_program
                    else None
                ),
                login_asid=held.login_asid,
                boot_session=held.boot_session,
            )
            failure = write_bootstrapped_render(record_path, loaded)
            if failure:
                return Outcome(NAME, State.STALE, ok=False, note=failure)

    rewritten = False
    if standing is None or standing.document != wanted:
        # launchd will not spawn a job whose output path names a directory that
        # is not there, and it reports that as the job failing to start — a
        # failure whose reason would land in the file it could not open.
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as refusal:
            return Outcome(NAME, State.ABSENT, ok=False, note=f"{log_path.parent}: {refusal}")
        failure = replace_text(path, wanted_text)
        if failure:
            return Outcome(NAME, State.ABSENT, ok=False, note=failure)
        rewritten = True

    if held is not None:
        identity_problem = _loaded_identity_problem(path, held, str(runtime.executable))
        if identity_problem:
            return Outcome(NAME, State.STALE, changed=rewritten, note=identity_problem)
        if rewritten:
            if loaded is not None and loaded.render_sha256 == wanted_sha256:
                return Outcome(
                    NAME,
                    State.CURRENT,
                    changed=True,
                    note=f"{path} written — the same render is already loaded",
                )
            if loaded is None or loaded.render_sha256 is None:
                return Outcome(
                    NAME,
                    State.STALE,
                    changed=True,
                    note=(
                        f"{path} written — loaded job is of unknown render; "
                        "applies at the next login"
                    ),
                )
            # Reloading it means `bootout`, and `bootout` stops the app-server
            # the user's own TUIs are attached to. The job the user has is the
            # one that was already right for them; the new render is next
            # login's. This is also #272's migration of a machine that ran #82's
            # job: same label, new program, and nothing stopped.
            return Outcome(
                NAME,
                State.STALE,
                changed=True,
                note=(
                    f"{path} written — the loaded job is the previous render and was not "
                    "reloaded, because that would stop the app-server live Sessions are on. "
                    "It applies at the next login."
                ),
            )
        if loaded is None or loaded.render_sha256 is None:
            return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
        if loaded.login is None:
            return Outcome(
                NAME,
                State.STALE,
                note=_unknown_identity_note(path, "did not record which login loaded it"),
            )
        if loaded.render_sha256 != standing.sha256:
            return Outcome(NAME, State.STALE, note=_previous_render_note(path))
        return Outcome(NAME, State.CURRENT, note=f"{path} — the job is loaded")

    bootstrapped, refusal = launchd.bootstrap(path)
    if refusal:
        return Outcome(NAME, State.STALE, changed=rewritten, ok=False, note=refusal)
    identity_problem = _loaded_identity_problem(path, bootstrapped, str(runtime.executable))
    loaded = BootstrappedRender(
        render_sha256=(wanted_sha256 if rewritten or standing is None else standing.sha256)
        if not identity_problem
        else None,
        login_asid=bootstrapped.login_asid if bootstrapped is not None else None,
        boot_session=bootstrapped.boot_session if bootstrapped is not None else None,
    )
    failure = write_bootstrapped_render(record_path, loaded)
    if failure:
        return Outcome(NAME, State.STALE, changed=True, ok=False, note=failure)
    if identity_problem:
        return Outcome(NAME, State.STALE, changed=True, note=identity_problem)
    return Outcome(NAME, State.CURRENT, changed=True, note=f"{path} — the job is loaded")


def uninstall(launch_agents_directory: Path) -> Outcome:
    """Take the job's file back, and leave the running app-server alone.

    No `bootout`. See the module note: by now the user's own Codex Sessions are
    thin clients of the server this job started, and stopping it would take
    every one of them down. Without the file, launchd does not load it again.
    """
    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.STALE, ok=False, note=standing)
    if standing is None:
        return Outcome(NAME, State.ABSENT, note=f"nothing of ours at {path}")

    failure = remove_file(path)
    if failure:
        return Outcome(NAME, State.STALE, ok=False, note=failure)
    return Outcome(
        NAME,
        State.ABSENT,
        changed=True,
        note=(
            f"{path} removed — an app-server that is already running was left alone "
            "and lives until logout"
        ),
    )
