"""The run directory, the journal, the verdict, and what a lane is arranged on (#350, #352).

**Responsibilities held here** (§9's `support.py`): the run directory of §7, the
places a run reads from — the bundle, its interpreter, the operator's real
config — the provenance of §5, the credential scan of §8, the journal every row
rests on, the derived config of §4.2, the lane's own engine, the `bridgectl`
runner, the trust of §4.1, the shared-daemon reading of §4.3, and `verdict.json`.

The one thing this module does **not** hold is a judgement: nothing here decides
an item. It arranges what an item is read on, and `journey` does the reading.

Two rules hold this module together:

* **A row's evidence is the journal line it rests on.** Not a sentence about it —
  a reference, `journal.jsonl:<line>`, that resolves. A verdict whose evidence is
  prose is a verdict nobody can audit, and nobody re-reads a green one to find
  out. `record` refuses anything else.
* **A run is judged on what it set out to observe.** `missing` is the difference
  between what the selection promised and what the run wrote, and `result` will
  not say PASS while any of it is outstanding (#73).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import deadlines
import hand_started
import items
from items import Item

from gpt_voicecoding import __version__
from gpt_voicecoding import config as engine_config
from gpt_voicecoding.adapters.agent.codex import discovery as codex_discovery
from gpt_voicecoding.adapters.agent.codex import shared_daemon as codex_shared_daemon
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.control_plane import client as control_plane_client
from gpt_voicecoding.installation import claude_hooks, codex_runtime
from gpt_voicecoding.seams.control_plane import Action, Request

# --- where a run lives -------------------------------------------------------

#: §7's home for every run's artifacts.
DEFAULT_ACCEPTANCE_ROOT = (
    Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance"
)

#: An override, so a test — or a second machine — can put runs somewhere else
#: without any call site deciding where a run lives.
ACCEPTANCE_ROOT_VARIABLE = "GPTVOICECODING_ACCEPTANCE_ROOT"

JOURNAL_NAME = "journal.jsonl"
VERDICT_NAME = "verdict.json"

#: The stamp format for a run id: a UTC timestamp, sortable, and legal in a path.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def located(variable: str, default: Path, environ: Mapping[str, str] | None = None) -> Path:
    """One place a location is resolved: the variable if it is set, else the default.

    Every location in this harness has the same shape — a default, an override,
    and no call site deciding where anything lives — and it was written out five
    times before this existed. Written once so a location that forgot to
    `expanduser` cannot be one of them.
    """
    values = os.environ if environ is None else environ
    override = values.get(variable)
    return Path(override).expanduser() if override else default


def acceptance_root(environ: Mapping[str, str] | None = None) -> Path:
    return located(ACCEPTANCE_ROOT_VARIABLE, DEFAULT_ACCEPTANCE_ROOT, environ)


def run_id(now: datetime | None = None) -> str:
    """A run's name: the UTC moment it started (§7)."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime(RUN_ID_FORMAT)


def new_run_directory(identifier: str | None = None) -> Path:
    """§7's directory, with the places both lanes write already there.

    The lanes run in parallel and both write into this tree, so the tree is made
    once, here, rather than by whichever lane arrives first.
    """
    directory = acceptance_root() / (identifier or run_id())
    for lane in items.LANES:
        (directory / f"engine-{lane}").mkdir(parents=True, exist_ok=True)
        (directory / f"workspace-{lane}").mkdir(parents=True, exist_ok=True)
    return directory


# --- where a run reads from -------------------------------------------------

#: The bundle under test. A location, overridable, because a run against a
#: side-by-side install is a legitimate thing to want and hard-coding
#: `/Applications` would make it impossible.
BUNDLE_VARIABLE = "GPTVOICECODING_ACCEPTANCE_BUNDLE"
DEFAULT_BUNDLE = Path("/Applications/GPT-VoiceCoding.app")

#: The engine's real configuration, the one a lane derives its own from (§4.2).
SOURCE_CONFIG_VARIABLE = "GPTVOICECODING_ACCEPTANCE_SOURCE_CONFIG"

#: Where inside the bundle the engine's own interpreter and CLI sit.
ENGINE_PARTS = ("Contents", "Resources", "engine")


def bundle_path(environ: Mapping[str, str] | None = None) -> Path:
    return located(BUNDLE_VARIABLE, DEFAULT_BUNDLE, environ)


def bundled_python(bundle: Path | None = None) -> Path:
    """The interpreter §2 item 0b runs the probe on — `aiortc` and `av` are here."""
    return (bundle or bundle_path()).joinpath(*ENGINE_PARTS, "bin", "python3")


def bundled_package(bundle: Path | None = None) -> Path | None:
    """The `gpt_voicecoding` the bundle actually installed, or nothing at all."""
    library = (bundle or bundle_path()).joinpath(*ENGINE_PARTS, "lib")
    return next(library.glob("python*/site-packages/gpt_voicecoding"), None)


def source_config_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    engine = (
        (home or Path.home()) / "Library" / "Application Support" / "GPT-VoiceCoding" / "engine"
    )
    return located(SOURCE_CONFIG_VARIABLE, engine / "config.toml", environ)


# --- provenance --------------------------------------------------------------


#: How many differences a refusal quotes before it stops listing them. Enough to
#: recognise what moved, short enough that a bundle built from another branch
#: does not put a thousand file names on the terminal.
QUOTED_DIFFERENCES = 5


@dataclass(frozen=True)
class Provenance:
    """Whether the installed bundle is the tree this run is being asked to accept.

    §5 refuses on this, and the reason is that nothing else in a run says which
    build it graded: a verdict names a commit, and a bundle that is not that
    commit makes the naming a lie rather than a mistake.
    """

    bundle: Path
    commit: str
    matches: bool
    differences: tuple[str, ...]

    @property
    def reason(self) -> str:
        """What this bundle is, said either way — the green branch is journalled.

        A run's `preflight.passed` line carries it, so `verdict.json`'s commit is
        readable beside the sentence that checked it rather than beside nothing.
        """
        if self.matches:
            return f"the bundle's engine is byte-identical to {self.commit}"
        listed = ", ".join(self.differences[:QUOTED_DIFFERENCES])
        return (
            f"the bundle's engine differs from the working tree at {self.commit}: {listed}"
            f"{' …' if len(self.differences) > QUOTED_DIFFERENCES else ''}"
        )


def compare_engine_to_tree(bundle: Path, repository: Path) -> Provenance:
    """`diff -r` the bundle's installed package against `src/`, as `docs/app-bundle.md` does.

    Only the project's own package is compared. The interpreter and the locked
    wheels beneath it are what the signature and the lock cover; what a run has to
    know is that the *product* inside the `.app` is the product in this checkout.
    """
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    installed = bundled_package(bundle)
    if installed is None:
        return Provenance(bundle, commit, False, ("no gpt_voicecoding package inside the bundle",))
    differences = tuple(_differing(repository / "src" / "gpt_voicecoding", installed))
    return Provenance(bundle, commit, not differences, differences)


def _differing(tree: Path, installed: Path) -> Iterator[str]:
    """Every `.py` the two trees do not agree on, named relative to the package.

    Compared by content rather than by `filecmp.cmp`'s default shallow reading:
    an installed copy has its own mtime and size can collide, and "the bundle is
    this checkout" is a claim about bytes.
    """
    ours = {path.relative_to(tree) for path in tree.rglob("*.py")}
    theirs = {path.relative_to(installed) for path in installed.rglob("*.py")}
    for missing in sorted(ours - theirs):
        yield f"{missing} is not in the bundle"
    for extra in sorted(theirs - ours):
        yield f"{extra} is in the bundle and not in this checkout"
    for shared in sorted(ours & theirs):
        if (tree / shared).read_bytes() != (installed / shared).read_bytes():
            yield f"{shared} differs"


def secrets_of(
    environ: Mapping[str, str],
    *,
    token_variables: Sequence[str],
    api_hash: str | None,
) -> tuple[str, ...]:
    """Every value the end-of-run scan looks for (§8, the deferred item of #351).

    Both lanes' bot tokens, read out of the variables the engine is *told* to
    read them from, and the Telegram user account's `api_hash`.

    **The `api_id` is deliberately not here.** It is a short integer, and a
    substring reading of one matches any number a journal happens to carry — a
    message id, a pid, a count — so scanning for it would turn a clean run red
    for a reason that is not a leak. The rule §8 states is about a value reaching
    an artifact; a reading that cannot tell the value apart from the run's own
    facts is not a reading of that rule.
    """
    values = [environ.get(name, "") for name in token_variables]
    values.append(api_hash or "")
    return tuple(dict.fromkeys(one for one in values if one))


def scan_for_credentials(directory: Path, secrets: Sequence[str]) -> tuple[str, ...]:
    """Every file under `directory` carrying one of these secrets (§8).

    The rule is that a bot token, an `api_id` or an `api_hash` reaches no
    artifact — not a derived config, not the journal, not the verdict, nothing
    under the run directory. A rule about what is **absent** is only as good as
    the reading that looks for it, so this is that reading, and
    `tests/test_harness_preflight.py` runs it over a fake run's whole tree.

    Here rather than in `preflight`, because its subject is the run directory —
    which is this module's — and not the machine, which is preflight's.
    """
    wanted = [one for one in secrets if one]
    found: list[str] = []
    for path in sorted(one for one in directory.rglob("*") if one.is_file()):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if any(secret in text for secret in wanted):
            found.append(f"{path.relative_to(directory)} carries a credential")
    return tuple(found)


# --- the journal -------------------------------------------------------------


class NotAJournalReference(ValueError):
    """Something that is not `journal.jsonl:<line>` was offered as evidence."""


class NoSuchJournalLine(LookupError):
    """A reference pointed at a line the journal does not have."""


class RowAlreadyWritten(RuntimeError):
    """A second row was offered for an item that already has one on that axis."""


class NoSuchLane(LookupError):
    """A row was offered for a lane this run never selected."""


class WrongAxis(ValueError):
    """A run-level check was offered a lane, or a lane item was offered none."""


#: What a reference looks like: a file beside the verdict, and a 1-based line.
REFERENCE = re.compile(r"^(?P<name>[\w.-]+\.jsonl):(?P<line>[1-9]\d*)$")


def _lines_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _line_of(path: Path, number: int) -> dict[str, Any]:
    lines = _lines_of(path)
    if not 1 <= number <= len(lines):
        raise NoSuchJournalLine(f"{path.name} has no line {number}")
    return lines[number - 1]


@dataclass
class Turn:
    """One real agent turn, and the journal line it will be recorded on.

    `reference` is `None` until the turn ends, because a turn's line carries its
    seconds and there are none until then.
    """

    name: str
    reference: str | None = None


class Journal:
    """One JSON line per harness event, in the order the run produced them.

    Locked because two lanes and the engine's own reader thread all write here,
    and a half-written line is worse than a missing one: this file is the
    evidence every verdict row points at.

    The API is small on purpose — #351, #352 and #353 call `__call__` and `turn`,
    and read nothing. Writing answers the reference the verdict records.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        # Counted from the file rather than started at zero: preflight writes
        # here before the verdict exists, and a second reader that renumbered
        # from one would hand out references to the wrong lines.
        self._lines = len(_lines_of(path))

    def __call__(self, event: str, **fields: Any) -> str:
        """Write one event; answer the reference a verdict row can rest on."""
        line = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self._lock, self.path.open("a") as sink:
            sink.write(json.dumps(line, default=str) + "\n")
            self._lines += 1
            return f"{self.path.name}:{self._lines}"

    @contextmanager
    def turn(
        self,
        name: str,
        *,
        now: Callable[[], float] = time.monotonic,
        **fields: Any,
    ) -> Iterator[Turn]:
        """A real agent turn, recorded with its seconds however it ends (§7).

        The seconds are the point: the five-minute figure is re-measured by every
        run rather than asserted, and a turn that raised is exactly the one whose
        duration a reader wants.
        """
        turn = Turn(name=name)
        started = now()
        ended = "returned"
        try:
            yield turn
        except BaseException:
            ended = "raised"
            raise
        finally:
            turn.reference = self("turn", turn=name, seconds=now() - started, ended=ended, **fields)

    def read(self) -> list[dict[str, Any]]:
        return _lines_of(self.path)

    def line(self, number: int) -> dict[str, Any]:
        return _line_of(self.path, number)

    def expired(self, hit: deadlines.DeadlineExpired, **fields: Any) -> str:
        """Record a deadline that ran out, so the FAIL row can point at it.

        §7: a deadline hit is FAIL with the verdict naming what never ended. The
        naming happens here, once, and the row carries the reference.
        """
        return self(
            "deadline.expired",
            what=hit.what,
            deadline=hit.deadline,
            seconds=hit.seconds,
            **fields,
        )


def resolve(run_directory: Path, reference: str) -> dict[str, Any]:
    """Read the journal line a row's evidence names.

    This is what makes `verdict.json` checkable by anything other than a reader's
    goodwill — and `tests/test_harness_verdict.py` runs it over every row a
    written verdict carries.
    """
    named = REFERENCE.match(reference)
    if named is None:
        raise NotAJournalReference(
            f"{reference!r} is not a journal reference — evidence is the journal line a row "
            f"rests on, spelled '{JOURNAL_NAME}:<line>', never free text"
        )
    path = run_directory / named["name"]
    if not path.exists():
        raise NoSuchJournalLine(f"{path} is not there")
    return _line_of(path, int(named["line"]))


# --- the verdict -------------------------------------------------------------


class Result(StrEnum):
    """The closed set a row's verdict comes from (§7), and it is closed for a reason.

    A fifth value is how "recorded but not graded" grows back — §1 rule 1 has no
    such class. A row that was not observed is `SKIPPED`, and `SKIPPED` is not a
    judgement: it means the row never ran, and the journal line it rests on says
    why.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    REFUSED = "REFUSED"
    SKIPPED = "SKIPPED"


PASS = Result.PASS
FAIL = Result.FAIL
REFUSED = Result.REFUSED
SKIPPED = Result.SKIPPED

#: REFUSED outranks FAIL outranks PASS (§7). `SKIPPED` ranks with `FAIL` because
#: a row the run never reached is not a row that passed — the run owed it.
_RANK: dict[Result, int] = {PASS: 0, SKIPPED: 1, FAIL: 1, REFUSED: 2}


def worst(results: Sequence[Result]) -> Result:
    """The one result a set of rows adds up to."""
    ranked = max((_RANK[Result(one)] for one in results), default=_RANK[PASS])
    return {0: PASS, 1: FAIL, 2: REFUSED}[ranked]


@dataclass
class Row:
    """One item's result, on one axis.

    The same shape in the `run` block and in a lane's, so a reader learns it once.
    `evidence` is the journal line this rests on, and it is `None` exactly when
    the row is ungraded — a setup row is how the run *reached* the item it
    promised, not a claim about the product, so it has nothing to point at.
    """

    item: Item
    verdict: Result
    graded: bool
    evidence: str | None
    seconds: float | None = None

    def document(self) -> dict[str, Any]:
        return {
            "item": str(self.item),
            "verdict": str(self.verdict),
            "graded": self.graded,
            "evidence": self.evidence,
            "seconds": self.seconds,
        }


class Verdict:
    """`verdict.json` — the one artifact a reader needs (§7).

    Two axes, one row shape: a `run` block with a row per run-level check, and a
    `lanes` block with a row per lane per item. What decides the file is stated
    in `result`; what the run still owes is `missing`.

    Two lanes write here at once (§4), so every mutation is under one lock.
    `write` is not: it runs once, after both lanes have been joined.
    """

    def __init__(
        self,
        *,
        run_id: str,
        selection: items.Selection,
        lanes: Sequence[str],
        journal: Journal,
        bundle: str = "",
        commit: str = "",
        versions: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.selection = selection
        self.lanes = tuple(lanes)
        self.journal = journal
        self.bundle = bundle
        self.commit = commit
        self.versions = versions or {}
        #: The run's wall clock and each lane's (§7), keyed `run` and by lane.
        self.seconds: dict[str, float] = {}
        #: §8's rule, read at the end of the run rather than only pinned by a
        #: fast test: every artifact this run wrote, scanned for the credentials
        #: it was handed. `None` until the scan has run.
        self._credentials: tuple[str, ...] | None = None
        self._credentials_evidence: str | None = None
        self._run: list[Row] = []
        self._rows: dict[str, list[Row]] = {lane: [] for lane in self.lanes}
        self._lock = threading.Lock()

    # -- writing rows ---------------------------------------------------------

    def record(
        self,
        item: Item,
        result: Result,
        evidence: str,
        *,
        lane: str | None = None,
        seconds: float | None = None,
    ) -> Row:
        """One row, resting on one journal line.

        `evidence` must be a reference `Journal.__call__` handed back, and the
        line it names must **be there**: a reference whose shape is right and
        whose line does not exist reads as evidence and resolves to nothing,
        which is the one thing this rule exists to prevent. Checked when the row
        is written, not when a reader tries.
        """
        self._resolved(evidence)
        graded = self.selection.graded(item)
        row = Row(
            item=item,
            verdict=Result(result),
            graded=graded,
            evidence=evidence if graded else None,
            seconds=seconds,
        )
        return self._add(row, lane)

    def skip(self, item: Item, why: str, *, lane: str | None = None) -> Row:
        """A row that never ran, and the reason (§7).

        `why` is one of the two forms the spec names — `blocked by <row>` or
        `setup for <item>` — and it is written to the journal, so a graded
        SKIPPED row carries a reference to the sentence rather than the sentence.
        """
        reference = self.journal("item.skipped", item=str(item), lane=lane, why=why)
        graded = self.selection.graded(item)
        return self._add(
            Row(
                item=item,
                verdict=SKIPPED,
                graded=graded,
                evidence=reference if graded else None,
            ),
            lane,
        )

    def scanned(self, artifacts: Sequence[str], evidence: str) -> tuple[str, ...]:
        """What the end-of-run credential scan found, and the line it rests on (§8).

        Not a row: §8's rule is about the run's whole tree rather than about one
        item, and the closed set of items is a contract build tickets cite. It
        decides the file all the same — a run that wrote a credential into an
        artifact does not report PASS — and it decides it as **FAIL**: REFUSED is
        preflight's word for a run that declined to observe anything, and this
        run observed everything it promised and then left a token behind.

        The artifact keeps its credential: a reader needs the file to see what
        leaked, and a harness that deleted the evidence of its own defect would
        be the last thing to report it.
        """
        self._resolved(evidence)
        with self._lock:
            self._credentials = tuple(artifacts)
            self._credentials_evidence = evidence
        return tuple(artifacts)

    def refuse(self, reason: str) -> Row:
        """Preflight refused: the run declines to observe anything (§5, §7).

        Every lane item this run promised becomes a SKIPPED row rather than a
        missing one, so the file says what was owed and why it is not there.
        """
        reference = self.journal("preflight.refused", why=reason)
        refused = self._add(
            Row(
                item=Item.PREFLIGHT,
                verdict=REFUSED,
                graded=self.selection.graded(Item.PREFLIGHT),
                evidence=reference,
            ),
            None,
        )
        for lane in self.lanes:
            for item in self.selection.items:
                if item in items.LANE_ITEMS:
                    self.skip(item, f"blocked by run/{Item.PREFLIGHT}", lane=lane)
        return refused

    def _resolved(self, evidence: str) -> dict[str, Any]:
        """The journal line this evidence names, or the reason it is not one."""
        named = REFERENCE.match(evidence)
        if named is None or named["name"] != self.journal.path.name:
            raise NotAJournalReference(
                f"{evidence!r} is not a reference into this run's journal — a row's evidence "
                f"is the line it rests on, spelled '{self.journal.path.name}:<line>'"
            )
        return self.journal.line(int(named["line"]))

    def _add(self, row: Row, lane: str | None) -> Row:
        """File one row on one axis, and refuse anything that is not one.

        Three refusals, each of which was once a silent wrong answer: a lane
        name nobody selected grew a lane in the file that never ran; an item
        filed on the wrong axis put a per-lane fact in the run block; and a
        second row for an item left `verdict.json` saying two things about it,
        with a reader's eye deciding which.
        """
        run_level = row.item in items.RUN_ITEMS
        if run_level and lane is not None:
            raise WrongAxis(f"{row.item!s} is a run-level check and has no lane; got {lane!r}")
        if not run_level and lane is None:
            raise WrongAxis(f"{row.item!s} is a per-lane item and needs the lane it was read on")
        if lane is not None and lane not in self._rows:
            raise NoSuchLane(
                f"{lane!r} is not a lane this run selected; they are: {', '.join(self.lanes)}"
            )
        with self._lock:
            written = self._run if lane is None else self._rows[lane]
            if any(one.item is row.item for one in written):
                where = "run" if lane is None else lane
                raise RowAlreadyWritten(f"{where}/{row.item!s} already has a row")
            written.append(row)
        return row

    def written(self, lane: str | None = None) -> set[Item]:
        """Every item this run has already recorded a row for, on one axis.

        Asked here rather than by re-reading `document()` at a call site: a
        caller that serialised the whole verdict to learn what one lane wrote
        was reaching past this object for something it holds, and both callers
        that did it wanted this one sentence.
        """
        with self._lock:
            return {row.item for row in (self._run if lane is None else self._rows.get(lane, []))}

    # -- what the file says ---------------------------------------------------

    @property
    def missing(self) -> tuple[str, ...]:
        """Every row the run promised and did not write, on both axes (§7).

        Without this, a lane that never ran contributes no rows at all, and a
        verdict made only of the surviving lane's greens says PASS for a run that
        observed half the product. That is the one failure mode a verdict file
        must not have, because it is the one nobody re-checks.
        """
        absent = [
            f"run/{item}"
            for item in self.selection.selected
            if item in items.RUN_ITEMS and item not in {row.item for row in self._run}
        ]
        for lane in self.lanes:
            written = {row.item for row in self._rows.get(lane, [])}
            absent.extend(
                f"{lane}/{item}"
                for item in self.selection.items
                if item in items.LANE_ITEMS and item not in written
            )
        return tuple(absent)

    @property
    def result(self) -> Result:
        """Decided by the graded rows, the refusals, and what is still owed.

        A **setup** row never decides the run: it is how the run reached the item
        it promised, not a claim about the product. It cannot hide a red either —
        a failed setup item blocks the lane and the item it was arranging is
        SKIPPED, which is not PASS.

        A **refusal** decides the run wherever it sits, graded or not: a red
        preflight is REFUSED for the whole run and no lane starts (§7), and that
        is true however few items the run was asked for.

        And §7's rule as #353 extends it: a run is PASS only when every graded
        row is PASS **and** the end-of-run credential scan is clean. An artifact
        carrying a token is not one item's failure — it is the run's.
        """
        rows = [*self._run, *(row for lane in self._rows.values() for row in lane)]
        if not rows:
            # Nothing was written at all: the run stopped before it could observe
            # anything, and a file that says PASS about nothing is the worst one.
            return REFUSED
        if any(row.verdict is REFUSED for row in rows):
            return REFUSED
        if self.missing:
            return FAIL
        if self._credentials:
            return FAIL
        return worst([row.verdict for row in rows if row.graded])

    def document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "result": str(self.result),
            "missing": list(self.missing),
            "selection": {
                "selected": [str(item) for item in self.selection.selected],
                "setup": [str(item) for item in self.selection.setup],
                "lanes": list(self.lanes),
            },
            "bundle": self.bundle,
            "commit": self.commit,
            "versions": dict(self.versions),
            "seconds": dict(self.seconds),
            "credentials": {
                "scanned": self._credentials is not None,
                # `null` until the scan has run: a run whose secrets could not
                # even be assembled has not been found clean, and a reader who
                # sees `true` beside `scanned: false` reads the reassurance and
                # not the contradiction.
                "clean": None if self._credentials is None else not self._credentials,
                "artifacts": list(self._credentials or ()),
                "evidence": self._credentials_evidence,
            },
            "run": [row.document() for row in self._run],
            "lanes": {lane: [row.document() for row in rows] for lane, rows in self._rows.items()},
        }

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.document(), indent=2) + "\n")
        return path


def write_refusal(run_directory: Path, reason: str) -> Path:
    """The smallest honest `verdict.json`, for a run that refused before it had one.

    A refusal that reached only the terminal left a directory a reader could not
    interpret: a run id, maybe a journal, and no statement of why nothing else is
    there. This is that statement, in the same vocabulary as a full verdict, and
    it says plainly that the refusal preceded the facts a full one would carry.
    """
    journal = Journal(run_directory / JOURNAL_NAME)
    verdict = Verdict(
        run_id=run_directory.name,
        selection=items.select(),
        lanes=items.LANES,
        journal=journal,
    )
    verdict.refuse(reason)
    return verdict.write(run_directory / VERDICT_NAME)


# --- the derived config (§4.2) ----------------------------------------------

#: What a run writes beside its derived config, naming everything it dropped.
#: A drop is a change of kind rather than a redirection — the launcher tables
#: name the operator's own project directories — so it is said out loud in a
#: file rather than left for a reader to diff two configs for.
DROPPED_NAME = "config-dropped.json"
CONFIG_NAME = "config.toml"
STATE_NAME = "state.json"
LOG_NAME = "engine.log"


#: The two lanes by role rather than by index, and the lane name **is** the
#: agent kind the engine's own tables are keyed by (`tests/test_harness_lanes.py`
#: pins the two sets equal). A name this file invented would be a table the
#: engine refuses outright.
CLAUDE_LANE, CODEX_LANE = items.LANES


def settings_key(agent: str) -> str:
    """`[adapters.settings]` is keyed **flat**, by the seam names the engine builds.

    `agent.<kind>`, `call`, `companion_channel` (`config.py:260`) — not by nested
    tables. Measured on run `20260902T013222Z`, where a nested
    `[adapters.settings.agent.codex]` stopped **both** engines with "names no
    seam this engine fills".
    """
    return f"agent.{agent}"


#: Darwin caps an `AF_UNIX` path at 103 bytes and a run directory is 111 before
#: the socket's own name, so a lane's engine socket cannot live under the run
#: directory (§4.2 items 1 and 6; the amended criterion on #352). **Read off the
#: product rather than copied**: `config.RUNTIME_ROOT` is where the engine's own
#: socket goes for exactly this reason, and a second spelling of it here is a
#: harness that keeps binding where the product no longer does.
SOCKET_ROOT = engine_config.RUNTIME_ROOT


def socket_directory(run_id: str, lane: str, *, uid: int | None = None) -> Path:
    """Where this lane's two sockets live: the engine's, and its app-server's.

    One directory per lane per run — which is the separation §4.2 items 1 and 6
    ask for — named for all three so no two lanes and no two runs can meet in it.
    """
    identity = os.getuid() if uid is None else uid
    return SOCKET_ROOT / f"gvc-acceptance-{identity}-{run_id}-{lane}"


#: Why a lane's sockets are not under its run directory, said in the journal
#: rather than only in this comment: the run directory is where a person looks,
#: and this is the one thing it cannot show them.
SOCKETS_ELSEWHERE = (
    "Darwin caps an AF_UNIX path at 103 bytes, which the run directory cannot hold "
    "(the control socket alone would be 111 there and the engine's own app-server "
    "socket 140), so this lane's sockets live here — one directory per lane per run, "
    "0700, removed when the lane is done with it"
)


@contextmanager
def lane_sockets(run_id: str, lane: str, journal: Journal) -> Iterator[Path]:
    """This lane's socket directory, made 0700 and taken away afterwards.

    A context manager rather than two lines at a call site because the making
    and the removal are one arrangement: a run that left one behind would leave
    a socket file the *next* run's engine finds and refuses to start on, which
    is the failure §4.2 item 6 exists to prevent, arriving a run later.

    Owner-only, because a socket anybody may connect to is a control plane
    anybody may drive.
    """
    directory = socket_directory(run_id, lane)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    journal("sockets.made", lane=lane, directory=str(directory), why=SOCKETS_ELSEWHERE)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        journal("sockets.removed", lane=lane, directory=str(directory))


@dataclass(frozen=True)
class DerivedConfig:
    """One lane's own configuration, and the run facts that travel with it.

    The paths are answered rather than re-derived by every caller: the engine
    reads its log, `roster` reads the workspace, and the journal names the file
    each row's evidence came out of — three readers of one arrangement, and a
    fourth spelling of `engine-<lane>/engine.log` is how they come to disagree.
    """

    path: Path
    socket_path: Path
    state_path: Path
    log_path: Path
    workspace: Path
    token_variable: str
    chat_id: str
    #: Every table and key this run dropped, in the order they were dropped.
    dropped: tuple[str, ...]


def derive_config(
    *,
    source: Path,
    engine_directory: Path,
    workspace: Path,
    socket_path: Path,
    token_variable: str,
    codex_socket_directory: Path,
    delegate_model: str,
    dropped_agents: Sequence[str] = (),
) -> DerivedConfig:
    """The operator's real `config.toml` with §4.2's separations replaced.

    **Every value is copied**, because the point of the run is to accept the
    engine the operator actually configured. What is replaced is what a second
    engine would otherwise fight the first one over, and each replacement is the
    *shipped mechanism* doing what it is for rather than a reach past it:

    * the **socket**, the **state** and the **log** (§4.2 items 1–3), so two
      engines do not share one of each;
    * `token_env` (item 4) — the engine is *told* which variable holds its
      token, so a lane's bot is its own and the token never lands in a file;
    * `[adapters.settings."agent.codex"] socket_directory` (item 6), which is
      keyed per **machine** by default: the product refuses rather than shadows
      a socket something is still listening on, and lane two's engine died at
      start on exactly that collision (run `20260902T012313Z`);
    * `[delegate] model` (§3), unconditionally, so no path through this function
      leaves a run billing whatever the operator last used.

    Item 5 — one private chat per bot — needs no key: a Telegram private chat is
    addressed by the *account's* id, which is the same whichever bot is talking,
    so the separation is carried entirely by item 4. Item 7's workspace and item
    8's config directory are not configuration at all; they are the launch's.

    **The launcher tables are dropped**, which is a change of kind and so is
    said out loud in `config-dropped.json`: `config.of` reads none of them, and
    inert is not harmless here — the operator's real `[[launch.projects]]` names
    a dozen of their actual project directories, and a run config that names
    them is one that could point an agent at one.

    **And one entry the operator's real config has is dropped** with no setting
    behind it: `[adapters.agents]` loses every kind in `dropped_agents`, which
    is the Claude kind on the Codex lane (#202, §4.3). Its settings table goes
    with it, because `[adapters.settings]` is checked against the seams the
    engine actually built and a table for an adapter that is no longer listed is
    a key that "names no seam this engine fills" — the refusal that stopped
    **both** engines on run `20260902T013222Z`.
    """
    engine_directory.mkdir(parents=True, exist_ok=True)
    document = tomllib.loads(source.read_text())
    dropped: list[str] = []

    delegate = dict(document.get("delegate", {}))
    delegate["model"] = delegate_model
    document["delegate"] = delegate

    engine = dict(document.get("engine", {}))
    engine["socket_path"] = str(socket_path)
    engine["state_path"] = str(engine_directory / STATE_NAME)
    document["engine"] = engine

    log = dict(document.get("log", {}))
    log["path"] = str(engine_directory / LOG_NAME)
    document["log"] = log

    if document.pop("launch", None) is not None:
        dropped.append("launch")
    adapters = dict(document.get("adapters", {}))
    if adapters.pop("session_launcher", None) is not None:
        dropped.append("adapters.session_launcher")
    settings = dict(adapters.get("settings", {}))
    if settings.pop("session_launcher", None) is not None:
        dropped.append("adapters.settings.session_launcher")

    agents = dict(adapters.get("agents", {}))
    for kind in dropped_agents:
        if agents.pop(str(kind), None) is not None:
            dropped.append(f"adapters.agents.{kind}")
        if settings.pop(settings_key(str(kind)), None) is not None:
            dropped.append(f'adapters.settings."{settings_key(str(kind))}"')
    adapters["agents"] = agents

    channel = dict(settings.get("companion_channel", {}))
    channel["token_env"] = token_variable
    settings["companion_channel"] = channel

    codex = dict(settings.get(settings_key(CODEX_LANE), {}))
    codex["socket_directory"] = str(codex_socket_directory)
    settings[settings_key(CODEX_LANE)] = codex

    adapters["settings"] = settings
    document["adapters"] = adapters

    path = engine_directory / CONFIG_NAME
    path.write_text(as_toml(document))
    (engine_directory / DROPPED_NAME).write_text(json.dumps(dropped, indent=2) + "\n")
    return DerivedConfig(
        path=path,
        socket_path=socket_path,
        state_path=Path(engine["state_path"]),
        log_path=Path(log["path"]),
        workspace=workspace,
        token_variable=token_variable,
        chat_id=str(channel.get("chat_id", "")),
        dropped=tuple(dropped),
    )


def fresh_workspace(workspace: Path, path_value: str) -> Path:
    """§4.2 item 7: a disposable `git init` directory, one per lane.

    A repository rather than a bare directory because that is what both agents
    are ordinarily started in, and because the trust grant's subject is a
    project — a run in a directory neither agent has ever seen is a run that
    stops at a full-screen dialog and never registers (§4.1).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        check=True,
        capture_output=True,
        env={**os.environ, "PATH": path_value},
    )
    return workspace


def as_toml(document: Mapping[str, Any]) -> str:
    """The smallest TOML writer that covers this document, and it says so.

    Python ships a TOML *reader* and no writer, and this harness will not take a
    dependency to emit four tables. What it covers is exactly the shape
    `docs/control-plane.md` documents: tables, arrays of tables, strings,
    numbers, booleans and string arrays. A key it cannot render **raises**
    rather than being silently dropped — a derived config missing a key the
    operator set would make the run accept an engine they are not running.
    """
    lines: list[str] = []
    _emit_table(dict(document), (), lines)
    return "\n".join(lines) + "\n"


def _emit_table(table: dict[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = {key: value for key, value in table.items() if not _is_table_like(value)}
    if prefix:
        lines.append(f"[{'.'.join(_quoted(part) for part in prefix)}]")
    for key, value in scalars.items():
        lines.append(f"{_quoted(key)} = {_as_value(value)}")
    if prefix or scalars:
        lines.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _emit_table(value, (*prefix, key), lines)
        elif _is_array_of_tables(value):
            for entry in value:
                lines.append(f"[[{'.'.join(_quoted(part) for part in (*prefix, key))}]]")
                for inner_key, inner_value in entry.items():
                    lines.append(f"{_quoted(inner_key)} = {_as_value(inner_value)}")
                lines.append("")


def _is_table_like(value: Any) -> bool:
    return isinstance(value, dict) or _is_array_of_tables(value)


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(one, dict) for one in value)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _quoted(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _as_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_as_value(one) for one in value) + "]"
    raise TypeError(
        f"the acceptance's TOML writer cannot render {type(value).__name__} — a key the "
        f"operator set would be silently dropped from the derived config: {value!r}"
    )


# --- the lane's own engine --------------------------------------------------


class EngineRefused(RuntimeError):
    """The engine under test never came up, and the message says where to look."""


class Engine:
    """The bundle's own interpreter, running the bundle's own engine (§2, §4.2).

    Spawned by the harness rather than by the menu-bar shell: repeatability over
    coverage, and the shell is out of scope. What the shell *does* contribute —
    the login shell's PATH — is reproduced here, because without it the engine
    finds neither agent.
    """

    def __init__(
        self,
        *,
        config: DerivedConfig,
        bundle: Path,
        journal: Journal,
        token: str,
        path_value: str,
        base: Mapping[str, str] | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._bundle = bundle
        self._journal = journal
        self._token = token
        self._path_value = path_value
        #: What the engine's environment is built on. This process's own, unless
        #: a caller states one — which only a test does, so the rules below can
        #: be read without writing into the environment of whoever runs pytest.
        self._base = dict(os.environ if base is None else base)
        #: What this lane adds: `CLAUDE_CONFIG_DIR` on the Claude lane (§4.1) —
        #: the engine's `claude agents --json` inherits the engine process's
        #: environment, so the engine has to carry the variable too.
        self._extra = dict(extra or {})
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def log_path(self) -> Path:
        return self._config.log_path

    @property
    def environment(self) -> dict[str, str]:
        """The real HOME, the login shell's PATH, and the token by its own name.

        `HOME` is the operator's own because the real `claude` keeps its login
        there, and an engine given a temporary one would launch an agent nobody
        has logged in as. **Extended, never replaced** (§4.4): what the operator
        runs is what reaches the engine, and dropping an entry would be the
        harness quietly changing the thing it is accepting.
        """
        environment = dict(self._base)
        environment["PATH"] = hand_started.extended_path(
            self._path_value, environment.get("PATH", "")
        )
        environment[self._config.token_variable] = self._token
        environment.update(self._extra)
        return environment

    def start(self) -> None:
        command = [
            str(bundled_python(self._bundle)),
            "-m",
            "gpt_voicecoding.engine",
            "--config",
            str(self._config.path),
        ]
        self._journal("engine.start", command=command, socket=str(self._config.socket_path))
        # **Not redirected**, and that is ADR 0004 rather than an oversight: the
        # engine owns its log, and a harness that took the descriptor here would
        # be the shell redirect the ADR exists to remove. The engine `dup2`s its
        # configured log onto its own stdout and stderr, so a redirect here would
        # only ever catch the moments before that — which the ADR accounts for:
        # "Output before adoption is discarded — an engine dying that early is
        # surfaced by silence on the socket." That silence is `EngineRefused`.
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            env=self.environment,
        )
        try:
            deadlines.wait(
                "ENGINE_START_SECONDS",
                self._listening,
                what=f"the {self._config.socket_path.name} this lane's engine binds",
            )
        except (deadlines.DeadlineExpired, EngineRefused) as unstarted:
            # **A refusal takes the engine with it.** Without this the process
            # goes on starting after the lane has given up on it, and the next
            # run meets a second bridge over every Session on this machine: run
            # `20260904T002514Z` refused and left two engines holding the
            # published approval address.
            self.stop()
            raise EngineRefused(
                f"{unstarted}; this lane's engine log is at {self._config.log_path} "
                f"(ADR 0004: output before the engine adopts its own log is discarded, and "
                f"this silence is how that surfaces)"
            ) from None
        self._journal("engine.listening", socket=str(self._config.socket_path))

    def _listening(self) -> bool:
        assert self._process is not None
        if self._config.socket_path.exists():
            return True
        if self._process.poll() is not None:
            raise EngineRefused(
                f"the engine exited {self._process.returncode} before binding its socket"
            )
        return False

    def stop(self) -> None:
        """Ask it to go, then insist — inside the deadline this run named for it (§7)."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=deadlines.ENGINE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._journal("engine.killed", reason="did not exit on SIGTERM")
            process.kill()
            process.wait(timeout=deadlines.ENGINE_STOP_SECONDS)
        self._journal("engine.stopped", returncode=process.returncode)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def log_lines(self) -> list[str]:
        """The engine's own log, as lines. The **mark** every chat read starts from.

        §2 reads each of this run's messages by the id the product issued, and
        the id is in the line the engine wrote when it sent it
        (`journey.ENGINE_SENT_LINE`). So a *mark* is a position in this list,
        taken before the turn whose message the run is about to read — never a
        clock, and never a search of the chat (#109).
        """
        if not self._config.log_path.exists():
            return []
        return self._config.log_path.read_text(errors="replace").splitlines()


# --- the surface ------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """One `bridgectl` call and what came back."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Exit 0 — the engine answered and did not refuse (`bridgectl` § Three exits)."""
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip() if self.ok else self.stderr.strip()


class Bridgectl:
    """Every product action the run takes, through the bundle's own `bridgectl`.

    §2: "Every product action goes through `bridgectl` against that engine", and
    against **this lane's** engine — which is what `--socket` says, since a run
    has two of them up at once and the CLI would otherwise read the operator's
    own configuration to find one.

    **And one read that is not an action.** §2 item 1 matches a roster row by
    `session_id` where the agent has one and by `pid` otherwise, and neither
    reaches the rendered line: `_status_roster_lines` writes a name, an address,
    a workspace, a state and a window (`control_plane/commands.py:341`). So
    `status_payload` asks the same request with the rendering left off — the
    document `bridgectl` itself receives — and journals it as what it is. It is
    not a private route into the engine: `bridgectl` is `ask` plus `render`, and
    this is `ask`.
    """

    def __init__(self, *, bundle: Path, socket_path: Path, journal: Journal) -> None:
        self._executable = bundled_bridgectl(bundle)
        self._socket = socket_path
        self._journal = journal

    def __call__(self, *arguments: str, deadline: str | None = None) -> Answer:
        """One action, and the journal line it leaves behind.

        `deadline` names one of `deadlines.DEADLINES` and is passed to the CLI
        as its own `--timeout`; left out, the CLI picks the deadline the action
        carries, which is the behaviour the run is accepting. The budget here is
        that plus `SURFACE_GRACE_SECONDS`, so a timeout in this process always
        means the *surface* hung and never that the action was merely slow.
        """
        budget = deadlines.DEADLINES[deadline] if deadline else None
        stated = ["--timeout", f"{budget:.1f}"] if budget is not None else []
        argv = [str(self._executable), "--socket", str(self._socket), *stated, *arguments]
        finished = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=(budget or deadlines.DEFAULT_SURFACE_SECONDS) + deadlines.SURFACE_GRACE_SECONDS,
            check=False,
        )
        answer = Answer(tuple(argv), finished.returncode, finished.stdout, finished.stderr)
        self._journal(
            "bridgectl",
            command=list(arguments),
            returncode=answer.returncode,
            stdout=answer.stdout.strip(),
            stderr=answer.stderr.strip(),
            deadline=deadline,
        )
        return answer

    def status_payload(self, *, why: str) -> dict[str, Any]:
        """`status`, as the document rather than as the lines it renders into.

        Journalled with the reason it was needed, so no verdict row can rest on
        a field no surface carries without the journal saying so.
        """
        reply = asyncio.run(
            control_plane_client.ask(
                Request(action=Action.STATUS, payload={}),
                path=self._socket,
                timeout=deadlines.DEFAULT_SURFACE_SECONDS,
            )
        )
        data = dict(reply.data)
        self._journal(
            "bridgectl.payload",
            action=Action.STATUS.value,
            why=why,
            sessions=len([one for one in data.get("sessions", []) if isinstance(one, dict)]),
        )
        return data


def bundled_bridgectl(bundle: Path | None = None) -> Path:
    """The CLI inside the bundle — the product's own surface, not this checkout's."""
    return (bundle or bundle_path()).joinpath(*ENGINE_PARTS, "bin", "bridgectl")


# --- the shared Codex daemon (§4.3, #232) -----------------------------------

#: What the daemon calls its own roster. Read from the product rather than
#: spelled again, so a method rename cannot leave this harness asking for
#: something nobody answers.
DAEMON_ROSTER_METHOD = codex_discovery.ROSTER_METHOD


@dataclass(frozen=True)
class DaemonMembership:
    """Whether the shared Codex daemon holds one thread, and how that was learned.

    **`held` is a tri-state and that is the whole design.** `True` and `False`
    are both observations — the daemon answered, and this thread was or was not
    among the ids it named. `None` is *no observation*, and the product's own
    rule says why it may not collapse into `False`: never claim anything about
    the daemon this build did not observe (#96). A daemon that is down, moved or
    answering a shape this run cannot read is not evidence that a thread is
    absent from it, and a lane refused on one of those would blame #232's own
    cause for somebody else's outage.
    """

    thread_id: str
    held: bool | None
    held_threads: tuple[str, ...] = ()
    daemon: str = ""
    reason: str = ""

    def refusal(self, flags: Sequence[str]) -> str | None:
        """Why this lane cannot be walked, or `None` — and only ever on an observed absence.

        **An absence refuses rather than reds.** ADR 0020 defines a Codex Session
        as a daemon thread a terminal vouches for, so a TUI outside the daemon is
        a Session the product is *right* not to list: grading it is grading the
        harness's own ground as a product defect, which is the reading run
        `20260904T202319Z` produced and #232 was opened to replace.

        The flags are **quoted rather than diagnosed**. A `-c` override is what
        was measured to keep a TUI out (2026-09-05) and is named as that
        measurement when it is there; when it is not, the sentence says what was
        launched and stops, because a stock explanation printed under a lane that
        cannot have this cause is how the next person loses an afternoon.
        """
        if self.held is not False:
            return None
        override = [flag for flag in flags if flag == "-c" or flag.startswith("-c")]
        measured = (
            " This lane was launched with a `-c` override, and a `-c` override was measured on "
            "2026-09-05 to make codex-tui run its own core instead of joining the shared daemon "
            "(#232) — which is exactly this."
            if override
            else " No `-c` override is in those flags, so this is not the cause #232 measured "
            "and the daemon-side reason is unknown to this run."
        )
        return (
            f"the Codex Session the harness hand-started writes thread {self.thread_id} into its "
            f"own rollout, and the shared daemon at {self.daemon} does not hold it — it holds "
            f"{list(self.held_threads) or 'no threads'}. A Codex Session is a daemon thread a "
            f"terminal vouches for (ADR 0020), so nothing the product does can give this TUI a "
            f"roster row, and every item of this lane reads one. Launched with "
            f"{list(flags)}.{measured}"
        )


def codex_daemon_membership(
    thread_id: str, *, control_socket: Path | None = None
) -> DaemonMembership:
    """Ask the shared Codex daemon whether it holds the thread this lane started.

    **The one fact that decides whether this lane can be walked at all.** The
    Codex roster composes a row from a daemon-held user thread plus a live
    terminal in its workspace, so a TUI whose thread the daemon does not hold is
    invisible to the product by construction — not under-reported, absent.

    **Asked the engine's own way, through the engine's own module.**
    `SharedDaemon` settles the derived control socket and becomes one more
    client of a server somebody else owns. A harness that implemented the wire
    again would be a second answer to a question the product already answers,
    and the product's answer is the one that decides whether a row appears.
    """
    if not thread_id.strip():
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            reason=(
                "the agent's own record names no thread yet, so there is nothing to look for "
                "in the daemon"
            ),
        )
    return asyncio.run(_codex_daemon_membership(thread_id.strip(), control_socket))


async def _codex_daemon_membership(thread_id: str, control_socket: Path | None) -> DaemonMembership:
    daemon = codex_shared_daemon.SharedDaemon(
        settings=CodexSettings(), version=__version__, control_socket=control_socket
    )
    where = str(control_socket or codex_shared_daemon.default_control_socket())
    try:
        connection = await daemon.client()
        if connection is None:
            return DaemonMembership(
                thread_id=thread_id, held=None, daemon=where, reason=daemon.note
            )
        answer = await connection.request(DAEMON_ROSTER_METHOD, {})
    except Exception as unread:  # noqa: BLE001 - every way this fails is one fact
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            daemon=where,
            reason=f"the shared Codex app-server at {where} could not be read: {unread!r}",
        )
    finally:
        await daemon.aclose()
    # **The four lines below are the product's `discovery._loaded_threads` shape
    # and not its code**, because the product's function answers a different
    # question: it follows every id with a `thread/read`, which was measured at
    # 558,875 bytes for a thread of two turns (`discovery.TurnCache`). This asks
    # the daemon what it holds and nothing else, on the machine-wide roster,
    # while a lane is starting. The method name is still the product's.
    listed = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(listed, list):
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            daemon=where,
            reason=(
                f"the shared Codex app-server at {where} answered {DAEMON_ROSTER_METHOD} in a "
                f"shape this run cannot read: {answer!r}"
            ),
        )
    held = tuple(one.strip() for one in listed if isinstance(one, str) and one.strip())
    return DaemonMembership(
        thread_id=thread_id,
        held=thread_id in held,
        held_threads=held,
        daemon=where,
        reason=f"read from {DAEMON_ROSTER_METHOD} at {where}",
    )


# --- trust (§4.1) -----------------------------------------------------------

#: Where `claude` records what it knows about a project, including whether the
#: trust dialog has been answered for it. Read by preflight for the account and
#: written by `TrustGate` for the grant — one spelling, because they are one
#: file, and #217 is what two spellings of it cost.
CLAUDE_STATE_NAME = ".claude.json"
CLAUDE_TRUST_KEY = "hasTrustDialogAccepted"

#: How `codex` records the same answer: `[projects."<path>"] trust_level`.
CODEX_TRUST_LEVEL = "trusted"

#: What a backup of the operator's own file is called: beside the file, named
#: for the run that took it, so a killed run leaves a breadcrumb rather than a
#: mystery and two runs cannot overwrite each other's pristine copy.
BACKUP_SUFFIX = "acceptance-backup"


class CouldNotReconcile(RuntimeError):
    """A trust row a killed run left behind could not be removed (§4.1, §5)."""


class NoConfigDirectory(LookupError):
    """A Claude trust grant was asked for with no config directory to write it in.

    §4.1 gives the Claude lane a **persistent** `CLAUDE_CONFIG_DIR` and the
    grant belongs in it. The old harness fell back to the *home* directory when
    the variable was unset, and that fallback is #217: run `20260903T050619Z`
    granted trust in `~/.claude.json`, journalled `trust.granted`, and still met
    the full-screen dialog, because the Session was reading a file the gate had
    never opened. There is no fallback now — an unset variable is a mistake at
    the call site, and this is it being said out loud.
    """


def claude_state_path(environment: Mapping[str, str]) -> Path:
    """The one file a Claude trust grant may land in: the lane's own state file.

    Measured on 2026-09-03 against `claude` 2.1.259, in a pty over a `git init`
    workspace under the acceptance root: with `CLAUDE_CONFIG_DIR` set, only an
    entry in `$CLAUDE_CONFIG_DIR/.claude.json` clears the dialog. The lane
    always sets it (§4.1), so that is the only case this harness has.
    """
    stated = environment.get(claude_hooks.CONFIG_DIRECTORY_VARIABLE, "")
    if not stated.strip():
        raise NoConfigDirectory(
            f"no {claude_hooks.CONFIG_DIRECTORY_VARIABLE} in the environment this Session "
            f"will be launched with, so there is no file a trust grant could land in that the "
            f"Session would read (§4.1, #217). The claude lane always exports it."
        )
    return claude_hooks.default_config_directory(environment) / CLAUDE_STATE_NAME


def codex_config_path(
    environment: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """The operator's own `config.toml`, resolved from `CODEX_HOME` else `~/.codex`.

    **Never a hardcoded path** — #219 is the bug this function exists to bury.
    Resolved through the product's own rule (`codex_runtime.default_codex_home`)
    so the harness and the engine cannot disagree about which directory is
    meant.
    """
    values = os.environ if environment is None else environment
    return codex_runtime.default_codex_home(values, home) / CONFIG_NAME


def codex_trust_block(workspace: str) -> str:
    """The one thing this harness writes into the operator's own `~/.codex`.

    Appended as one block and removed as the same block, so their file keeps its
    order, its comments and its formatting.
    """
    return f'\n[projects."{workspace}"]\ntrust_level = "{CODEX_TRUST_LEVEL}"\n'


def _trusted_projects(text: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    projects = document.get("projects")
    return tuple(projects) if isinstance(projects, dict) else ()


def _project_of(line: str) -> str | None:
    """The project a `[projects."<path>"]` header names, in **TOML's** reading of it.

    Asked of the parser rather than of the string, because a header has more
    than one legal spelling: `[projects.'…']` with single quotes, a trailing
    comment, whitespace inside the brackets. A matcher that only knew the
    spelling this harness writes would leave a row it had just reported as
    removed sitting in the operator's file.
    """
    if not line.lstrip().startswith("[") or line.lstrip().startswith("[["):
        return None
    try:
        header = tomllib.loads(f"{line}\n")
    except tomllib.TOMLDecodeError:
        return None
    projects = header.get("projects")
    if not isinstance(projects, dict) or len(projects) != 1 or len(header) != 1:
        return None
    return next(iter(projects))


def _without_project_rows(text: str, paths: Sequence[str]) -> str:
    """The file without those `[projects."<path>"]` tables, and nothing else changed.

    Line-based rather than a re-emit of the parsed document: the file is the
    operator's, and a writer that rendered it back would return their comments
    and their key order as this harness's idea of them.
    """
    wanted = set(paths)
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _project_of(line) in wanted:
            dropping = True
            # The blank line that separated this table from the one above goes
            # with it, so removing an appended block leaves the file as it was.
            while kept and not kept[-1].strip():
                kept.pop()
            continue
        if dropping:
            if stripped.startswith("["):
                dropping = False
            else:
                continue
        kept.append(line)
    return "".join(kept)


def reconcile_codex_trust(
    acceptance_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    taken_by: str | None = None,
) -> tuple[str, ...]:
    """Remove any Codex trust row left behind for an acceptance workspace (§4.1).

    §5's one row that is **not** a refusal: a killed run can leave a
    `[projects."<workspace>"]` row in the operator's own `config.toml` for a
    workspace under the acceptance root, and a leftover is arranged away before
    the lane starts rather than graded. Answers the workspaces it removed, so
    preflight journals `trust.reconciled` with them.

    Rows are recognised **by place**: a project path under the acceptance run
    directory is one of this harness's, because every run directory and every
    lane workspace is made there. Every other row is the operator's and is not
    read again. Run before any lane starts and from one thread, so there is
    nothing to serialise against — a second *run* is refused by §5's session
    lock, and a thread lock would say nothing about it anyway (#182).
    """
    path = codex_config_path(environment, home)
    if not path.exists():
        return ()
    text = path.read_text()
    owned = acceptance_root.expanduser().resolve(strict=False)
    stale = [
        project
        for project in _trusted_projects(text)
        if Path(project).expanduser().resolve(strict=False).is_relative_to(owned)
    ]
    if not stale:
        return ()
    backup = path.with_name(f"{path.name}.{BACKUP_SUFFIX}-{taken_by or run_id()}-reconcile")
    shutil.copy2(path, backup)
    rewritten = _without_project_rows(text, stale)
    path.write_text(rewritten)
    #: **Verified rather than assumed.** The rows are *found* by parsing and
    #: *removed* by matching header lines, and a header this harness cannot
    #: match — a spelling `tomllib` accepts and `_project_of` does not — would
    #: otherwise be reported as reconciled while still sitting in the operator's
    #: file. A run that then walked would grade a Session in a workspace
    #: somebody else's row had already trusted.
    survivors = tuple(one for one in stale if one in _trusted_projects(rewritten))
    if survivors:
        # The backup **stays**: it is now the only copy of their file from
        # before this run touched it.
        raise CouldNotReconcile(
            f"{path} still carries a trust row for {list(survivors)} after the rewrite — a "
            f"killed acceptance run left a row this harness cannot remove, and a run that "
            f"walked past it would grade a Session in a workspace it did not trust itself. "
            f"The file as it was is beside it at {backup.name}."
        )
    # And on the verified path it goes, for the reason it was taken: §4 says
    # nothing a lane writes lands in the operator's own agent configuration, and
    # a backup per run that reconciles anything is a pile of files in their
    # directory that nothing ever removes. What the removal *was* is in the
    # journal, which is where a run's record belongs. `TrustGate` does the same.
    backup.unlink(missing_ok=True)
    return tuple(stale)


class TrustGate:
    """One disposable workspace, trusted the way the lane's own agent records it (§4.1).

    **Why this is arranged and never graded.** A launch into a directory the
    agent has never seen stops at a full-screen "Is this a project you created
    or one you trust?" and the Session never registers (re-measured on `claude`
    2.1.259, 2026-09-03; the same gate #18 reports for Codex). A fresh workspace
    is what §4.2 item 7 requires, so every run would end at `roster` for a
    reason that is real but singular. The run cannot both arrange this and judge
    it.

    **The two lanes write two different files, and that is §4.1's whole point.**

    * The **Claude lane** writes into its own persistent `CLAUDE_CONFIG_DIR`,
      which is the harness's directory and not the operator's: there is nothing
      of theirs to back up, nothing to revoke, and nothing to reconcile after a
      killed run. `~/.claude.json` and `~/.claude/` are not reachable from here
      at all (`claude_state_path`).
    * The **Codex lane** writes one row into the operator's real `config.toml`,
      because ADR 0022 leaves it no directory of its own. That is the one place
      the "nothing of the operator's is written" rule bends, so it is done as
      one block, with a backup beside the file, and removed on the way out.

    No lock. Two lanes here write two different files, and the operator's file
    is guarded against a second *run* by §5's session lock — a thread lock
    serialises nothing across processes (#182).
    """

    def __init__(
        self,
        workspace: Path,
        *,
        agent: str,
        environment: Mapping[str, str],
        journal: Journal,
        run_id: str,
        home: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._agent = agent
        self._environment = dict(environment)
        self._journal = journal
        self._run_id = run_id
        self._home = home
        #: Both spellings, and only where they differ. Measured 2026-08-26:
        #: `claude agents --json` reports a Session's `cwd` resolved and
        #: `codex`'s `session_meta.cwd` the same, so today the two coincide —
        #: but a gate that only holds while that stays true is a gate that fails
        #: once, confusingly.
        self._spellings = sorted({str(workspace), os.path.realpath(workspace)})
        self._codex_block: str | None = None

    def __enter__(self) -> TrustGate:
        if self._agent == CLAUDE_LANE:
            self._grant_claude()
        else:
            self._write_codex_row()
        return self

    def __exit__(self, *_: object) -> None:
        if self._agent != CLAUDE_LANE:
            self._remove_codex_row()

    # -- the Claude lane's own directory -------------------------------------

    def _grant_claude(self) -> None:
        state = claude_state_path(self._environment)
        if not state.parent.is_dir():
            # §5 refuses a run whose lane directory is missing, so this is only
            # reachable when something removed it after preflight passed. Said
            # rather than created: a fresh directory is logged out, and a lane
            # that authenticates nobody is not a lane a grant can rescue.
            self._journal("trust.absent", agent=self._agent, state=str(state))
            return
        document = json.loads(state.read_text()) if state.exists() else {}
        projects = document.setdefault("projects", {})
        #: **Untrusted is not absent.** An entry that says `false` — or one
        #: carrying the operator's `allowedTools` and no verdict at all —
        #: answers "not trusted" while being present, and asking `path not in
        #: projects` reported `trust.already` and granted nothing (#217, by a
        #: second route, on any machine that has opened this directory once).
        granted = [
            path for path in self._spellings if not (projects.get(path) or {}).get(CLAUDE_TRUST_KEY)
        ]
        if not granted:
            self._journal(
                "trust.already", agent=self._agent, workspaces=self._spellings, state=str(state)
            )
            return
        for path in granted:
            projects[path] = {**(projects.get(path) or {}), CLAUDE_TRUST_KEY: True}
        state.write_text(json.dumps(document, indent=2))
        self._journal("trust.granted", agent=self._agent, workspaces=granted, state=str(state))

    # -- the Codex lane's one row in the operator's file ---------------------

    def _codex_config(self) -> Path:
        return codex_config_path(self._environment, self._home)

    def _write_codex_row(self) -> None:
        path = self._codex_config()
        if not path.exists():
            # The operator's file, and an absent one is theirs too: `codex`
            # writes it on first run and this harness will not author one on
            # their behalf. The lane then meets the trust dialog and `roster`
            # says so, which is a better failure than a file nobody asked for.
            self._journal("trust.absent", agent=self._agent, path=str(path))
            return
        existing = path.read_text()
        #: The **realpath**, and one row: `codex`'s own `session_meta.cwd` is
        #: resolved, so that is the spelling its config has to carry.
        wanted = os.path.realpath(self._workspace)
        if f'[projects."{wanted}"]' in existing:
            self._journal("trust.already", agent=self._agent, workspaces=[wanted], path=str(path))
            return
        backup = path.with_name(f"{path.name}.{BACKUP_SUFFIX}-{self._run_id}")
        shutil.copy2(path, backup)
        self._codex_block = codex_trust_block(wanted)
        path.write_text(existing + self._codex_block)
        self._journal(
            "trust.granted",
            agent=self._agent,
            workspaces=[wanted],
            path=str(path),
            backup=str(backup),
        )

    def _remove_codex_row(self) -> None:
        if self._codex_block is None:
            return
        path = self._codex_config()
        if not path.exists():
            self._journal("trust.unremoved", agent=self._agent, path=str(path), why="not there")
            return
        existing = path.read_text()
        wanted = os.path.realpath(self._workspace)
        if self._codex_block in existing:
            # The same block, removed once: the operator's file goes back to the
            # bytes it had, comments and key order included.
            path.write_text(existing.replace(self._codex_block, "", 1))
        else:
            # Their file changed under the run. The row is still this harness's
            # to remove, so it is removed structurally rather than left behind.
            path.write_text(_without_project_rows(existing, [wanted]))
        backup = path.with_name(f"{path.name}.{BACKUP_SUFFIX}-{self._run_id}")
        if wanted in _trusted_projects(path.read_text()):
            # Verified rather than assumed, for `reconcile_codex_trust`'s reason:
            # a row still there is a workspace left trusted in the operator's own
            # file. The backup **stays**, because it is now the only copy of
            # their file from before this run touched it.
            self._journal(
                "trust.unremoved",
                agent=self._agent,
                workspaces=[wanted],
                path=str(path),
                backup=str(backup),
                why="the row is still in the file after the removal; it is left for preflight",
            )
            return
        backup.unlink(missing_ok=True)
        self._journal(
            "trust.revoked",
            agent=self._agent,
            workspaces=[wanted],
            path=str(path),
        )
