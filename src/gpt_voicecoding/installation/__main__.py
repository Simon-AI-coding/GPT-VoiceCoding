"""``bridge-install`` — the composition root for installation, and its whole CLI.

A ``.app`` dragged into ``/Applications`` has no install step: macOS copies a
directory and runs nothing. So first launch *is* the install, and the menu-bar
shell runs ``bridge-install reconcile`` before it spawns the engine (ADR 0012).
Reconcile is the only verb the shell uses, and it writes nothing when the machine
already agrees with what this build would put there.

**It does not go through the engine, on purpose.** The engine refuses to start
without a ``config.toml`` the user writes by hand, so an installation that waited
for the engine would not have happened by the time the user first opens the app.
Nothing here dials a socket, reads engine state, or asks Bridge Core anything.

**The items are named here, one line each.** That is what stands in for a
registry: with two artifacts and no plugin story, a list of two calls *is* the
boundary, and adding a third is adding a line. Both of v1.0's items are on it —
ADR 0011's Claude hook block (#86) and the Codex login ``LaunchAgent`` (#83).

**Where things go is resolved once, here, and passed down.** Each item knows how
to find its own default, and nothing below this file reads ``os.environ`` or
``Path.home``: an item that resolved its own paths could not be run against a
temporary directory, and a boundary whose tests cannot avoid the real
``~/Library/LaunchAgents`` is one whose tests load real login jobs.

**The interpreter is ``sys.executable`` and is never written down.** Inside the
bundle that is the bundled python beside this console script, which is the whole
point: a hook command naming a developer checkout's interpreter is #38, and the
only way not to have that bug is never to name an interpreter at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gpt_voicecoding.installation import (
    Intent,
    Outcome,
    State,
    claude_hooks,
    codex_launch_agent,
    codex_runtime,
    read_intent,
    write_intent,
)
from gpt_voicecoding.locations import codex_daemon_log_path, installation_path

#: Everything went as asked.
EXIT_OK = 0
#: At least one item could not be put where it belongs, and said why.
EXIT_FAILED = 1

VERBS = ("reconcile", "install", "uninstall", "status")

#: How the intent file is named in a report. It is not an installed artifact —
#: it lives in this product's own directory — but a run that cannot write it has
#: to say so, because the next launch reads the answer it failed to change.
RECORD_NAME = "installation-record"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where this run puts things, resolved once from the environment it runs in.

    Nothing in here is a decision — every field is an answer some item already
    knows how to work out for itself. It exists so the answers are worked out in
    one place, at the top, where a test can supply a different environment
    instead of the machine's own.
    """

    claude_config_directory: Path
    interpreter: Path
    launch_agents_directory: Path
    #: This machine's one codex, or the reason there is none. Resolved here,
    #: once, from the ``PATH`` this process was given — which is the user's own
    #: login ``PATH``, because the shell hands the installation subprocess the
    #: environment it built for the engine (#272, ADR 0022).
    codex: codex_runtime.Resolution
    codex_log_path: Path
    installation_record_path: Path
    launchd: codex_launch_agent.Launchd


def _resolve(
    environ: Mapping[str, str],
    interpreter: Path,
    base_dir: Path | None,
    home: Path | None,
    launchd: codex_launch_agent.Launchd | None,
) -> Placement:
    return Placement(
        claude_config_directory=claude_hooks.default_config_directory(environ, home),
        interpreter=interpreter,
        launch_agents_directory=codex_launch_agent.default_launch_agents_directory(home),
        codex=codex_runtime.resolve(environ, home),
        codex_log_path=codex_daemon_log_path(base_dir),
        installation_record_path=installation_path(base_dir),
        launchd=launchd or codex_launch_agent.default_launchd(),
    )


def _inspect_all(where: Placement) -> list[Outcome]:
    return [
        claude_hooks.inspect(where.claude_config_directory, where.interpreter),
        codex_launch_agent.inspect(
            where.launch_agents_directory,
            where.codex,
            where.codex_log_path,
            where.installation_record_path,
            where.launchd,
        ),
    ]


def _install_all(where: Placement) -> list[Outcome]:
    return [
        claude_hooks.install(where.claude_config_directory, where.interpreter),
        codex_launch_agent.install(
            where.launch_agents_directory,
            where.codex,
            where.codex_log_path,
            where.installation_record_path,
            where.launchd,
        ),
    ]


def _uninstall_all(where: Placement) -> list[Outcome]:
    return [
        claude_hooks.uninstall(where.claude_config_directory),
        codex_launch_agent.uninstall(where.launch_agents_directory),
    ]


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bridge-install",
        description=(
            "Put this product's artifacts where the coding agents will find them, "
            "take them back, or say what is there now."
        ),
        epilog=(
            "reconcile  install what is missing or out of date, unless uninstalled\n"
            "install    install, and record that this machine wants it\n"
            "uninstall  take back exactly what was written, and record that\n"
            "status     say what is on this machine, and write nothing"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("verb", choices=VERBS)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    interpreter: Path | None = None,
    home: Path | None = None,
    launchd: codex_launch_agent.Launchd | None = None,
) -> int:
    """Entry point for the ``bridge-install`` console script.

    The keyword arguments exist so a test can run the whole verb without
    installing this product on the machine running the test. ``home`` and
    ``launchd`` are the two that matter most: the Codex item's directory is
    macOS's own and has no variable that moves it, and its ``launchctl`` would
    otherwise be the real one, loading a real login job into a real session.
    """
    verb = parse(argv).verb
    environ = os.environ if environ is None else environ
    interpreter = Path(sys.executable) if interpreter is None else interpreter
    where = _resolve(environ, interpreter, base_dir, home, launchd)

    if verb == "status":
        outcomes = _inspect_all(where)
        print(_intent_line(read_intent(base_dir)))
        print(_server_line(where))
    elif verb == "uninstall":
        outcomes = _uninstall_all(where)
        # Only when it really came back out. `wanted: false` is what stops every
        # later reconcile from touching this machine, so recording it over an
        # uninstall that failed would leave our hooks in the user's settings file
        # with nothing left that would ever repair or remove them.
        if all(outcome.ok for outcome in outcomes):
            outcomes = _with_intent(outcomes, wanted=False, base_dir=base_dir)
    elif verb == "install":
        # Recorded whether or not the artifacts landed, and that asymmetry is the
        # honest one: this file holds what the user *wants*, and a failed install
        # is a want that the next reconcile should retry rather than forget.
        outcomes = _install_all(where)
        outcomes = _with_intent(outcomes, wanted=True, base_dir=base_dir)
    else:
        intent = read_intent(base_dir)
        if not intent.install_wanted:
            print("uninstalled on this machine — reconcile leaves it alone")
            return EXIT_OK
        outcomes = _install_all(where)
        if intent.first_run and all(outcome.ok for outcome in outcomes):
            outcomes = _with_intent(outcomes, wanted=True, base_dir=base_dir)

    for outcome in outcomes:
        print(outcome.line(), file=sys.stdout if outcome.ok else sys.stderr)
    return EXIT_OK if all(outcome.ok for outcome in outcomes) else EXIT_FAILED


def _with_intent(outcomes: list[Outcome], *, wanted: bool, base_dir: Path | None) -> list[Outcome]:
    """Record the answer, and report a record that could not be written.

    A failed record is not a failed install: the artifacts are where they belong.
    It is reported anyway, because the next launch would read the old answer.
    """
    failure = write_intent(wanted, base_dir)
    if not failure:
        return outcomes
    return [*outcomes, Outcome(RECORD_NAME, State.ABSENT, ok=False, note=failure)]


def _server_line(where: Placement) -> str:
    """What the shared Codex app-server says about itself, for `status` only.

    Never on the install path. This dials a control socket that is absent
    whenever the server is not running, and a reconcile runs before the engine
    at every launch with no business paying for it. A person typing `status` is
    asking exactly this question.

    **It is a handshake now, not a subprocess** (#272). `codex app-server daemon
    version` is gone with the rest of the `daemon` subcommands; what replaced it
    is one connect-and-`initialize` on the derived control socket, measured at
    1 ms against a live server where the subprocess cost 139 ms. It says nothing
    about versions, and that is the point rather than an omission: with one
    codex on the machine there is no second version for the first to disagree
    with, so #67's no-pin ruling is dissolved rather than reopened.
    """
    if where.codex.runtime is None:
        return f"{codex_launch_agent.NAME}: {where.codex.reason}, so no app-server to ask"
    return (
        f"{codex_launch_agent.NAME}: {codex_runtime.answering(where.codex.runtime.control_socket)}"
    )


def _intent_line(intent: Intent) -> str:
    if intent.wanted is None:
        return f"{RECORD_NAME}: nothing recorded — the next reconcile installs"
    return f"{RECORD_NAME}: wanted={str(intent.wanted).lower()}"


if __name__ == "__main__":  # pragma: no cover - the console script calls `main`
    raise SystemExit(main())
