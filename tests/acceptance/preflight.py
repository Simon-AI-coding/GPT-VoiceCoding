"""The two run-level checks: the machine is arranged, and the backend answers (#351).

**Responsibilities held here:** §5's refusals, §2 item 0b's probe, and the two
facts §7's verdict carries about the machine the run happened on — the bundle
under test and the agent versions seen. The first two are run-level, run once
before any lane starts, are reads, and start no engine — which is the whole of
"the run can start or refuse". The third is here because `Machine` **is** the
machine: the bundle path was already one of its fields, and a second module
reading the same PATH for the same binaries would be a second answer about them.

`docs/acceptance-design.md` §9 parks the refusals in `conftest.py`. They are here
for #350's reason and no other: a conftest is loaded by pytest **by path** and is
not an importable name (`tests/test_layout.py`), so a refusal kept there cannot be
driven by a fast test — and this ticket's first acceptance criterion is that
every one of them is. `conftest.py` keeps the fixture; the reading is here.

Two rules hold this module together:

* **Every check is a read, and a refusal is a sentence.** A check answers `None`
  when the machine is arranged and the reason when it is not. Nothing here fixes
  anything, starts anything, or types a turn — refusing is cheap precisely
  because it never does.
* **The machine is a value.** Everything a check consults arrives on `Machine`,
  values for what is on disk and callables for what has to be shelled out or
  dialled, so a fast test hands preflight a machine that is wrong in exactly one
  way and reads the sentence back. `Machine.real()` is the only place the real
  ones are wired, and no check reaches past its argument.

**Credentials never reach an artifact** (§8). A bot token is read from the
environment by the variable the operator's own config names, and what is
journalled is the variable's *name* and the bot's public identity — never the
token. The user account's `api_id`, `api_hash` and session file are
`telegram_person`'s and are journalled nowhere at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import deadlines
import items
import journey
import support
import telegram_person

from gpt_voicecoding.adapters.agent.codex import processes as codex_processes
from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramError, http_transport
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    DEFAULT_API_ROOT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from gpt_voicecoding.config import default_socket_path
from gpt_voicecoding.control_plane.ownership import is_connectable
from gpt_voicecoding.control_plane.server import CLAIM_PROBE_SECONDS
from gpt_voicecoding.installation import claude_hooks, codex_runtime

# --- what a refusal is -------------------------------------------------------


class Refused(Exception):
    """The machine is not one this run can be attributed to (§5).

    Carried rather than returned, because a refusal ends the run: `conftest`
    turns it into `Verdict.refuse`, a non-zero exit and five SKIPPED rows per
    lane, and nothing after it starts an engine.
    """

    def __init__(self, check: str, reason: str) -> None:
        super().__init__(reason)
        self.check = check
        self.reason = reason


# --- the places this run's own arrangement lives -----------------------------

#: The maintainer's realtime probe, and the variable that moves it. The script
#: stays in the sibling legacy checkout: §2 says the probe is the one that was
#: actually run against the backend, and a copy in this tree would be a fork of
#: the only thing that gives it its value.
REALTIME_PROBE_VARIABLE = "GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE"
LEGACY_REALTIME_PROBE = Path("GPT-VoiceCoding-legacy/scripts/rt_prototype.py")

#: The Claude lane's own `CLAUDE_CONFIG_DIR` (§4.1) — **persistent**, created
#: once and logged in once by hand, because a fresh one is logged out: Claude
#: Code keys its Keychain entry to the config directory. A location with a
#: default and an override, like every other of this harness's, and beside the
#: user-account credentials rather than among the run directories.
CLAUDE_CONFIG_DIRECTORY_VARIABLE = "GPTVOICECODING_ACCEPTANCE_CLAUDE_CONFIG_DIR"
CLAUDE_CONFIG_DIRECTORY_NAME = "claude-config"

#: Where `claude` records the account it is logged in as. The same file #217
#: measured the trust grant into, read here for a different key: a config
#: directory with no `oauthAccount` is one nobody has logged in to, which is the
#: state §4.1 says a *fresh* directory is always in. Its name is
#: `support.CLAUDE_STATE_NAME` — one spelling, because it is one file, and two
#: spellings of it is what #217 cost a whole lane.
CLAUDE_STATE_NAME = support.CLAUDE_STATE_NAME
CLAUDE_ACCOUNT_KEY = "oauthAccount"

#: Ground a Codex sandbox may write without asking. A run root inside any of
#: them is an `approval` that can never fire (§5), because the permission the
#: relay turn exists to raise is raised by the write being refused.
ALWAYS_WRITABLE = (Path("/tmp"),)
TEMPORARY_DIRECTORY_VARIABLE = "TMPDIR"

#: §5's row that `Machine` itself can fail: reading the operator's config to
#: learn which variable holds a lane's token is part of the same refusal, and it
#: is named once so the check and the reason cannot drift apart.
TOKEN_VARIABLE_CHECK = "bot token variable"


def check(method: Callable[[Preflight], str | None]) -> Callable[[Preflight], str | None]:
    """Mark a §5 refusal check, so `ORDER` can be held to naming every one.

    Identity, not shape: a check is what this says it is. Recognising them by
    their `str | None` return instead would collect the next helper that happens
    to share it and drop the next check that spells it differently (#362).
    """
    method.__refusal_check__ = True  # type: ignore[attr-defined]
    return method


#: The two lanes by role rather than by index. Spelled from `items.LANES` so the
#: names still live in one place (`tests/test_harness_contract.py`), and named
#: here because two of §5's checks are about **one** lane each and `LANES[0]` at
#: a call site says nothing about which.
CLAUDE_LANE, CODEX_LANE = items.LANES


def claude_config_directory(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    values = os.environ if environ is None else environ
    override = values.get(CLAUDE_CONFIG_DIRECTORY_VARIABLE)
    if override:
        return Path(override).expanduser()
    return support.acceptance_root(dict(values)) / CLAUDE_CONFIG_DIRECTORY_NAME


def realtime_probe_path(repository: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """The probe script, or nothing — a worktree is the case this exists for.

    The default is resolved through git's **common** directory rather than the
    checkout this file sits in: a worktree's parent is `.claude/worktrees`, and
    the sibling legacy checkout is beside the primary one.
    """
    values = os.environ if environ is None else environ
    override = values.get(REALTIME_PROBE_VARIABLE)
    if override:
        probe = Path(override).expanduser()
    else:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not common:
            return None
        probe = Path(common).resolve().parent.parent / LEGACY_REALTIME_PROBE
    return probe if probe.is_file() and os.access(probe, os.R_OK) else None


def codex_writable_roots(codex_home: Path) -> tuple[Path, ...]:
    """Every root the operator's own Codex config already allows a write into.

    Read from their `config.toml` rather than assumed, because the refusal is
    about *their* machine: a `writable_roots` entry covering the acceptance root
    is a permission that would never be asked for, and a harness that only knew
    about `/tmp` would grade an `approval` that could not fail.
    """
    configuration = codex_home / "config.toml"
    if not configuration.exists():
        return ()
    try:
        document = tomllib.loads(configuration.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    stated = document.get("sandbox_workspace_write", {}).get("writable_roots", [])
    if not isinstance(stated, list):
        return ()
    return tuple(Path(str(one)).expanduser() for one in stated)


def answering(socket_path: Path) -> bool:
    """Whether anything is **listening** there — the product's own question.

    `is_connectable` is what the engine's own `_claim` asks before it takes a
    socket over (`control_plane/server.py`), and a file nobody answers is what
    that code calls *debris*. Both of §5's socket rows turn on this and not on
    `exists()`: an engine killed with `SIGKILL` leaves its file behind, and a
    harness that read the file would refuse every later run until somebody
    deleted it by hand — a false refusal, which is the false verdict preflight
    exists to prevent, pointed the other way. Free either way: an `AF_UNIX`
    connect never leaves the machine.
    """
    return socket_path.exists() and is_connectable(socket_path, timeout=CLAIM_PROBE_SECONDS)


def login_shell_path(
    environ: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """The user's own PATH, or None — never a guess, never a partial answer.

    Mirrors `shell/Sources/ShellCore/LoginShellPath.swift`, which is the method
    the menu-bar shell uses and therefore the PATH the engine really runs on.
    `-lic`, not `-lc`: zsh sources `~/.zshrc` only when interactive, and
    `~/.zshrc` is where `nvm` and `brew shellenv` actually write. The sentinels
    separate the answer from an interactive profile's chatter, and **exactly two
    or nothing** — a third means something other than the `printf` wrote the
    marker, and then no part of the output is the answer.
    """
    values = os.environ if environ is None else environ
    shell = values.get("SHELL")
    if not shell or not os.access(shell, os.X_OK):
        return None
    try:
        printed = run(
            [shell, "-lic", PATH_SCRIPT],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=deadlines.PATH_TIMEOUT_SECONDS,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    parts = (printed or "").split(PATH_SENTINEL)
    if len(parts) != 3:  # exactly two sentinels bound exactly one answer
        return None
    answer = parts[1].strip(" \t")
    if not answer or "\n" in answer or "\0" in answer:
        return None
    if not any(entry.startswith("/") for entry in answer.split(":")):
        return None
    return answer


PATH_SENTINEL = "<<<GVC-PATH>>>"
PATH_SCRIPT = f"printf '{PATH_SENTINEL}%s{PATH_SENTINEL}' \"$PATH\""


#: What a version reads as when it could not be read. A sentence under the
#: lane's own key, never a missing key and never an empty string: `""` is what
#: every run of the rebuilt harness wrote while nothing passed a version at all
#: (#357), and a reader cannot tell that apart from an agent that printed none.
UNKNOWN_VERSION = "unknown"


def _unknown(reason: str) -> str:
    """A version that could not be read, as the one sentence the verdict carries."""
    return f"{UNKNOWN_VERSION} — {reason}"


def agent_version(
    binary: Path, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str:
    """What `<binary> --version` says it is, or the sentence saying why it does not.

    The one line each agent answers with, taken from whichever stream it used —
    `claude` prints `2.1.268 (Claude Code)` and `codex` prints `codex-cli
    0.154.0` (read 2026-09-11), both on stdout, and a CLI that chose stderr is
    reporting the same fact. Never raises: this is read into the verdict's
    constructor, and a fact that took the file down with it would be worse than
    the gap it is filling.
    """
    try:
        answered = run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=deadlines.VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as unreadable:
        return _unknown(f"`{binary} --version` did not answer: {unreadable!r}")
    printed = (answered.stdout or "").strip() or (answered.stderr or "").strip()
    if answered.returncode != 0 or not printed:
        # **Carrying what it said, not only that it failed.** A binary that
        # printed something and then exited non-zero did not answer the
        # question — the line may be a usage message rather than a version — but
        # a reader attributing a red months later needs the words it used, and
        # dropping them leaves a sentence that says it printed nothing when it
        # did not.
        said = f"printed {printed.splitlines()[0].strip()!r}" if printed else "printed nothing"
        return _unknown(f"`{binary} --version` exited {answered.returncode} and {said}")
    return printed.splitlines()[0].strip()


def foreign_codex(
    acceptance_root: Path,
    *,
    run: codex_processes.Runner = codex_processes.run_command,
    now: Callable[[], float] = time.time,
) -> str | None:
    """Why this run cannot be isolated from the Codex Sessions already open (§5, #228).

    **Codex discovery is machine-wide by construction, and that is the product
    behaving as written.** The adapter lists every live interactive `codex` TUI on
    the machine from one `ps`, and the per-lane `socket_directory` a run derives
    isolates the app-server socket, not that scan. So a Codex window the operator
    left open sits on the roster for the whole walk: one run had 586 of its 687
    roster readings polluted by one.

    Asked through **the adapter's own enumeration**, because a second scanner
    would be a second answer to "is this a Session" — and the answer the adapter
    gives is the reason a foreign TUI reaches the roster at all.

    **Unconditional on `--lane`**: both lanes' engines load both agent adapters,
    so a `--lane claude` run reads the same foreign row. **What this run owns is
    decided by place**: a candidate whose workspace is inside the acceptance root
    is the walk's own, because every run directory and every lane workspace is
    made there. A candidate with no controlling terminal is a Headless Run —
    never announced, never addressable, never on the graded roster — so it is not
    worth refusing over (#319).
    """
    sampled_at = now()
    try:
        live = asyncio.run(codex_processes.enumerate_runs(run=run, now=lambda: sampled_at))
    except (OSError, TimeoutError) as unreadable:
        return (
            "the process table could not be read, so this run cannot tell whether a Codex "
            f"Session it did not hand-start is live and would join both lanes' rosters: "
            f"{unreadable!r}"
        )
    owned = acceptance_root.expanduser().resolve(strict=False)
    strangers = [
        candidate
        for candidate in live
        if candidate.has_controlling_terminal
        and not candidate.workspace.expanduser().resolve(strict=False).is_relative_to(owned)
    ]
    if not strangers:
        return None
    named = "; ".join(f"pid {one.pid} in {one.workspace}" for one in strangers)
    return (
        "a Codex Session this run did not hand-start is live on this machine, and both lanes' "
        "engines bridge every Codex TUI on it — so it would sit on the roster the walk is "
        f"graded on: {named}. Quit these Codex sessions and re-run; this run will not stop "
        "them for you."
    )


def ask_bot(token: str, method: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """One Bot API call, by the token the run was told to read. Never journalled."""
    transport = http_transport(token=token, api_root=DEFAULT_API_ROOT)
    return transport(method, dict(parameters), timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS)


class SessionLock(Protocol):
    """What a held user-account session lock has to be: something that closes."""

    def release(self) -> None: ...


# --- the machine as §5 reads it ----------------------------------------------


@dataclass(frozen=True)
class Machine:
    """Everything the checks consult, taken once and passed in.

    Values for what is on disk, callables for what has to be shelled out or
    dialled. A fast test builds one that is wrong in exactly one way; nothing in
    a check reaches past this object, so what a test arranges is what the check
    sees.
    """

    lanes: tuple[str, ...]
    run_directory: Path
    repository: Path
    environ: Mapping[str, str]
    home: Path
    bundle: Path
    engine_socket: Path
    source_config: Path
    claude_config: Path
    codex_home: Path
    codex_control_socket: Path
    probe_script: Path | None
    writable_roots: tuple[Path, ...]
    provenance: Callable[[], support.Provenance]
    path_of_login_shell: Callable[[], str | None]
    which: Callable[[str, str], str | None] = field(
        default=lambda binary, path: shutil.which(binary, path=path)
    )
    #: What one agent binary reports about itself, for §7's verdict (#357). Not
    #: consulted by any check: no run refuses over a version, and this is read
    #: before preflight so that a refused run's verdict names the agents too.
    read_version: Callable[[Path], str] = field(default=agent_version)
    foreign_codex: Callable[[], str | None] = field(default=lambda: None)
    server_live: Callable[[Path], bool] = field(default=answering)
    ask_bot: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] = field(default=ask_bot)
    take_session_lock: Callable[[], SessionLock] = field(default=lambda: _no_lock())
    #: §4.1's one read-modify-write of the operator's own `config.toml`. The
    #: default is **inert**, and deliberately: the real reading rewrites a file
    #: this suite's fast tests must never reach, so it is wired in `real()` where
    #: the machine is being wired anyway, and a `Machine` built by a test that
    #: named no reconciliation reconciles nothing (`tests/conftest.py`'s rule,
    #: applied to the one file that is the operator's).
    reconcile_trust: Callable[[Path], tuple[str, ...]] = field(default=lambda _: ())

    @classmethod
    def real(cls, *, run_directory: Path, repository: Path, lanes: Sequence[str]) -> Machine:
        """This machine, with every reading wired to the thing that really answers it."""
        environ = dict(os.environ)
        home = Path.home()
        bundle = support.bundle_path(environ)
        codex_home = codex_runtime.default_codex_home(environ, home)
        root = support.acceptance_root(environ)
        return cls(
            lanes=tuple(lanes),
            run_directory=run_directory,
            repository=repository,
            environ=environ,
            home=home,
            bundle=bundle,
            engine_socket=default_socket_path(),
            source_config=support.source_config_path(environ, home),
            claude_config=claude_config_directory(environ, home),
            codex_home=codex_home,
            codex_control_socket=codex_runtime.control_socket(codex_home),
            probe_script=realtime_probe_path(repository, environ),
            writable_roots=(*ALWAYS_WRITABLE, *codex_writable_roots(codex_home)),
            # Memoised, because each is asked more than once and each costs
            # something real: the provenance walks two trees and the PATH is a
            # whole interactive login shell. A second reading would also be a
            # second *answer*, and a check that disagreed with the line the
            # journal already carries is worse than a slow one.
            provenance=cache(lambda: support.compare_engine_to_tree(bundle, repository)),
            path_of_login_shell=cache(lambda: login_shell_path(environ)),
            foreign_codex=cache(lambda: foreign_codex(root)),
            reconcile_trust=lambda root: support.reconcile_codex_trust(
                root, environment=environ, home=home, taken_by=run_directory.name
            ),
            take_session_lock=lambda: telegram_person.PersonSessionLock(
                run_directory=run_directory,
                held_by=telegram_person.ACCEPTANCE_RUN_HOLDER,
            ).acquire(),
        )

    def resolved_binary(self, lane: str) -> Path | None:
        """Where this lane's agent really is, or nothing at all (§4.4, §7).

        The one resolution there is: §5's `agent binary` refusal and §7's
        version are the same question asked for two reasons, and two walks of
        the same PATH could answer it two ways. It is `hand_started.resolve`'s
        walk, because that is the file the lane execs.
        """
        found = self.which(journey.lane(lane).binary, self.path_of_login_shell() or "")
        return Path(found) if found else None

    def agent_versions(self) -> dict[str, str]:
        """The agent versions seen (§7) — one entry per lane, whatever happened.

        **The ruling #357 asked for.** "The agent versions seen" means the
        version each lane's own binary reports, and nothing else: the roster
        carries no version at all — `control_plane/payloads.py`'s
        `session_document` travels a target, a name, a workspace, a lifecycle, a
        state, a progress and a reply window — so there is no second reading for
        this to be half of.

        Read off the file `which` finds on the PATH the engine will be handed,
        which is the one `hand_started.resolve` execs, so the version named is
        the agent the lane really launched rather than another of the same name
        earlier on this process's own PATH. That PATH is the memoised reading
        the refusals share, so this asks no second question about it.

        Never raises and never omits a lane. The verdict is **constructed** from
        this, before preflight has decided anything, so an exception here is a
        run with no `verdict.json` at all, and a missing key is a lane a reader
        would take for one that never ran.
        """
        return {lane: self._version_of(lane) for lane in self.lanes}

    def _version_of(self, lane: str) -> str:
        # Everything inside, the lane's own name included: `journey.lane` raises
        # for a name it does not know, and a raise here is the run with no
        # verdict at all that this method exists not to be.
        try:
            resolved = self.resolved_binary(lane)
            if resolved is None:
                return _unknown(
                    f"`{journey.lane(lane).binary}` does not resolve on the PATH the engine "
                    f"will be handed"
                )
            return self.read_version(resolved)
        except Exception as unreadable:  # noqa: BLE001 - every way it fails is one sentence
            return _unknown(f"reading the {lane} lane's version raised {unreadable!r}")

    def token_variable(self, lane: str) -> str:
        """The variable the lane's engine is told to read its token out of (§4.2).

        Lane one's is the operator's own configured name; lane two's is that name
        with `_2`. Derived from the position in `items.LANES` rather than from
        the lane's name, so the rule is "the second lane" and not "the Codex one".
        """
        configured = self.configured_channel().get("token_env")
        if not configured:
            raise Refused(
                TOKEN_VARIABLE_CHECK,
                f"the engine configuration at {self.source_config} names no "
                f"`[adapters.settings.companion_channel] token_env`, so this run cannot tell "
                f"which variable holds either lane's bot token",
            )
        return f"{configured}{'_2' if items.LANES.index(lane) else ''}"

    def configured_channel(self) -> Mapping[str, Any]:
        """The operator's real Companion Channel table — the run derives, never invents."""
        if not self.source_config.exists():
            raise Refused(
                TOKEN_VARIABLE_CHECK,
                f"no engine configuration at {self.source_config} to derive this run's from",
            )
        document = tomllib.loads(self.source_config.read_text())
        return dict(document.get("adapters", {}).get("settings", {}).get("companion_channel", {}))


class _no_lock:  # noqa: N801 - a stand-in, named for what it is
    """The lock a machine that was handed none takes: nothing, releasable."""

    def release(self) -> None:
        return None


# --- the checks --------------------------------------------------------------


class Preflight:
    """§5, in order, as a context manager over the one thing a check *holds*.

    Ordered cheapest and most local first, so a machine that is wrong in an
    obvious way is refused before anything crosses a network. Two orderings are
    not preference:

    * the **session lock** is taken before any bot token is read, any chat is
      opened, or any trust row is reconciled — a refusal that arrives after one
      of those has already had the collision it was there to prevent (#203);
    * the **foreign Codex** scan is the one costly check and is last but for the
      two HTTP calls, because everything above it is free.

    Held for the whole run and released on exit. `flock` belongs to the open file
    description, so a run killed outright leaves nothing to sweep.
    """

    def __init__(self, machine: Machine, journal: support.Journal) -> None:
        self.machine = machine
        self.journal = journal
        self._held = ExitStack()
        #: Every bot this run's lanes resolve to, filled by `getMe` and read by
        #: the checks after it. Public identities only — never a token.
        self.bots: dict[str, Mapping[str, Any]] = {}

    #: Every refusal of §5, in the order they are asked. The name is the table's
    #: row, and it is what the verdict's refusal is filed under.
    ORDER = (
        "bundle",
        "realtime probe script",
        "live engine",
        "writable run root",
        "claude config directory",
        "codex app-server",
        "session lock",
        TOKEN_VARIABLE_CHECK,
        "one bot token for both lanes",
        "login shell PATH",
        "agent binary",
        "foreign codex",
        "bot reachable",
        "chat opened",
    )

    def __enter__(self) -> str:
        try:
            for name in self.ORDER:
                reason = getattr(self, self._method(name))()
                if reason is not None:
                    raise Refused(name, reason)
            reference = self._settled()
        except BaseException:
            self._held.close()
            raise
        return reference

    def __exit__(self, *_: object) -> None:
        self._held.close()

    @staticmethod
    def _method(name: str) -> str:
        return "_" + name.replace(" ", "_").replace("-", "_")

    def _settled(self) -> str:
        """The stale trust row, reconciled rather than refused, and the passing line.

        §4.1: a killed run can leave a `[projects."<workspace>"]` row for a
        workspace under the acceptance run directory. That is the one row this
        harness ever writes into the operator's own `~/.codex/config.toml`, and a
        leftover is arranged away rather than graded — so it is journalled as
        `trust.reconciled` and the run goes on. The read-modify-write of the
        real file is `support.reconcile_codex_trust`; what happens here is the
        call and the journal line.
        """
        root = support.acceptance_root(dict(self.machine.environ))
        try:
            reconciled = self.machine.reconcile_trust(root)
        except support.CouldNotReconcile as unremoved:
            # §5 says a stale row is reconciled rather than refused — and a row
            # this harness **cannot** remove is a different fact. The run would
            # otherwise walk a lane whose workspace somebody else's row had
            # already trusted, which is the one thing the grant exists to make
            # this run's own.
            raise Refused("stale codex trust row", str(unremoved)) from None
        # Journalled only when a row was actually removed. §4.1's line is about a
        # **removal**, and a line on every run would say a killed run had left
        # something behind on every machine that has never had one.
        if reconciled:
            self.journal(
                "trust.reconciled",
                agent="codex",
                codex_home=str(self.machine.codex_home),
                workspaces=list(reconciled),
            )
        provenance = self.machine.provenance()
        return self.journal(
            "preflight.passed",
            lanes=list(self.machine.lanes),
            bundle=str(self.machine.bundle),
            commit=provenance.commit,
            provenance=provenance.reason,
            token_variables={
                lane: self.machine.token_variable(lane) for lane in self.machine.lanes
            },
            bots={lane: identity.get("username") for lane, identity in self.bots.items()},
        )

    # -- the machine's own arrangement ---------------------------------------

    @check
    def _bundle(self) -> str | None:
        """The bundle is there, has an interpreter, and is this checkout's `src/`."""
        bundle = self.machine.bundle
        if not bundle.exists():
            return f"no bundle at {bundle}"
        interpreter = support.bundled_python(bundle)
        # Executable, not merely present: the probe runs this, and a `Popen` on
        # an unrunnable interpreter raises where the row should have been read.
        if not (interpreter.exists() and os.access(interpreter, os.X_OK)):
            return f"the bundle at {bundle} carries no runnable engine interpreter"
        provenance = self.machine.provenance()
        return None if provenance.matches else provenance.reason

    @check
    def _realtime_probe_script(self) -> str | None:
        """A missing probe is a refusal, not a SKIPPED row (§5) — worktrees are why."""
        if self.machine.probe_script is not None:
            return None
        return (
            "no readable realtime probe script; §2 item 0b runs the maintainer's own "
            f"`rt_prototype.py --silent`, expected beside this checkout at "
            f"{LEGACY_REALTIME_PROBE}. Set {REALTIME_PROBE_VARIABLE} to it — a worktree "
            "does not carry the sibling checkout."
        )

    @check
    def _live_engine(self) -> str | None:
        """One bot, one engine: the menu-bar app's engine is not stopped for you."""
        live = self.machine.engine_socket
        if not self.machine.server_live(live):
            return None
        return (
            f"the shell's engine is answering at {live} — one bot, one engine "
            f"(`docs/app-bundle.md` § Cutover). Quit the menu-bar app and run again; this "
            f"run will not stop it for you."
        )

    @check
    def _writable_run_root(self) -> str | None:
        """A run root Codex may already write is an `approval` that can never fire.

        §3's relay turn asks the Codex lane for a write **outside** the Session's
        writable roots, and the permission that raises is what `approval` grades.
        A run directory inside ground the sandbox already allows — `/tmp`, the
        operator's `TMPDIR`, or a `writable_roots` entry of their own — makes
        that write succeed silently, and the item would be red for a reason that
        is not the product's.
        """
        root = self.machine.run_directory.expanduser().resolve(strict=False)
        temporary = self.machine.environ.get(TEMPORARY_DIRECTORY_VARIABLE)
        candidates = [*self.machine.writable_roots]
        if temporary:
            candidates.append(Path(temporary).expanduser())
        inside = [
            one for one in candidates if root.is_relative_to(one.expanduser().resolve(strict=False))
        ]
        if not inside:
            return None
        return (
            f"this run's directory {root} is inside ground a Codex sandbox may already write "
            f"({', '.join(str(one) for one in inside)}), so the relay turn's write would "
            f"raise no permission and `approval` could never fire. Move the acceptance root "
            f"with {support.ACCEPTANCE_ROOT_VARIABLE}."
        )

    @check
    def _claude_config_directory(self) -> str | None:
        """The lane's own `CLAUDE_CONFIG_DIR`, logged in once by hand (§4.1)."""
        if CLAUDE_LANE not in self.machine.lanes:
            return None
        directory = self.machine.claude_config
        state = directory / CLAUDE_STATE_NAME
        arranged = (
            f"Run `{claude_hooks.CONFIG_DIRECTORY_VARIABLE}={directory} claude` once and log "
            f"in; a fresh directory is logged out, because Claude Code keys its Keychain entry "
            f"to it."
        )
        if not directory.is_dir():
            return f"the claude lane's config directory {directory} is not there. {arranged}"
        try:
            recorded = json.loads(state.read_text())
        except (OSError, ValueError):
            return (
                f"the claude lane's config directory {directory} has no readable "
                f"{CLAUDE_STATE_NAME}. {arranged}"
            )
        if not recorded.get(CLAUDE_ACCOUNT_KEY):
            return (
                f"the claude lane's config directory {directory} is not authenticated — "
                f"{CLAUDE_STATE_NAME} names no {CLAUDE_ACCOUNT_KEY}. {arranged}"
            )
        return None

    @check
    def _codex_app_server(self) -> str | None:
        """The operator's shared app-server is live at its derived socket (§4.3).

        The Codex lane runs on the operator's own `~/.codex` and never installs a
        job of its own, so the server it joins is theirs. A lane that finds none
        is REFUSED rather than left to degrade silently: its TUI would run its own
        core, its Session would not join the daemon, and `roster` would go red for
        the environment (#232).
        """
        if CODEX_LANE not in self.machine.lanes:
            return None
        control_socket = self.machine.codex_control_socket
        if self.machine.server_live(control_socket):
            return None
        return (
            f"the operator's shared Codex app-server is not answering at {control_socket}, "
            f"derived from {codex_runtime.CODEX_HOME_VARIABLE} "
            f"({self.machine.codex_home}). The codex lane joins that server (ADR 0022) and "
            f"would otherwise run its own core, leaving its Session off the roster."
        )

    # -- what a second run must not walk into --------------------------------

    @check
    def _session_lock(self) -> str | None:
        """One acceptance run per machine, refused rather than kept by hand (#203).

        Taken **here**, ahead of every token, every chat and the trust
        reconciliation, because those are what two runs collide over: one SQLite
        session behind one client, and one `config.toml` a thread lock means
        nothing about across processes.
        """
        try:
            self._held.callback(self.machine.take_session_lock().release)
        except telegram_person.SessionInUse as in_use:
            return str(in_use)
        return None

    # -- the bots ------------------------------------------------------------

    @check
    def _bot_token_variable(self) -> str | None:
        """Each lane's token is in the environment, under the name its config gives."""
        for lane in self.machine.lanes:
            variable = self.machine.token_variable(lane)
            if not self.machine.environ.get(variable):
                return (
                    f"the {lane} lane's bot token variable {variable} is unset. The run drives "
                    f"the real bot; export it into the shell that runs pytest."
                )
        return None

    @check
    def _one_bot_token_for_both_lanes(self) -> str | None:
        """Two lanes is two bots: two engines long-polling one take each other's updates."""
        held: dict[str, list[str]] = {}
        for lane in self.machine.lanes:
            held.setdefault(self.machine.environ[self.machine.token_variable(lane)], []).append(
                lane
            )
        shared = [lanes for lanes in held.values() if len(lanes) > 1]
        if not shared:
            return None
        return "; ".join(
            f"the {' and '.join(lanes)} lanes hold the same bot token "
            f"({', '.join(self.machine.token_variable(lane) for lane in lanes)}), and two "
            f"engines long-polling one bot take each other's updates"
            for lanes in shared
        )

    # -- the PATH the engine will really run on ------------------------------

    @check
    def _login_shell_PATH(self) -> str | None:  # noqa: N802 - the table's own spelling
        path = self.machine.path_of_login_shell()
        if path:
            return None
        return (
            "could not read a usable PATH from the login shell, so the engine would run on "
            "launchd's and find neither agent. "
            "`shell/Sources/ShellCore/LoginShellPath.swift` is the method being mirrored."
        )

    @check
    def _agent_binary(self) -> str | None:
        """`claude` / `codex` resolve on the PATH the engine will be handed (§4.4)."""
        for lane in self.machine.lanes:
            if self.machine.resolved_binary(lane) is None:
                return (
                    f"the {lane} lane's `{journey.lane(lane).binary}` does not resolve on the "
                    f"PATH the engine will be handed"
                )
        return None

    @check
    def _foreign_codex(self) -> str | None:
        return self.machine.foreign_codex()

    # -- the two calls -------------------------------------------------------

    @check
    def _bot_reachable(self) -> str | None:
        """`getMe`: it answers, and says who it is. The token itself is never recorded."""
        for lane in self.machine.lanes:
            variable = self.machine.token_variable(lane)
            try:
                identity = self.machine.ask_bot(self.machine.environ[variable], "getMe", {})
            except TelegramError as unreachable:
                return (
                    f"the {lane} lane's Telegram bot did not answer getMe: "
                    f"{unreachable.detail}. {variable} holds a dead token."
                )
            self.bots[lane] = dict(identity)
            self.journal(
                "preflight.getMe",
                lane=lane,
                token_env=variable,
                username=identity.get("username"),
                id=identity.get("id"),
            )
        return None

    @check
    def _chat_opened(self) -> str | None:
        """A bot cannot open a chat with a person — Telegram gives the human the first move.

        Asked with `getChat`, which is a **read**. The obvious alternative — send
        something and see whether it lands — would write a probe into the very
        chat the run then reads for evidence.
        """
        chat_id = str(self.machine.configured_channel().get("chat_id", ""))
        if not chat_id:
            return (
                f"the engine configuration at {self.machine.source_config} names no "
                f"`[adapters.settings.companion_channel] chat_id`, so no lane has a chat"
            )
        for lane in self.machine.lanes:
            username = self.bots.get(lane, {}).get("username")
            try:
                self.machine.ask_bot(
                    self.machine.environ[self.machine.token_variable(lane)],
                    "getChat",
                    {"chat_id": chat_id},
                )
            except TelegramError as refused:
                return (
                    f"@{username} cannot reach chat {chat_id}: {refused.detail}. A bot cannot "
                    f"open a chat with a person — send `/start` to @{username} from the "
                    f"Telegram user account this acceptance drives, then run again."
                )
        return None


# --- §2 item 0b — the probe --------------------------------------------------

#: What the probe prints when a remote track ends. The count is the observation
#: §2 item 0b asks for, and it is printed by the **shutdown** — which is why the
#: probe is ended with SIGINT and never with a kill.
TOTAL_FRAMES = re.compile(r"total remote frames:\s*(\d+)")
FIRST_FRAME = re.compile(r"first remote audio frame received")

PROBE_TRANSCRIPT_NAME = "realtime-probe.log"


@dataclass(frozen=True)
class ProbeReading:
    """What one probe run came to: frames, and whether a first frame was ever seen."""

    frames: int
    heard: bool
    returncode: int | None
    transcript: Path

    @property
    def passed(self) -> bool:
        return self.frames > 0


def frames_in(output: str) -> tuple[int, bool]:
    """The frame total and the first-frame line, read out of the probe's own words."""
    total = TOTAL_FRAMES.search(output)
    return (int(total.group(1)) if total else 0, bool(FIRST_FRAME.search(output)))


def run_realtime_probe(
    machine: Machine,
    journal: support.Journal,
    *,
    path: str,
    popen: Callable[..., Any] = subprocess.Popen,
    interrupt: Callable[[int, int], None] = os.killpg,
) -> tuple[ProbeReading, str]:
    """The engine-free realtime start, run for its deadline and ended with SIGINT.

    Invoked, never rewritten (§2 item 0b), on the **bundle's own interpreter** —
    which is where `aiortc` and `av` are, and the thing being accepted is the
    `.app`. Ended with its own clean hang-up rather than a kill, because the
    frame total is printed by the shutdown and a killed probe reports nothing at
    all.
    """
    assert machine.probe_script is not None, "a missing probe script is a preflight refusal"
    transcript = machine.run_directory / PROBE_TRANSCRIPT_NAME
    command = [
        str(support.bundled_python(machine.bundle)),
        str(machine.probe_script),
        "--silent",
        "--cwd",
        str(machine.run_directory),
    ]
    started = journal("probe.start", command=command, deadline="PROBE_SECONDS")
    with transcript.open("wb") as sink:
        process = popen(
            command,
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**machine.environ, "PATH": path},
            start_new_session=True,
        )
        try:
            process.wait(timeout=deadlines.PROBE_SECONDS)
        except subprocess.TimeoutExpired:
            interrupt(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=deadlines.PROBE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=deadlines.PROBE_GRACE_SECONDS)
    frames, heard = frames_in(transcript.read_text(errors="replace"))
    reading = ProbeReading(frames, heard, process.returncode, transcript)
    reference = journal(
        "probe.done",
        returncode=reading.returncode,
        frames=reading.frames,
        heard=reading.heard,
        transcript=str(transcript),
        started=started,
    )
    return reading, reference
