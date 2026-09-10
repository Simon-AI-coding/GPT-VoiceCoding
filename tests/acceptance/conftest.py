"""The run's options and its fixtures (#350).

**Responsibilities held here** (§9's `conftest.py`): the two selectors of §6, the
preflight of §5, and the fixtures that arrange a run — the run directory, the
journal, the verdict, the engine and the hand-started Session.

The deadlines §9 also parks here live in `deadlines.py` instead. A conftest is
loaded by pytest **by path** and is not an importable name
(`tests/test_layout.py`), so a constant kept here cannot be read by the fast
suite — and every deadline in this harness is pinned by a fast test. The
selection is in `items.py` for the same reason.

What is **not** here: the concurrency of §4 and the walk of §2. Both are #352's
and #353's, and both are named below so those tickets have somewhere to land.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import items
import journey
import pytest
import support

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
) -> Iterator[support.Verdict]:
    """The one artifact a reader needs — written however the run ends (§7).

    Written from a teardown rather than by whatever finished last, because a run
    that raised is exactly the one whose verdict is worth having.
    """
    written = support.Verdict(
        run_id=run_directory.name,
        selection=selection,
        lanes=selected_lanes,
        journal=journal,
        commit=commit,
    )
    started = time.monotonic()
    try:
        yield written
    finally:
        written.seconds["run"] = time.monotonic() - started
        written.write(run_directory / support.VERDICT_NAME)


@pytest.fixture(scope="session")
def preflight(verdict: support.Verdict) -> object:
    """§5's refusals, every one a read and never a turn.

    **#351's.** Any failure is REFUSED: the run exits non-zero, writes a valid
    `verdict.json` naming the refusal (`support.Verdict.refuse`), and never
    starts an engine.
    """
    raise NotImplementedError("preflight is #351's")


@pytest.fixture(scope="session")
def lane_runs(preflight: object, probe_run: object) -> object:
    """Both lanes, walking in parallel on one thread each (§4).

    Depends on `probe_run` because §2 puts both run-level checks **before any
    lane starts**, and collection order does not: `test_lanes.py` sorts ahead of
    `test_realtime_probe.py`, so without this the probe would run after the
    lanes it is supposed to precede. A red probe still does not stop them — it
    is FAIL for the run and the lanes walk on (§7), so `probe_run` answers
    rather than raising when the probe found nothing.

    The walking itself is **#352's** (the engine, the config directories, the
    trust, the Session) and **#353's** (the items). This fixture **joins** both
    lanes before it yields, so the tests that read the verdict read a finished
    one.
    """
    raise NotImplementedError("the lane runs are #352's")


@pytest.fixture(scope="session")
def probe_run(
    preflight: object, run_directory: Path, journal: support.Journal, verdict: support.Verdict
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
    raise NotImplementedError("the realtime probe run is #351's")
