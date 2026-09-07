"""The Codex login `LaunchAgent` — #83, on ADR 0012's boundary.

The load-bearing properties are different from the Claude item's, and that is why
they are asserted separately. This file is **wholly ours**: no foreign content
ever shares it, so the round trip is not a merge but a creation and a removal,
and "byte for byte" means the directory is back to not having the file at all.
What replaces the merge as the thing that can go wrong is *identity*: a file
already sitting at our path that we did not write must be refused, not deleted.

The second property is that **nothing here is hard-coded** — not the user, not
their home, not `CODEX_HOME`, not the executable, not the `PATH` and not the
Codex version. #83's scope says so in those words, and #38 is what it looks like
when a rendered artifact names a path that was true only on the machine that
rendered it. Since #272 the executable and the `PATH` come from the environment
the reconcile was given, which is the user's own login `PATH`.

The third is the asymmetry, which is this item's whole reason for having its own
rule: it asks launchd to **load** the job, and never to unload it. The product
starts a server the user's TUIs will join; it never stops one they are attached
to. Every `bootout` assertion below is an assertion that a command was *not* run.
It is also #272's migration: a machine that ran #82's job gets the same label
re-rendered and nothing stopped.

**No test here may reach the real launchd.** `Launchd` is passed in everywhere and
has no default, because the first draft of this module defaulted it and a test run
loaded a real job into the author's own login session, naming a plist that pytest
deleted a second later.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.installation import (
    BootstrappedRender,
    State,
    codex_runtime,
    read_bootstrapped_render,
    write_bootstrapped_render,
)
from gpt_voicecoding.installation import codex_launch_agent as agent
from launchd_fake import DOMAIN, FakeLaunchd, codex, codex_home, no_codex


def launch_agents(root: Path) -> Path:
    directory = root / "Library" / "LaunchAgents"
    directory.mkdir(parents=True)
    return directory


def log_in(root: Path) -> Path:
    return root / "Application Support" / "GPT-VoiceCoding" / "codex-daemon.log"


def record_in(root: Path) -> Path:
    return root / "Application Support" / "GPT-VoiceCoding" / "installation.json"


def written(directory: Path) -> dict:
    return plistlib.loads(agent.plist_path(directory).read_bytes())


# -- what the job says ---------------------------------------------------


def test_the_job_runs_the_users_own_codex_as_a_plain_server(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#272 replaced #82's command outright, and this is the whole of it.

    The user's own resolved codex, `app-server --listen`, and the derived
    control socket. No `daemon` subcommand: that family is what tied this job to
    a managed standalone tree the user never updates, and the seam is the socket.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert written(directory)["ProgramArguments"] == [
        str(found.runtime.executable),
        "app-server",
        "--listen",
        f"unix://{found.runtime.control_socket}",
    ]


def test_the_socket_is_derived_from_the_codex_home_and_never_asked_for(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Upstream fixes this path; nothing here asks a running process for it.

    That is what lets `daemon version` go: the one thing it was still being run
    for was a `socketPath` this side can work out, for a 139 ms subprocess.
    """
    directory = launch_agents(tmp_path)
    found = codex(tmp_path, home=codex_home(tmp_path / "elsewhere"))
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    listened = written(directory)["ProgramArguments"][-1]
    assert listened == (
        f"unix://{tmp_path / 'elsewhere' / '.codex'}/app-server-control/app-server-control.sock"
    )


def test_the_job_carries_a_path_that_can_find_the_executables_interpreter(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The finding #271 did not anticipate, and found by failing.

    launchd's whole `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`. An npm codex is
    `codex.js` behind `#!/usr/bin/env node`, and under that `PATH` it died with
    `env: node: No such file or directory` and never held the socket. So the job
    carries the `PATH` this reconcile was given — on which the executable was
    found, which is where nvm and npm also put `node`.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    carried = written(directory)["EnvironmentVariables"]["PATH"]
    assert carried == found.runtime.path
    assert str(found.runtime.executable.parent) in carried.split(":")


def test_the_job_starts_at_login_and_is_never_kept_alive(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#83's scope: a one-shot idempotent daemon start, and no supervisor.

    `KeepAlive` would make this a polling supervisor by another name — launchd
    restarting `daemon start` forever the moment it exits, which it does at once
    because starting the daemon is all it is for. Legacy's job had it
    (`legacy@1d32845:scripts/launch-agent.py:64`) and legacy's job was a
    supervised daemon; this one is dropped on the way across.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["RunAtLoad"] is True
    assert "KeepAlive" not in job


def test_the_job_raises_both_open_file_limits_to_the_ruled_value(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#129: launchd's soft default of 256 failed after two days at 271 fds.

    The daemon leaks descriptors in the managed Codex binary, outside this
    repository.  A high launch limit keeps Sessions usable while that defect is
    reported upstream; both limits deliberately carry the same ruled value.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["SoftResourceLimits"] == {"NumberOfFiles": 65_536}
    assert job["HardResourceLimits"] == {"NumberOfFiles": 65_536}


def test_the_job_carries_the_codex_home_it_was_resolved_from(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """launchd hands a job none of the user's shell environment.

    Without this, a user whose `CODEX_HOME` is not the default gets a server on
    one home and TUIs on another, and an empty roster nothing explains.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert written(directory)["EnvironmentVariables"]["CODEX_HOME"] == str(found.runtime.codex_home)


def test_the_job_names_no_version(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """The updater this job follows is the user's own package manager — #272.

    #82 got version-independence from the `current` symlink Codex's own updater
    moved, and that symlink was the thing that went stale: on the reference
    machine it still pointed at 0.149.1 while the user's terminal ran 0.153.4.
    Now the job names the path `which` answered, which npm rewrites in place on
    an upgrade, and nothing rendered here carries a version at all.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    rendered = agent.plist_path(directory).read_text(encoding="utf-8")
    assert "standalone" not in rendered
    assert not re.search(r"\d+\.\d+\.\d+", rendered), rendered


def test_the_job_logs_where_the_engine_does_not(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """ADR 0004: rotation is rename-and-reopen, and launchd cannot be told to
    reopen anything — so this descriptor must never be on the engine's log."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["StandardOutPath"] == job["StandardErrorPath"] == str(log)
    assert log.parent.is_dir(), "launchd will not spawn a job whose log directory is missing"


# -- resolving from the environment --------------------------------------


def test_the_launch_agents_directory_is_the_users_own(tmp_path: Path) -> None:
    assert agent.default_launch_agents_directory(tmp_path) == tmp_path / "Library" / "LaunchAgents"


def test_the_label_is_not_the_gen_one_job(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """`com.gpt-voicecoding.bridge` is still in real `~/Library/LaunchAgents`
    directories and is #54's to dispose of. A collision would have this install
    silently replace a supervised job it never wrote."""
    directory = launch_agents(tmp_path)
    (directory / "com.gpt-voicecoding.bridge.plist").write_bytes(
        plistlib.dumps({"Label": "com.gpt-voicecoding.bridge", "KeepAlive": True})
    )
    agent.install(
        directory, codex(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert agent.LABEL != "com.gpt-voicecoding.bridge"
    assert plistlib.loads((directory / "com.gpt-voicecoding.bridge.plist").read_bytes()) == {
        "Label": "com.gpt-voicecoding.bridge",
        "KeepAlive": True,
    }


# -- asking launchd, and the one thing it is never asked -----------------


def test_install_loads_the_job_now_rather_than_at_the_next_login(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The whole reason this item carries a process action at all.

    A `.app` dragged in has no install step (ADR 0012), so without this a user
    who opens `codex` on install day gets no shared daemon and no bridged Codex
    Session until they next log out.
    """
    directory = launch_agents(tmp_path)
    plist = agent.plist_path(directory)

    outcome = agent.install(
        directory, codex(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.CURRENT, True)
    assert ["print", "bootstrap", "print"] == launchd.verbs
    assert launchd.commands[1] == ["/bin/launchctl", "bootstrap", DOMAIN, str(plist)]


def test_an_already_loaded_job_is_not_bootstrapped_again(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """`bootstrap` on a loaded job fails, and that failure is not one: the state
    it was reaching for is the state that is already there."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    again = agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    assert (again.state, again.changed, again.ok) == (State.CURRENT, False, True)
    assert "bootstrap" not in launchd.verbs


def test_a_job_that_died_is_loaded_again_by_the_next_reconcile(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Which is repair at an event, not a supervisor on a timer (#83's scope)."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    launchd.program = None  # the job died
    launchd.commands.clear()

    repaired = agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    assert (repaired.ok, repaired.state, repaired.changed) == (True, State.CURRENT, True)
    assert "bootstrap" in launchd.verbs


def test_launchd_refusing_to_load_the_job_is_reported_in_its_own_words(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    launchd.refuses = True
    outcome = agent.install(
        launch_agents(tmp_path),
        codex(tmp_path),
        log_in(tmp_path),
        record_in(tmp_path),
        launchd.launchd,
    )

    assert outcome.ok is False
    assert agent.LABEL in outcome.note
    assert "Input/output error" in outcome.note


def test_a_changed_render_is_written_and_the_running_job_is_left_alone(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The asymmetry, at its sharpest.

    Reloading means `bootout`, and by now the user's own `codex` TUIs are thin
    clients of the daemon this job started. The new render is for the next login;
    what the user has running is the job that was right for them.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    moved = codex(tmp_path, home=codex_home(tmp_path / "elsewhere"))
    launchd.commands.clear()

    rewritten = agent.install(directory, moved, log, record_in(tmp_path), launchd.launchd)

    assert (rewritten.ok, rewritten.changed) == (True, True)
    assert "next login" in rewritten.note
    assert "bootstrap" not in launchd.verbs
    assert written(directory)["ProgramArguments"][0] == str(moved.runtime.executable)


def test_uninstall_never_asks_launchd_for_anything(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """No `bootout`, ever. It would stop the daemon live Sessions are attached
    to, which is what #83 forbids in the words "without stopping user Sessions"."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    removed = agent.uninstall(directory)

    assert (removed.ok, removed.state, removed.changed) == (True, State.ABSENT, True)
    assert launchd.commands == []
    assert launchd.held is True, "the daemon the user's Sessions are on kept running"


# -- the round trip ------------------------------------------------------


def test_uninstall_takes_the_file_back_out(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)

    assert (
        agent.install(
            directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd
        ).changed
        is True
    )
    assert agent.plist_path(directory).exists()

    agent.uninstall(directory)
    assert list(directory.iterdir()) == []


def test_reinstalling_the_same_loaded_render_is_current(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Recreating a removed plist is not a render change when its SHA is loaded."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    agent.uninstall(directory)

    restored = agent.install(directory, found, log, record, launchd.launchd)

    assert (restored.state, restored.changed, restored.ok) == (State.CURRENT, True, True)


def test_uninstall_with_nothing_there_is_not_a_failure(tmp_path: Path) -> None:
    outcome = agent.uninstall(launch_agents(tmp_path))
    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.ABSENT, False)


def test_a_moved_codex_home_is_stale(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    moved = codex(tmp_path, home=codex_home(tmp_path / "elsewhere"))
    assert (
        agent.inspect(directory, moved, log, record_in(tmp_path), launchd.launchd).state
        is State.STALE
    )


def test_a_current_plist_with_no_job_loaded_is_stale(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """Both facts are reported, and only one of them is `state`. A plist that is
    right with no job holding it is a machine that will be right at next login
    and is not right now — which is the thing a status run has to be able to say.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    launchd.program = None  # the job died

    standing = agent.inspect(directory, found, log, record_in(tmp_path), launchd.launchd)
    assert standing.state is State.STALE
    assert "is current" in standing.note and "not loaded" in standing.note


def test_inspect_writes_nothing(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = launch_agents(tmp_path)
    standing = agent.inspect(
        directory, codex(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (standing.ok, standing.state) == (True, State.ABSENT)
    assert list(directory.iterdir()) == []
    assert "bootstrap" not in launchd.verbs


# -- refusals ------------------------------------------------------------


def test_no_codex_on_the_machine_is_not_a_failure(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """The Codex lane reports itself absent, with the reason — #272's first edge case.

    Nothing went wrong; there is simply nothing this job could start, because
    the user has no codex. Same answer the Claude item gives a user with no
    Claude config directory. No exception, and no empty-string executable
    rendered into a plist to fail at the next login instead.
    """
    directory, missing = launch_agents(tmp_path), no_codex(tmp_path)
    outcome = agent.install(
        directory, missing, log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.ABSENT, False)
    assert not agent.plist_path(directory).exists()
    assert "there is no codex" in outcome.note
    assert launchd.commands == []


def test_a_codex_without_the_executable_bit_is_no_codex(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A file named `codex` that cannot be run is not a codex — #272's second edge case.

    `command -v` in a login shell would answer a shell function or an alias by
    *name*, which is the shape #82 met on this product's author's machine.
    Resolving over a `PATH` instead cannot: `which` only ever answers a file it
    found, and only one with the executable bit. The case is dissolved rather
    than guarded, and this pins the half of it that is still reachable.
    """
    directory = launch_agents(tmp_path)
    bin_directory = tmp_path / "unrunnable"
    bin_directory.mkdir()
    (bin_directory / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_directory / "codex").chmod(0o644)

    found = codex_runtime.resolve({"PATH": str(bin_directory), "CODEX_HOME": str(tmp_path)})

    assert found.runtime is None
    assert "there is no codex" in found.reason
    outcome = agent.install(
        directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )
    assert (outcome.ok, outcome.state) == (True, State.ABSENT)


def test_a_file_at_our_path_that_is_not_ours_is_refused_untouched(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The one way this file can carry somebody else's content.

    It cannot be merged — a launchd job is not a document with room for two — so
    the only honest answers are refuse and say so. Deleting it on an uninstall
    would take away a job this product never wrote.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    theirs = plistlib.dumps({"Label": "com.somebody.else", "ProgramArguments": ["/bin/true"]})
    agent.plist_path(directory).write_bytes(theirs)

    refused = agent.install(
        directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )
    assert refused.ok is False
    assert "com.somebody.else" in refused.note
    assert agent.plist_path(directory).read_bytes() == theirs

    assert agent.uninstall(directory).ok is False
    assert agent.plist_path(directory).read_bytes() == theirs


def test_a_plist_that_will_not_parse_is_refused_untouched(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    agent.plist_path(directory).write_text("<plist> and then nothing", encoding="utf-8")

    assert (
        agent.install(directory, found, log_in(tmp_path), record_in(tmp_path), launchd.launchd).ok
        is False
    )
    assert agent.plist_path(directory).read_text(encoding="utf-8") == "<plist> and then nothing"
    assert agent.uninstall(directory).ok is False


def test_a_launch_agents_directory_that_is_not_there_is_created(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Unlike Claude's config directory, this one is macOS's and is ours to make:
    a user who has never installed a login item simply has no such directory."""
    directory = tmp_path / "Library" / "LaunchAgents"
    outcome = agent.install(
        directory, codex(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.CURRENT, True)
    assert agent.plist_path(directory).exists()


def test_a_launch_agents_directory_that_cannot_be_written_is_reported(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#83's installation-side failure mode, in the boundary's own vocabulary."""
    directory = launch_agents(tmp_path)
    directory.chmod(0o500)
    try:
        outcome = agent.install(
            directory, codex(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
        )
    finally:
        directory.chmod(0o700)

    assert outcome.ok is False
    assert str(directory) in outcome.note
    assert "bootstrap" not in launchd.verbs


def test_the_command_timeout_fits_inside_the_shell_ceiling_it_is_derived_from() -> None:
    """The number is derived, and this is what holds the derivation together.

    `Installation.deadline` is the only measured ceiling in this picture: the
    shell kills a reconcile that outlives it, and this item's subprocesses run
    inside that. The value lives in Swift and is used in Python, which is exactly
    the shape #47 records — a constant spelled in two languages with no test
    between them. So this reads the Swift one rather than trusting a comment.
    """
    swift = (
        Path(__file__).resolve().parents[1] / "shell/Sources/ShellCore/Installation.swift"
    ).read_text(encoding="utf-8")
    stated = re.search(r"deadline:\s*TimeInterval\s*=\s*([0-9.]+)", swift)
    assert stated, "the shell no longer states a reconcile deadline this can be derived from"

    assert float(stated.group(1)) == agent.SHELL_RECONCILE_DEADLINE_SECONDS
    assert agent.COMMAND_TIMEOUT_SECONDS * agent.COMMANDS_PER_RUN <= float(stated.group(1))


# -- migrating a machine that ran #82's job ------------------------------


#: The job #82 and #83 wrote, exactly as it stands in a real
#: `~/Library/LaunchAgents` on a machine that has been running this product:
#: the managed standalone binary under the symlink Codex's own updater moved,
#: and `app-server daemon start`. Spelled out here rather than rendered by the
#: old code, because the old code is gone and a migration test that built its
#: own "before" out of the "after" would prove nothing.
PREVIOUS_MANAGED_PARTS = ("packages", "standalone", "current", "codex")
PREVIOUS_DAEMON_ARGUMENTS = ("app-server", "daemon", "start")


def as_82_left_it(directory: Path, codex_home: Path, log_path: Path) -> str:
    """Write #82's plist where launchd finds it, and answer its program."""
    binary = codex_home.joinpath(*PREVIOUS_MANAGED_PARTS)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    document = {
        "Label": agent.LABEL,
        "ProgramArguments": [str(binary), *PREVIOUS_DAEMON_ARGUMENTS],
        "RunAtLoad": True,
        "EnvironmentVariables": {"CODEX_HOME": str(codex_home)},
        "SoftResourceLimits": {"NumberOfFiles": agent.OPEN_FILE_LIMIT},
        "HardResourceLimits": {"NumberOfFiles": agent.OPEN_FILE_LIMIT},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    agent.plist_path(directory).write_bytes(plistlib.dumps(document, sort_keys=True))
    return str(binary)


def test_a_machine_that_ran_the_previous_job_gets_the_same_label_re_rendered(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """One job, not two — the migration's whole load-bearing property.

    Two `RunAtLoad` plists would race for one control socket, and that is
    precisely the state #271's prototype left the author's machine in, which is
    why it was restored before this landed. The label does not change, so the
    reconcile rewrites the job that is there rather than standing a second one
    beside it.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    previous = as_82_left_it(directory, found.runtime.codex_home, log)
    launchd.program = previous  # the server the user's TUIs are attached to

    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    assert sorted(path.name for path in directory.iterdir()) == [f"{agent.LABEL}.plist"]
    assert written(directory)["ProgramArguments"] == [
        str(found.runtime.executable),
        *found.runtime.server_arguments,
    ]


def test_the_migration_does_not_stop_the_server_the_user_is_attached_to(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#83's rule, and the triage ruling on #272 restating it for this case.

    By now the user's own `codex` TUIs are thin clients of the standalone server
    #82's job started. Booting it out to load the new render would end every one
    of their Sessions, so the render is written and left for the next login.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    previous = as_82_left_it(directory, found.runtime.codex_home, log)
    launchd.program = previous
    launchd.commands.clear()

    outcome = agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    assert "bootout" not in launchd.verbs
    assert "bootstrap" not in launchd.verbs
    assert launchd.program == previous, "the running standalone server was displaced"
    assert outcome.ok and outcome.changed
    assert outcome.state is State.STALE
    assert "next login" in outcome.note


def test_the_migration_leaves_codexs_own_standalone_directory_alone(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """It is codex's directory, not this product's (triage ruling on #272).

    `~/.local/bin/codex` pointing into it is the user's shell to sort out, and
    the resolution rule already yields the codex they actually get.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    as_82_left_it(directory, found.runtime.codex_home, log)
    standalone = found.runtime.codex_home.joinpath(*PREVIOUS_MANAGED_PARTS)
    launchd.program = str(standalone)

    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)

    assert standalone.exists(), "the migration deleted a tree codex owns"


def test_the_next_login_makes_the_migrated_job_current(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The other half of "applies at the next login": that it actually does.

    A migration that only ever reported `stale` would be one nobody could tell
    from a broken one, so this walks the login through and reads the answer.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log, record = log_in(tmp_path), record_in(tmp_path)
    launchd.program = as_82_left_it(directory, found.runtime.codex_home, log)
    agent.install(directory, found, log, record, launchd.launchd)

    launchd.begin_login(agent.plist_path(directory))
    agent.install(directory, found, log, record, launchd.launchd)
    standing = agent.inspect(directory, found, log, record, launchd.launchd)

    assert launchd.program == str(found.runtime.executable)
    assert standing.state is State.CURRENT


def test_a_loaded_job_still_running_the_previous_render_is_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The thing `state` would otherwise lie about.

    Nothing here reloads a job, so after a render changes there is a window —
    until the next login — in which the file on disk is right and what launchd is
    actually running is the render before it. A status run that read our own file
    back would call that `current`. Asking launchd what it holds is what makes
    the difference visible.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    # A machine whose codex moved: `nvm use`, a reinstall, a Homebrew codex
    # arriving ahead of an npm one. The rendered program changes; the job launchd
    # holds still runs the one the user's own TUIs joined.
    upgraded = codex(tmp_path / "after-upgrade", home=codex_home(tmp_path))
    agent.install(directory, upgraded, log, record_in(tmp_path), launchd.launchd)

    standing = agent.inspect(directory, upgraded, log, record_in(tmp_path), launchd.launchd)

    assert standing.state is State.STALE
    assert str(found.runtime.executable) in standing.note, "it does not say what is running"
    assert "next login" in standing.note
    assert launchd.program == str(found.runtime.executable), "the job was reloaded"


def test_a_loaded_job_with_the_same_program_and_previous_render_is_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#132: the program path cannot identify the whole loaded render.

    #129 changed only the resource limits, so launchd kept the same program while
    holding the old definition.  A status run must not call that loaded job current.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, found, previous_log, record_in(tmp_path), launchd.launchd)
    agent.install(directory, found, current_log, record_in(tmp_path), launchd.launchd)

    standing = agent.inspect(directory, found, current_log, record_in(tmp_path), launchd.launchd)

    assert standing.state is State.STALE
    assert "loaded job is a previous render; applies at the next login" in standing.note
    assert launchd.program == str(found.runtime.executable), "the job was reloaded"


def test_a_second_reconcile_in_the_same_login_keeps_the_previous_render_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A byte-identical reconcile is not evidence that launchd reloaded the file."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, found, previous_log, record, launchd.launchd)
    agent.install(directory, found, current_log, record, launchd.launchd)
    recorded_stale = record.read_bytes()
    launchd.commands.clear()

    again = agent.install(directory, found, current_log, record, launchd.launchd)

    assert again.state is State.STALE
    assert "previous render" in again.note
    assert record.read_bytes() == recorded_stale
    assert "bootstrap" not in launchd.verbs


def test_a_new_login_makes_the_render_it_loaded_current(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """ASID change is the fake equivalent of logout/login in #132's acceptance."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, found, previous_log, record, launchd.launchd)
    agent.install(directory, found, current_log, record, launchd.launchd)
    launchd.begin_login(agent.plist_path(directory))

    reconciled = agent.install(directory, found, current_log, record, launchd.launchd)
    standing = agent.inspect(directory, found, current_log, record, launchd.launchd)

    assert reconciled.state is State.CURRENT
    assert standing.state is State.CURRENT


def test_a_new_login_records_the_disk_render_before_reconcile_changes_it(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The login loaded the old disk bytes, not the new build's later render."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, found, previous_log, record, launchd.launchd)
    launchd.begin_login(agent.plist_path(directory))

    reconciled = agent.install(directory, found, current_log, record, launchd.launchd)
    standing = agent.inspect(directory, found, current_log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert standing.state is State.STALE
    assert "previous render" in standing.note


def test_a_missing_loaded_render_record_fails_closed_without_status_writing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """An upgraded install cannot guess which same-program render launchd holds."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    record.unlink()

    standing = agent.inspect(directory, found, log, record, launchd.launchd)

    assert standing.state is State.STALE
    assert "unknown render" in standing.note
    assert not record.exists(), "status wrote the missing installation record"


def test_install_records_an_unknown_render_when_the_loaded_record_is_missing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    record.unlink()

    reconciled = agent.install(directory, found, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)

    assert reconciled.state is State.STALE
    assert "unknown render" in reconciled.note
    assert loaded is not None
    assert loaded.render_sha256 is None
    assert loaded.login_asid == launchd.login_asid


def test_a_new_login_with_no_plist_records_an_unknown_loaded_render(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A loaded job plus an absent file never supplies bytes the login loaded."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    agent.plist_path(directory).unlink()
    launchd.login_asid += 1

    reconciled = agent.install(directory, found, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)
    standing = agent.inspect(directory, found, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "unknown render" in reconciled.note
    assert loaded is not None and loaded.render_sha256 is None
    assert loaded.login_asid == launchd.login_asid
    assert standing.state is State.STALE
    assert "unknown render" in standing.note


def test_a_launchd_that_does_not_say_what_it_loaded_fails_closed(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A `print` whose shape this does not recognise is reported as unknown.

    The alternative is a status run that calls a job current on the strength of a
    line it could not find, which is the failure this whole reading exists to
    avoid.
    """
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, found, log, record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    silent = agent.Launchd(domain=DOMAIN, run=lambda _: (0, "a shape this does not recognise"))
    standing = agent.inspect(directory, found, log, record_in(tmp_path), silent)

    assert standing.state is State.STALE
    assert "did not say what it runs" in standing.note


def test_a_loaded_render_record_without_an_asid_fails_closed(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)
    assert loaded is not None
    write_bootstrapped_render(
        record, BootstrappedRender(render_sha256=loaded.render_sha256, login_asid=None)
    )

    standing = agent.inspect(directory, found, log, record, launchd.launchd)

    assert standing.state is State.STALE
    assert "which login" in standing.note


def test_reconcile_keeps_a_foreign_loaded_program_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    launchd.program = "/foreign/codex"

    reconciled = agent.install(directory, found, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "/foreign/codex" in reconciled.note


def test_reconcile_keeps_a_foreign_loaded_program_stale_when_plist_is_missing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The wanted program is still authoritative when reconcile recreates the plist."""
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, found, log, record, launchd.launchd)
    agent.plist_path(directory).unlink()
    launchd.program = "/foreign/codex"

    reconciled = agent.install(directory, found, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "/foreign/codex" in reconciled.note


def test_bootstrap_without_a_login_asid_records_an_unknown_render(tmp_path: Path) -> None:
    directory, found = launch_agents(tmp_path), codex(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    binary = str(found.runtime.executable)
    print_count = 0

    def answer(arguments: Sequence[str]) -> tuple[int, str]:
        nonlocal print_count
        if arguments[1] == "bootstrap":
            return (0, "")
        print_count += 1
        if print_count == 1:
            return (113, "Could not find service")
        return (0, f"program = {binary}")

    launchd = agent.Launchd(domain=DOMAIN, run=answer)

    outcome = agent.install(directory, found, log, record, launchd)
    loaded = read_bootstrapped_render(record)

    assert outcome.state is State.STALE
    assert loaded is not None and loaded.render_sha256 is None
    assert loaded.login_asid is None


#: One real `launchctl print` answer, captured on 2026-08-26 from the job this
#: item installed. The parse below is the one thing in this module that depends
#: on launchd's output shape, so it is held against a real one rather than only
#: against the fake that was written from it.
#:
#: **It is #82's job, and that is deliberate.** #272 changed the program and the
#: arguments and left launchd's *output shape* exactly as it was — which is the
#: only thing this fixture is evidence of. Re-capturing it against the new job
#: would replace real output with real output and prove nothing more; inventing
#: the new block rather than capturing it would replace it with something worse.
#: So it stays as it was measured, and what it is measured against is said out
#: loud. #271's prototype did read a live `print` of the new job (`state =
#: running, active count = 1`, `docs/codex-runtime-flow.md` step 2), but its
#: `arguments` block is elided in that document, so it is not a fixture.
REAL_PRINT = """gui/501/com.gpt-voicecoding.codex-daemon = {
\tactive count = 0
\tpath = /Users/simon/Library/LaunchAgents/com.gpt-voicecoding.codex-daemon.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /Users/simon/.codex/packages/standalone/current/codex
\targuments = {
\t\t/Users/simon/.codex/packages/standalone/current/codex
\t\tapp-server
\t\tdaemon
\t\tstart

\tasid = 100016
"""


def test_the_program_is_read_out_of_a_real_launchctl_answer() -> None:
    held = agent.Launchd(domain=DOMAIN, run=lambda _: (0, REAL_PRINT)).held_job()
    assert held is not None
    assert held.program == "/Users/simon/.codex/packages/standalone/current/codex"
    assert held.login_asid == 100_016
