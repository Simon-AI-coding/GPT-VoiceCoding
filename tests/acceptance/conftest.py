"""The run's options and its fixtures (#350).

**Responsibilities held here** (§9's `conftest.py`): the two selectors of §6, the
preflight of §5, and the fixtures that arrange a run — the run directory, the
journal, the verdict, the engine and the hand-started Session.

The deadlines §9 also parks here live in `deadlines.py` instead. A conftest is
loaded by pytest **by path** and is not an importable name
(`tests/test_layout.py`), so a constant kept here cannot be read by the fast
suite — and every deadline in this harness is pinned by a fast test. The
selection is in `items.py` for the same reason.

§4's concurrency is here too, and it is the reason `_one_lane` is a function
rather than three more fixtures: a fixture is set up on the thread that
*requests* it, which is pytest's, so two lanes' engines built there would be two
engines built one after the other. What is **not** here is the walk of §2, which
is `journey`'s.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hand_started
import items
import journey
import preflight as preflight_module
import pytest
import support
import telegram_person
from items import Item

from gpt_voicecoding.installation import claude_hooks

REPOSITORY = Path(__file__).resolve().parents[2]


def pytest_addoption(parser: pytest.Parser) -> None:
    """The two selectors, and why they are options rather than `-k` (§6).

    An item is not a test — the items of a lane share one engine, one Session
    and one chat — so `-k` cannot address one, and asking for an item is asking
    for the items beneath it too (`items.PREREQUISITES`). A lane *is* a
    parametrised test, but a lane nobody selected must not be **started**, which
    is a decision made before collection rather than a filter after it.
    """
    group = parser.getgroup("acceptance")
    group.addoption(
        "--step",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "grade this item and walk its prerequisites as ungraded setup; repeatable. "
            "Default: every item. Names: " + ", ".join(str(item) for item in items.ITEMS)
        ),
    )
    group.addoption(
        "--lane",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "walk this lane only; repeatable. Default: every lane, concurrently. Names: "
            + ", ".join(items.LANES)
        ),
    )


def _resolve(select: Callable[[list[str]], Any], config: pytest.Config, option: str) -> Any:
    """One selector, resolved into pytest's own way of refusing a bad option.

    Both selectors refuse the same way (`items.UnknownName`), so they are
    converted the same way here — a second `try`/`except` per option is how the
    two refusals come to read differently.
    """
    try:
        return select(config.getoption(option, default=[]))
    except items.UnknownName as unknown:
        raise pytest.UsageError(str(unknown)) from None


def _selection(config: pytest.Config) -> items.Selection:
    return _resolve(items.select, config, "--step")


def _lanes(config: pytest.Config) -> tuple[str, ...]:
    return _resolve(items.select_lanes, config, "--lane")


def pytest_configure(config: pytest.Config) -> None:
    """Resolve both selectors before anything is collected.

    A misspelled `--step` must never silently drop an item (§6), so the refusal
    happens here — before collection, where it is a usage error carrying the
    list — rather than inside a fixture, where it would arrive after the run had
    already decided what to walk.
    """
    _selection(config)
    _lanes(config)


# `items` is pytest's own name for this hook's argument and cannot be renamed;
# inside this one function it shadows the `items` module, which the function does
# not need.
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Drop the tests whose items this run did not ask for (§6).

    A test declares what it covers with `@pytest.mark.covers(...)`; anything
    unmarked — the whole fast suite — is left alone. `--step probe` walks no
    lane, and `--step approval` runs no probe: selecting an item is selecting
    *only* it and the ground beneath it.

    One rule and no special case: each run-level check is selectable like any
    item (§2) and so has a test of its own, collected by default like the lane
    test is.
    """
    chosen = set(_selection(config).items)
    kept, dropped = [], []
    for test in items:
        marker = test.get_closest_marker("covers")
        if marker is None or chosen.intersection(marker.args):
            kept.append(test)
        else:
            dropped.append(test)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Every test that walks a lane is parametrised over the lanes this run selected.

    The parametrisation stays pytest's own, so a failure reads `test_the_lane[codex]`
    without a module per lane to say it — and `--lane` decides which ids exist
    rather than deselecting them after the fact.
    """
    if "lane" in metafunc.fixturenames:
        metafunc.parametrize(
            "lane",
            [journey.lane(name) for name in _lanes(metafunc.config)],
            ids=lambda one: one.name,
        )


@pytest.fixture(scope="session")
def selection(request: pytest.FixtureRequest) -> items.Selection:
    """Which items this run grades, and which it walks only to reach them."""
    return _selection(request.config)


@pytest.fixture(scope="session")
def selected_lanes(request: pytest.FixtureRequest) -> tuple[str, ...]:
    return _lanes(request.config)


@pytest.fixture(scope="session")
def run_directory() -> Path:
    """§7's directory for this run, named by a UTC timestamp."""
    return support.new_run_directory()


@pytest.fixture(scope="session")
def journal(run_directory: Path) -> support.Journal:
    return support.Journal(run_directory / support.JOURNAL_NAME)


@pytest.fixture(scope="session")
def commit() -> str:
    """The checkout the bundle is graded against (§7)."""
    return subprocess.run(
        ["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.fixture(scope="session")
def verdict(
    run_directory: Path,
    journal: support.Journal,
    selection: items.Selection,
    selected_lanes: tuple[str, ...],
    commit: str,
    machine: preflight_module.Machine,
) -> Iterator[support.Verdict]:
    """The one artifact a reader needs — written however the run ends (§7).

    Written from a teardown rather than by whatever finished last, because a run
    that raised is exactly the one whose verdict is worth having.

    Every fact §7 names the file carries is handed over **here**, at the one
    construction there is, and before preflight has decided anything: §7's rule
    is that a refusal still writes a valid verdict, and what a red read months
    later is attributed by is the build under test and the agents that walked it
    (#357). Both come off the machine — `bundle` is the path preflight also
    journals, and the versions are read from the binaries on the PATH the engine
    is handed. Neither reading refuses a run, and neither can raise.
    """
    written = support.Verdict(
        run_id=run_directory.name,
        selection=selection,
        lanes=selected_lanes,
        journal=journal,
        bundle=str(machine.bundle),
        commit=commit,
        versions=machine.agent_versions(),
    )
    started = time.monotonic()
    try:
        yield written
    finally:
        written.seconds["run"] = time.monotonic() - started
        _scan_for_credentials(written, run_directory, journal, machine)
        written.write(run_directory / support.VERDICT_NAME)


def _scan_for_credentials(
    verdict: support.Verdict,
    run_directory: Path,
    journal: support.Journal,
    machine: preflight_module.Machine,
) -> None:
    """§8's rule, read over the tree this run just wrote (the deferred item of #351).

    A rule about what is **absent** is only as good as the reading that looks for
    it, and until now nothing read the derived configs, the engine logs and the
    journal a real run leaves behind. Here rather than in a test, because it is a
    teardown of the run and not a claim about one item: the verdict carries the
    result (`Verdict.scanned`), and a run that wrote a token into an artifact
    does not report PASS.

    A scan that could not be **assembled** — no token variable, no account
    credentials — is journalled and leaves the verdict unscanned rather than
    clean: a run refused before it had either is not evidence of anything, and
    saying so is the honest answer.
    """
    try:
        secrets = support.secrets_of(
            machine.environ,
            token_variables=[machine.token_variable(lane) for lane in machine.lanes],
            api_hash=_api_hash(machine),
        )
    except Exception as unassembled:  # noqa: BLE001 - every way it fails is one line
        journal("credentials.unscanned", why=repr(unassembled))
        return
    if not secrets:
        # **Nothing to look for is not a clean tree.** A run refused at §5's
        # `bot token variable` check has empty variables rather than missing
        # ones, so the assembling above succeeds and returns nothing — and a
        # scan for zero values would report a clean scan it never performed.
        journal("credentials.unscanned", why="this run was handed no credential values to scan for")
        return
    found = support.scan_for_credentials(run_directory, secrets)
    verdict.scanned(
        found,
        # The **artifacts**, never the values: a scan that printed what it found
        # would be the one artifact carrying every credential at once.
        journal("credentials.scanned", artifacts=list(found), secrets=len(secrets)),
    )


def _api_hash(machine: preflight_module.Machine) -> str | None:
    """The user account's `api_hash`, when this machine has one to look for."""
    try:
        return telegram_person.load_credentials(environ=machine.environ).api_hash
    except telegram_person.PersonError:
        return None


@pytest.fixture(scope="session")
def machine(run_directory: Path, selected_lanes: tuple[str, ...]) -> preflight_module.Machine:
    """This machine, as §5 reads it — the one place the real readings are wired."""
    return preflight_module.Machine.real(
        run_directory=run_directory, repository=REPOSITORY, lanes=selected_lanes
    )


@pytest.fixture(scope="session")
def preflight(
    machine: preflight_module.Machine,
    journal: support.Journal,
    verdict: support.Verdict,
    run_directory: Path,
) -> Iterator[preflight_module.Preflight]:
    """§5's refusals, every one a read and never a turn.

    Any failure is REFUSED: the run exits non-zero, writes a valid `verdict.json`
    naming the refusal, and **never starts an engine** — which holds by
    construction rather than by care, because every fixture that starts one lists
    this one first and a fixture that raised has no dependants.

    The checks are in `preflight.py`, not here: a conftest is loaded by path and
    is not importable, and every refusal below is driven by a fast test
    (`tests/test_harness_preflight.py`).
    """
    checks = preflight_module.Preflight(machine, journal)
    try:
        with checks as passed:
            verdict.record(Item.PREFLIGHT, support.PASS, passed)
            print(f"\nacceptance run directory: {run_directory}")
            # The checks themselves rather than the line they passed on: each
            # lane's bot was resolved here by `getMe` (§5), and the chat every
            # item reads is that bot's. Resolving it a second time would be a
            # second answer about who the lane is talking to.
            yield checks
    except preflight_module.Refused as refused:
        verdict.refuse(f"{refused.check}: {refused.reason}")
        pytest.fail(f"preflight refused — {refused.check}: {refused.reason}", pytrace=False)


@dataclass(frozen=True)
class Arrangement:
    """Everything a lane's walk needs that belongs to the **run** rather than the lane.

    One value rather than seven parameters through two functions: what the lanes
    share is a run, and naming it once is what keeps the next thing a run
    acquires from becoming an eighth parameter in three signatures.
    """

    run_directory: Path
    journal: support.Journal
    verdict: support.Verdict
    selection: items.Selection
    machine: preflight_module.Machine
    path_value: str
    #: Each lane's private chat with its own bot (§4.2 items 4–5), or nothing
    #: for a lane the run could not open one for. One client backs both: one
    #: SQLite session is one account, and the lanes differ by peer.
    chats: Mapping[str, journey.Chat] = field(default_factory=dict)


@dataclass
class LaneRun:
    """One lane's whole walk, on its own thread, and how it ended.

    An exception that escapes a thread is a traceback on stderr and a test that
    passes, so the thread keeps hold of it — and writes the rows the lane owed
    before it does, because a lane that left no row is one the verdict cannot
    tell apart from a lane that was never asked to run.
    """

    lane: journey.Lane
    thread: threading.Thread | None = None
    failure: BaseException | None = None


@pytest.fixture(scope="session")
def lane_runs(
    preflight: preflight_module.Preflight,
    probe_run: object,  # noqa: ARG001 - both run-level checks precede any lane (§2)
    person: telegram_person.PersonConnection,
    selected_lanes: tuple[str, ...],
    selection: items.Selection,
    run_directory: Path,
    journal: support.Journal,
    verdict: support.Verdict,
    machine: preflight_module.Machine,
) -> dict[str, LaneRun]:
    """Both lanes, walking in parallel on one thread each (§4).

    Depends on `probe_run` because §2 puts both run-level checks **before any
    lane starts**, and collection order does not: `test_lanes.py` sorts ahead of
    `test_realtime_probe.py`, so without this the probe would run after the
    lanes it is supposed to precede. A red probe still does not stop them — it
    is FAIL for the run and the lanes walk on (§7), so `probe_run` answers
    rather than raising when the probe found nothing.

    Session-scoped and **joined here**, so the tests that read the verdict read
    a finished one, and so the run costs one lane's wall clock rather than the
    sum of two.

    The threads are where §4's isolation is actually spent: two engines, two
    workspaces, two bots, and — on the Claude lane — a config directory of its
    own. Everything they do not share is arranged in `_one_lane`.
    """
    arrangement = Arrangement(
        run_directory=run_directory,
        journal=journal,
        verdict=verdict,
        selection=selection,
        machine=machine,
        path_value=machine.path_of_login_shell() or "",
        # Resolved here, before either thread starts: `get_entity` is a call on
        # the account, and two lanes racing one client for it would be two
        # answers to a question with one.
        chats=_chats(person, preflight, journal),
    )
    runs = {name: LaneRun(lane=journey.lane(name)) for name in selected_lanes}
    for run in runs.values():
        run.thread = threading.Thread(
            target=_walk_lane, args=(run, arrangement), name=f"lane-{run.lane.name}"
        )
        run.thread.start()
    for run in runs.values():
        if run.thread is not None:
            run.thread.join()
    return runs


@pytest.fixture(scope="session")
def person(
    preflight: preflight_module.Preflight,  # noqa: ARG001 - the session lock is preflight's
    journal: support.Journal,
) -> Iterator[telegram_person.PersonConnection]:
    """The Telegram **user account**, one client for the whole run (§8).

    One SQLite session backs one client — Telethon's own rule — so both lanes
    share this and differ by peer. Opened after preflight, which is what holds
    the cross-process session lock: two runs on one session file is the
    `database is locked` §5 refuses rather than meets.
    """
    with telegram_person.PersonConnection(journal=journal) as connection:
        yield connection


def _chats(
    person: telegram_person.PersonConnection,
    preflight: preflight_module.Preflight,
    journal: support.Journal,
) -> dict[str, journey.Chat]:
    """One chat per lane, with the bot preflight already said hello to.

    The username is handed down from `getMe` (§5) rather than configured a second
    time, so no bot is named anywhere in this suite and no lane can end up
    reading a chat that is not its own.
    """
    chats: dict[str, journey.Chat] = {}
    for lane, identity in preflight.bots.items():
        username = identity.get("username")
        if not username:
            # A bot that answered `getMe` without a username is a lane with no
            # peer to resolve. Said out loud, because the items that need a chat
            # are about to be blocked and a reader is owed the cause rather than
            # four blocked rows with no line behind them.
            journal("chat.unopened", lane=lane, why="the bot's getMe carried no username")
            continue
        chats[lane] = journey.Chat(peer=person.peer(str(username)), connection=person)
        journal("chat.opened", lane=lane, bot=str(username))
    return chats


def _walk_lane(run: LaneRun, arrangement: Arrangement) -> None:
    """The thread body: arrange this lane, walk it, and never raise into the thread."""
    started = time.monotonic()
    try:
        _one_lane(run, arrangement)
    except BaseException as unfinished:  # noqa: BLE001 - the verdict is what reports it
        run.failure = unfinished
        journey.unarranged(
            run.lane,
            selection=arrangement.selection,
            journal=arrangement.journal,
            verdict=arrangement.verdict,
            why=f"the lane ended in {type(unfinished).__name__}: {unfinished}",
        )
    finally:
        arrangement.verdict.seconds[run.lane.name] = time.monotonic() - started


def _one_lane(run: LaneRun, arrangement: Arrangement) -> None:
    """A fresh engine, a fresh workspace and a hand-started Session, then the walk (§4).

    This is one function rather than three fixtures because the two lanes run on
    two threads: a fixture is set up on the thread that *requests* it, which is
    pytest's, and two lanes' engines built there would be two engines built one
    after the other.

    Both of this lane's sockets live under `/tmp` rather than in the run
    directory, because Darwin caps an `AF_UNIX` path at 103 bytes and the run
    directory is 111 before the socket's own name — the same reason
    `config.RUNTIME_ROOT` exists. `support.lane_sockets` makes that directory,
    journals where it went, and takes it away again.

    **The Session is started after the engine, and that is a choice with a
    reason.** Both of the Claude lane's routes are hot — a Session already
    running is reached by the built-in inbox socket and by the user-scope hooks
    (#71) — so the harder order (Session first) is one the product claims to
    survive, and a later ticket may want it. It is not this run's order because a
    Session started before the engine has no `SessionStart` for the engine to
    have heard, and every red would then have the same single cause.
    """
    lane = run.lane
    directory = arrangement.run_directory
    workspace = support.fresh_workspace(
        directory / f"workspace-{lane.name}", arrangement.path_value
    )
    with support.lane_sockets(directory.name, lane.name, arrangement.journal) as sockets:
        _the_lane_on(run, arrangement, workspace, sockets)


def _the_lane_on(run: LaneRun, arrangement: Arrangement, workspace: Path, sockets: Path) -> None:
    """The engine, the trust, the Session and the walk, on ground already arranged."""
    lane, machine = run.lane, arrangement.machine
    directory = arrangement.run_directory
    engine_directory = directory / f"engine-{lane.name}"
    config = support.derive_config(
        source=machine.source_config,
        engine_directory=engine_directory,
        workspace=workspace,
        socket_path=sockets / "control.sock",
        token_variable=machine.token_variable(lane.name),
        codex_socket_directory=sockets,
        delegate_model=journey.DELEGATED_TURN_MODEL,
        dropped_agents=lane.dropped_agents,
    )
    # §4.1: the Claude lane's own config directory reaches the Session **and**
    # the engine — `claude agents --json` inherits the engine process's
    # environment, so an engine without the variable lists another registry.
    # The Codex lane has none, and cannot: ADR 0022 derives the shared
    # app-server's socket from one `CODEX_HOME`.
    lane_variables = (
        {claude_hooks.CONFIG_DIRECTORY_VARIABLE: str(machine.claude_config)}
        if lane.own_config_directory
        else {}
    )
    terminal = hand_started.terminal_environment(arrangement.path_value, extra=lane_variables)
    environment = terminal.environment
    # §4.3: and one variable the **engine alone** carries — the Claude lane
    # engine's own empty `CODEX_HOME`, so it joins no shared app-server and sees
    # no Codex thread on the machine (#355). The Session's environment above is
    # deliberately untouched: the Codex lane's TUI must join the operator's real
    # daemon or the product is right not to list it (#232).
    engine_variables = support.derive_engine_variables(
        lane_variables, engine_directory=engine_directory, own_codex_home=lane.empty_codex_home
    )
    engine = support.Engine(
        config=config,
        bundle=machine.bundle,
        journal=arrangement.journal,
        token=machine.environ[config.token_variable],
        path_value=arrangement.path_value,
        # The **scrubbed** environment, the Session's own. The engine runs
        # `claude agents --json` (§4.1), and the real engine is started by the
        # menu-bar shell with no agent markers in its environment at all — this
        # harness is run from inside a Claude Code session, which is the one
        # place they come from (§4.4, #73).
        base=environment,
        extra=engine_variables,
    )
    surface = support.Bridgectl(
        bundle=machine.bundle, socket_path=config.socket_path, journal=arrangement.journal
    )
    binary = hand_started.resolve(lane.binary, arrangement.path_value)
    if binary is None:  # §5 refuses this before a lane starts; here it is a lane that cannot
        raise hand_started.SessionRefused(
            f"`{lane.binary}` does not resolve on the PATH the engine was handed"
        )

    # §4.1, and **before the engine**: the hooks are what the Session registers
    # through, and the engine publishes the address they look for. Only the lane
    # with a config directory of its own has one to arrange; the Codex lane
    # starts no Claude Session and its engine drops that kind entirely (§4.3).
    if lane.own_config_directory:
        support.arrange_claude_hooks(
            environment, bundle=machine.bundle, journal=arrangement.journal
        )

    with support.TrustGate(
        workspace,
        agent=lane.agent,
        # The **Session's** environment, not this process's: it is what decides
        # which Claude state file the grant has to land in (§4.1, #217).
        environment=environment,
        journal=arrangement.journal,
        run_id=directory.name,
    ):
        engine.start()
        session: hand_started.Session | None = None
        try:
            started_at = time.time()
            session = hand_started.Session(
                lane=lane.name,
                binary=binary,
                arguments=hand_started.launch_arguments(lane.arguments, lane.boot_words),
                workspace=workspace,
                environment=terminal,
                journal=arrangement.journal,
                transcript=directory / f"pty-{lane.name}.log",
            )
            session.start()
            journey.Walk(
                lane=lane,
                selection=arrangement.selection,
                journal=arrangement.journal,
                verdict=arrangement.verdict,
                bridgectl=surface,
                engine=engine,
                session=session,
                workspace=workspace,
                run_directory=directory,
                chat=arrangement.chats.get(lane.name),
                truth=_agents_own_record(
                    lane, session, environment, machine, workspace, started_at
                ),
                boot_turn_over=_boot_turn_over(lane, machine, workspace, started_at),
                membership=_daemon_membership(lane, session, machine, workspace, started_at),
            ).walk()
        finally:
            # Both, whatever either does: `Engine.stop` can raise after a kill
            # that did not take, and a hand-started TUI left running is a
            # Session on the *next* run's roster (§5's foreign-codex refusal is
            # what it would meet).
            try:
                engine.stop()
            finally:
                if session is not None:
                    session.stop()


def _agents_own_record(
    lane: journey.Lane,
    session: hand_started.Session,
    environment: dict[str, str],
    machine: preflight_module.Machine,
    workspace: Path,
    started_at: float,
) -> Callable[[], hand_started.GroundTruth | None]:
    """Who the harness started, according to the **agent** rather than the engine.

    The two lanes answer this differently and neither is the other's fallback:
    `claude` keeps an official roster of its own, and `codex` writes nothing at
    all until its first turn — so its oracle is the process the harness started,
    which is the same evidence the product's own discovery has.
    """
    if lane.own_config_directory:
        return lambda: hand_started.claude_ground_truth(session.pid or 0, environment)
    return lambda: hand_started.codex_ground_truth(
        session.pid or 0, machine.codex_home, workspace, started_at
    )


def _boot_turn_over(
    lane: journey.Lane, machine: preflight_module.Machine, workspace: Path, started_at: float
) -> Callable[[], bool]:
    """Whether the turn the launch started has ended, on Codex's own bracketing (§3)."""
    if lane.boot_words is None:
        return lambda: True
    return lambda: hand_started.codex_turn_over(
        hand_started.codex_rollout(machine.codex_home, workspace, started_at)
    )


def _daemon_membership(
    lane: journey.Lane,
    session: hand_started.Session,
    machine: preflight_module.Machine,
    workspace: Path,
    started_at: float,
) -> Callable[[], support.DaemonMembership | None]:
    """Whether the shared Codex daemon holds this Session's thread (§4.3, #232).

    Asked of the thread id **as of now** rather than of one resolved earlier: the
    id is written when the first turn starts, and the boot turn this follows is
    that turn.
    """
    if lane.own_config_directory:
        return lambda: None
    return lambda: support.codex_daemon_membership(
        hand_started.codex_ground_truth(
            session.pid or 0, machine.codex_home, workspace, started_at
        ).session_id,
        control_socket=machine.codex_control_socket,
    )


@pytest.fixture(scope="session")
def probe_run(
    preflight: str,
    machine: preflight_module.Machine,
    selection: items.Selection,
    journal: support.Journal,
    verdict: support.Verdict,
) -> object:
    """The engine-free realtime probe, run once and recorded (§2 item 0b).

    **#351's**: the maintainer's own `rt_prototype.py --silent` on the bundle's
    interpreter, for `deadlines.PROBE_SECONDS` and then SIGINT — a `kill()` loses
    the frame-count line the row rests on.

    It **records its row and answers**; it never raises. A red probe is FAIL for
    the run and the lanes still walk (§7), and a fixture that raised would stop
    them. When `probe` is not selected it does nothing and answers, so a lane
    run that depends on it for ordering does not drag the probe along.
    """
    if Item.PROBE not in selection.items:
        return None
    started = time.monotonic()
    try:
        reading, evidence = preflight_module.run_realtime_probe(
            machine, journal, path=machine.path_of_login_shell() or ""
        )
    except Exception as unrun:  # noqa: BLE001 - every way it can fail is one row
        # A probe that could not be *started* is still a probe that returned no
        # frames, and this fixture may not raise: the lanes do not depend on the
        # realtime backend and a raise here would stop them (§7). So the failure
        # becomes the row's own evidence rather than the run's traceback.
        reading, evidence = None, journal("probe.unrun", error=repr(unrun))
    verdict.record(
        Item.PROBE,
        support.PASS if reading is not None and reading.passed else support.FAIL,
        evidence,
        seconds=time.monotonic() - started,
    )
    return reading
