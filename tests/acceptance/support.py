"""The run directory, the journal and the verdict (#350).

**Responsibilities held here** (§9's `support.py`): the run directory of §7, the
places a run reads from — the bundle, its interpreter, the operator's real
config — the provenance of §5, the credential scan of §8, the journal every row rests on,
the `bridgectl` runner, the derived config, the trust grant, and `verdict.json`.
The locations, the provenance, the journal, the run directory and the verdict are
built here; the `bridgectl` runner, `derive_config` and `TrustGate` are the seams
#352 and #353 fill, and are named below so those tickets have somewhere to land
rather than a shape to invent.

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

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import deadlines
import items
from items import Item

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


# --- the seams #351 and #352 fill -------------------------------------------


class Bridgectl:
    """Every product action the run takes, through the bundle's own `bridgectl`.

    §2: "Every product action goes through `bridgectl` against that engine." The
    runner — the call, its reply parsed as fields, and the journal line each one
    writes — is **#352's**. #350 placed it on #351 as "the ticket that first
    needs one"; #351 turned out not to need one at all, because every check it
    holds is a read of the machine and the probe never starts an engine. The
    first item that calls it is `roster` (`bridgectl status`), and that is #352's.
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the bridgectl runner is #352's")


def derive_config(*_: Any, **__: Any) -> Any:
    """The operator's real `config.toml` with §4.2's eight values replaced.

    **#352's**, together with `config-dropped.json` and the launcher tables it
    drops.
    """
    raise NotImplementedError("the derived config is #352's")


def reconcile_codex_trust(acceptance_root: Path) -> tuple[str, ...]:
    """Remove any Codex trust row left behind for an acceptance workspace (§4.1).

    §5's one row that is **not** a refusal: a killed run can leave a
    `[projects."<workspace>"]` row in the operator's own `config.toml` for a
    workspace under the acceptance root, and a leftover is arranged away before
    the lane starts rather than graded. Answers the workspaces it removed, so
    preflight journals `trust.reconciled` with them.

    **The read-modify-write of the real file is #352's**, together with the
    backup beside it and the block shape `TrustGate` writes and removes. What
    lands here is the seam preflight calls and journals. Until #352 fills it this
    answers *nothing removed*, which is an **assumption** and not a reading — it
    is right about every machine no killed run left a row on, and #352 is what
    turns it into an answer.
    """
    return ()


class TrustGate:
    """The Claude lane's trust grant and the Codex lane's trust row (§4.1).

    **#352's**, together with the reconciliation preflight does for a row a
    killed run left behind.
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the trust gate is #352's")
