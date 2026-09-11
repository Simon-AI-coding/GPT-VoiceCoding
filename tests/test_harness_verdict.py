"""The run directory, the journal and `verdict.json`, at CI speed (#350).

`verdict.json` is the one artifact a reader needs, and every rule it obeys
(`docs/acceptance-design.md` §7) is ordinary code: the closed result set, the
graded/setup split, `missing`, the ranking, and the rule that makes the whole
file checkable — **a row's evidence is the journal line it rests on**, never a
sentence someone wrote about it. A verdict whose evidence cannot be resolved is
a verdict nobody can audit, and nobody re-reads a green one to find out.

The journal is the other half of that: one JSON line per harness event, written
once, with a reference the verdict can point at. #351–#353 write through this
API and read nothing else.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import items
import pytest
import support


@pytest.fixture
def run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A run directory of the real shape, somewhere disposable."""
    monkeypatch.setenv(support.ACCEPTANCE_ROOT_VARIABLE, str(tmp_path))
    return support.new_run_directory()


@pytest.fixture
def journal(run_directory: Path) -> support.Journal:
    return support.Journal(run_directory / support.JOURNAL_NAME)


def _verdict(
    journal: support.Journal, names: list[str] | None = None, **facts: object
) -> support.Verdict:
    return support.Verdict(
        run_id="20260910T000000Z",
        selection=items.select(names),
        lanes=items.LANES,
        journal=journal,
        **facts,  # type: ignore[arg-type]
    )


#: What the two agents printed about themselves on the run this was written
#: against, spelled as they spell it — `claude` gives the bare version and its
#: product name, `codex` gives the package name first.
VERSIONS = {"claude": "2.1.268 (Claude Code)", "codex": "codex-cli 0.154.0"}
BUNDLE = "/Applications/GPT-VoiceCoding.app"

#: The conftest, read rather than imported. A conftest is loaded by pytest **by
#: path** and is not an importable name (`tests/test_layout.py`), so the one
#: place the run builds its verdict cannot be called from here — but it can be
#: read, which is what `tests/test_harness_contract.py` already does to the rest
#: of the harness.
CONFTEST = Path(__file__).resolve().parent / "acceptance" / "conftest.py"


class TestTheRunDirectory:
    """§7's layout, under `~/Library/Application Support/…/acceptance/<run-id>/`."""

    def test_the_run_id_is_a_utc_timestamp(self) -> None:
        stamped = support.run_id(datetime(2026, 9, 10, 4, 5, 6, tzinfo=UTC))
        assert stamped == "20260910T040506Z"
        assert re.fullmatch(r"\d{8}T\d{6}Z", support.run_id())

    def test_the_default_root_is_the_products_application_support(self) -> None:
        """Named rather than derived at each call site, so one run has one home."""
        assert support.DEFAULT_ACCEPTANCE_ROOT == (
            Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance"
        )

    def test_a_run_directory_carries_the_layout_the_lanes_write_into(
        self, run_directory: Path
    ) -> None:
        assert run_directory.is_dir()
        for lane in items.LANES:
            assert (run_directory / f"engine-{lane}").is_dir()
            assert (run_directory / f"workspace-{lane}").is_dir()

    def test_the_run_directory_is_named_by_the_run_id(self, run_directory: Path) -> None:
        assert re.fullmatch(r"\d{8}T\d{6}Z", run_directory.name)


class TestTheJournal:
    """One small API, and the reference every verdict row rests on."""

    def test_one_json_line_per_event_in_the_order_they_happened(
        self, journal: support.Journal
    ) -> None:
        journal("engine.started", lane="claude")
        journal("bridgectl", action="status")
        assert [line["event"] for line in journal.read()] == ["engine.started", "bridgectl"]
        assert journal.read()[0]["lane"] == "claude"

    def test_every_line_is_stamped(self, journal: support.Journal) -> None:
        journal("engine.started")
        assert datetime.fromisoformat(journal.read()[0]["at"]).tzinfo is not None

    def test_writing_answers_the_reference_a_verdict_row_points_at(
        self, journal: support.Journal
    ) -> None:
        first = journal("one")
        second = journal("two")
        assert first == "journal.jsonl:1"
        assert second == "journal.jsonl:2"

    def test_a_reference_resolves_to_the_line_it_names(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        journal("first")
        reference = journal("relay.receipt", grade="delivered")
        resolved = support.resolve(run_directory, reference)
        assert resolved["event"] == "relay.receipt"
        assert resolved["grade"] == "delivered"

    def test_a_reference_to_a_line_that_is_not_there_is_an_error(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        journal("first")
        with pytest.raises(support.NoSuchJournalLine):
            support.resolve(run_directory, "journal.jsonl:9")

    def test_a_turn_records_its_seconds(self, journal: support.Journal) -> None:
        """§7: the five-minute figure is re-measured by every run, never asserted."""
        clock = _Clock(step=4.0)
        with journal.turn("acknowledge", lane="claude", now=clock) as turn:
            assert turn.reference is None
        (line,) = [one for one in journal.read() if one["event"] == "turn"]
        assert line["turn"] == "acknowledge"
        assert line["lane"] == "claude"
        assert line["seconds"] == pytest.approx(4.0)

    def test_a_finished_turn_hands_back_the_reference_of_its_own_line(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        with journal.turn("relay", now=_Clock(step=1.0)) as turn:
            pass
        assert support.resolve(run_directory, turn.reference)["turn"] == "relay"

    def test_a_turn_that_raised_still_recorded_its_seconds(self, journal: support.Journal) -> None:
        with pytest.raises(RuntimeError):  # noqa: PT012 - the point is the exit path
            with journal.turn("relay", now=_Clock(step=2.0)):
                raise RuntimeError("the far side never answered")
        (line,) = [one for one in journal.read() if one["event"] == "turn"]
        assert line["seconds"] == pytest.approx(2.0)
        assert line["ended"] == "raised"

    def test_two_threads_write_whole_lines(self, journal: support.Journal) -> None:
        """Two lanes write at once (§4); a half-written line is worse than none."""

        def tick(lane: str) -> None:
            for _ in range(50):
                journal("tick", lane=lane)

        threads = [threading.Thread(target=tick, args=(lane,)) for lane in items.LANES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(journal.read()) == 100


class TestTheResults:
    def test_the_closed_set_is_the_four_the_spec_names(self) -> None:
        assert {str(one) for one in support.Result} == {"PASS", "FAIL", "REFUSED", "SKIPPED"}

    def test_refused_outranks_fail_outranks_pass(self) -> None:
        assert support.worst([support.PASS, support.FAIL]) is support.FAIL
        assert support.worst([support.FAIL, support.REFUSED]) is support.REFUSED
        assert support.worst([support.PASS, support.PASS]) is support.PASS

    def test_a_skipped_row_is_not_a_pass(self) -> None:
        assert support.worst([support.PASS, support.SKIPPED]) is support.FAIL


class TestTheVerdict:
    """§7 — the rules a reader trusts the file for."""

    def test_a_run_level_check_writes_a_row_in_the_run_block(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal)
        verdict.record(items.Item.PREFLIGHT, support.PASS, journal("preflight.done"))
        document = verdict.document()
        assert [row["item"] for row in document["run"]] == ["preflight"]
        assert document["run"][0]["verdict"] == "PASS"

    def test_a_lane_item_writes_a_row_under_its_lane(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        document = verdict.document()
        assert list(document["lanes"]) == ["claude", "codex"]
        assert [row["item"] for row in document["lanes"]["claude"]] == ["roster"]
        assert document["lanes"]["codex"] == []

    def test_a_row_is_keyed_by_the_item_name_the_spec_spells(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal)
        verdict.record(
            items.Item.COMPANION_INBOUND, support.PASS, journal("inbound.file"), lane="codex"
        )
        assert verdict.document()["lanes"]["codex"][0]["item"] == "companion inbound"

    def test_every_written_rows_evidence_resolves_to_a_journal_line(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        """The rule that makes the file auditable: evidence is a reference, not prose."""
        verdict = _verdict(journal)
        verdict.record(items.Item.PROBE, support.PASS, journal("probe.done", frames=41))
        verdict.record(items.Item.RELAY, support.FAIL, journal("relay.receipt"), lane="claude")
        verdict.skip(items.Item.APPROVAL, "blocked by claude/relay", lane="claude")
        written = json.loads(verdict.write(run_directory / support.VERDICT_NAME).read_text())
        rows = written["run"] + [row for lane in written["lanes"].values() for row in lane]
        assert rows
        for row in rows:
            if row["evidence"] is None:
                continue
            assert support.resolve(run_directory, row["evidence"])

    def test_evidence_is_never_free_text(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        with pytest.raises(support.NotAJournalReference):
            verdict.record(items.Item.PREFLIGHT, support.PASS, "the machine looked fine")

    def test_a_skipped_row_says_why_in_the_line_it_rests_on(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        """§7: SKIPPED is not a judgement — it means the row never ran, and why."""
        verdict = _verdict(journal, ["approval"])
        verdict.skip(items.Item.APPROVAL, "blocked by claude/roster", lane="claude")
        row = verdict.document()["lanes"]["claude"][-1]
        assert row["verdict"] == "SKIPPED"
        assert support.resolve(run_directory, row["evidence"])["why"] == "blocked by claude/roster"

    def test_an_ungraded_setup_row_carries_no_evidence(self, journal: support.Journal) -> None:
        """A setup row is how the run reached the item, not a claim about the product."""
        verdict = _verdict(journal, ["approval"])
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        row = verdict.document()["lanes"]["claude"][0]
        assert row["graded"] is False
        assert row["evidence"] is None

    def test_a_graded_row_says_so(self, journal: support.Journal) -> None:
        verdict = _verdict(journal, ["approval"])
        verdict.record(items.Item.APPROVAL, support.PASS, journal("approval.file"), lane="claude")
        assert verdict.document()["lanes"]["claude"][0]["graded"] is True

    def test_a_row_carries_its_own_seconds(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        verdict.record(
            items.Item.RELAY, support.PASS, journal("relay.receipt"), lane="claude", seconds=7.5
        )
        assert verdict.document()["lanes"]["claude"][0]["seconds"] == pytest.approx(7.5)

    def test_the_run_is_pass_only_when_every_graded_row_is_pass(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal, ["roster"])
        for lane in items.LANES:
            verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane=lane)
        verdict.record(items.Item.PREFLIGHT, support.PASS, journal("preflight.done"))
        verdict.record(items.Item.PROBE, support.PASS, journal("probe.done"))
        assert verdict.result is support.PASS

    def test_one_failed_graded_row_fails_the_run(self, journal: support.Journal) -> None:
        verdict = _verdict(journal, ["roster"])
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        verdict.record(items.Item.ROSTER, support.FAIL, journal("roster.read"), lane="codex")
        verdict.record(items.Item.PREFLIGHT, support.PASS, journal("preflight.done"))
        verdict.record(items.Item.PROBE, support.PASS, journal("probe.done"))
        assert verdict.result is support.FAIL

    def test_a_failed_setup_row_does_not_decide_the_run_by_itself(
        self, journal: support.Journal
    ) -> None:
        """It cannot hide a red either — the item it was arranging is SKIPPED."""
        verdict = _verdict(journal, ["approval"])
        verdict.record(items.Item.ROSTER, support.FAIL, journal("roster.read"), lane="claude")
        verdict.skip(items.Item.RELAY, "blocked by claude/roster", lane="claude")
        verdict.skip(items.Item.APPROVAL, "blocked by claude/roster", lane="claude")
        rows = verdict.document()["lanes"]["claude"]
        assert [row["graded"] for row in rows] == [False, False, True]
        assert verdict.result is support.FAIL

    def test_a_refusal_outranks_a_failure(self, journal: support.Journal) -> None:
        verdict = _verdict(journal, ["roster"])
        verdict.record(items.Item.ROSTER, support.FAIL, journal("roster.read"), lane="claude")
        verdict.record(items.Item.PREFLIGHT, support.REFUSED, journal("preflight.refused"))
        assert verdict.result is support.REFUSED

    def test_a_promised_row_the_run_never_wrote_is_missing_and_not_a_pass(
        self, journal: support.Journal
    ) -> None:
        """The one failure mode a verdict must not have (#73)."""
        verdict = _verdict(journal, ["roster"])
        verdict.record(items.Item.PREFLIGHT, support.PASS, journal("preflight.done"))
        verdict.record(items.Item.PROBE, support.PASS, journal("probe.done"))
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        assert verdict.missing == ("codex/roster",)
        assert verdict.result is support.FAIL

    def test_missing_names_run_level_rows_too(self, journal: support.Journal) -> None:
        verdict = _verdict(journal, ["probe"])
        assert verdict.missing == ("run/probe",)

    def test_missing_never_names_a_row_no_option_asked_for(self, journal: support.Journal) -> None:
        verdict = _verdict(journal, ["roster"])
        verdict.record(items.Item.PREFLIGHT, support.PASS, journal("preflight.done"))
        verdict.record(items.Item.PROBE, support.PASS, journal("probe.done"))
        for lane in items.LANES:
            verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane=lane)
        assert verdict.missing == ()

    def test_a_lane_refused_at_preflight_has_five_skipped_rows_not_five_missing_ones(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal)
        verdict.refuse("no bot token for the codex lane")
        document = verdict.document()
        assert [(row["item"], row["verdict"]) for row in document["run"]] == [
            ("preflight", "REFUSED")
        ]
        for lane in items.LANES:
            rows = document["lanes"][lane]
            assert [row["verdict"] for row in rows] == ["SKIPPED"] * 5
        assert verdict.missing == ("run/probe",)
        assert verdict.result is support.REFUSED

    def test_a_refusal_still_writes_a_valid_verdict(self, run_directory: Path) -> None:
        written = json.loads(
            support.write_refusal(run_directory, "the bundle is missing").read_text()
        )
        assert written["result"] == "REFUSED"
        assert written["run"][0]["item"] == "preflight"
        assert support.resolve(run_directory, written["run"][0]["evidence"])["why"] == (
            "the bundle is missing"
        )


class TestTheVerdictDocument:
    def test_it_carries_what_section_seven_names(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        verdict.bundle = "/Applications/GPT-VoiceCoding.app"
        verdict.commit = "95ea513"
        verdict.versions = {"claude": "1.2.3", "codex": "4.5.6"}
        document = verdict.document()
        assert document["run_id"] == "20260910T000000Z"
        assert document["bundle"] == "/Applications/GPT-VoiceCoding.app"
        assert document["commit"] == "95ea513"
        assert document["versions"] == {"claude": "1.2.3", "codex": "4.5.6"}
        assert set(document) >= {
            "run_id",
            "result",
            "missing",
            "selection",
            "bundle",
            "commit",
            "versions",
            "seconds",
            "run",
            "lanes",
        }

    def test_the_run_passes_every_fact_section_seven_names(self) -> None:
        """#357: the constructor accepted `bundle` and `versions`; nobody passed them.

        Every test above builds a verdict of its own, so all of them were green
        while each real run wrote `"bundle": ""` and `"versions": {}` — the
        defect was never in the shape of the file but in the one call that fills
        it. Read here rather than asserted on a run, because the run costs five
        minutes and this costs a parse.

        **Exact on purpose**, which makes it the one test here a rename breaks:
        the set is §7's list of what the file carries, so a keyword dropped is
        the fact leaving the verdict, and a keyword added is §7 growing one and
        this list owing an entry. Both are edits worth making by hand.
        """
        built = [
            node
            for node in ast.walk(ast.parse(CONFTEST.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Verdict"
        ]
        assert len(built) == 1, "the run builds exactly one verdict"
        assert {keyword.arg for keyword in built[0].keywords} == {
            "run_id",
            "selection",
            "lanes",
            "journal",
            "bundle",
            "commit",
            "versions",
        }

    def test_the_build_and_the_agents_are_given_at_construction(
        self, journal: support.Journal
    ) -> None:
        """The gap #357 closed, pinned at the seam a run really builds one through.

        `bundle` and `versions` were accepted by the constructor and passed by
        nobody, so every real run wrote `"bundle": ""` and `"versions": {}` while
        the test above — which sets them on the object afterwards — stayed green.
        A fact a reader needs has to arrive the way the run supplies it.
        """
        document = _verdict(journal, bundle=BUNDLE, commit="95ea513", versions=VERSIONS).document()
        assert document["bundle"] == BUNDLE
        assert document["commit"] == "95ea513"
        assert document["versions"] == VERSIONS

    def test_a_refused_run_still_names_the_build_and_the_agents(
        self, journal: support.Journal
    ) -> None:
        """§7: a refusal writes a valid verdict, and these two are still in it.

        They are facts about the machine rather than claims about anything the
        run observed, so the run that observed nothing is exactly the one whose
        verdict has nothing else to be attributed by.
        """
        verdict = _verdict(journal, bundle=BUNDLE, versions=VERSIONS)
        verdict.refuse("the bundle is missing")
        document = verdict.document()
        assert document["result"] == "REFUSED"
        assert document["bundle"] == BUNDLE
        assert document["versions"] == VERSIONS

    def test_the_versions_are_one_entry_per_lane(self, journal: support.Journal) -> None:
        """One name per lane, so a reader attributes a red to the agent that ran it."""
        document = _verdict(journal, versions=VERSIONS).document()
        assert set(document["versions"]) == set(items.LANES)

    def test_the_selection_says_what_was_graded_and_what_was_arranged(
        self, journal: support.Journal
    ) -> None:
        """So a green item is never read as a green lane (§6)."""
        verdict = _verdict(journal, ["approval"])
        assert verdict.document()["selection"] == {
            "selected": ["approval"],
            "setup": ["roster", "relay"],
            "lanes": ["claude", "codex"],
        }

    def test_the_seconds_are_the_runs_wall_clock_and_each_lanes(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal)
        verdict.seconds["run"] = 214.0
        verdict.seconds["claude"] = 101.0
        assert verdict.document()["seconds"] == {"run": 214.0, "claude": 101.0}

    def test_it_is_written_as_json_with_a_trailing_newline(
        self, run_directory: Path, journal: support.Journal
    ) -> None:
        path = _verdict(journal).write(run_directory / support.VERDICT_NAME)
        assert path.name == "verdict.json"
        assert path.read_text().endswith("\n")


class _Clock:
    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        answer = self.now
        self.now += self.step
        return answer


class TestARowIsOneRowRestingOnARealLine:
    """The two ways a verdict could carry a row nobody can check."""

    def test_evidence_that_names_a_line_the_journal_does_not_have_is_refused(
        self, journal: support.Journal
    ) -> None:
        """Format is not existence: `journal.jsonl:999` looks like a reference."""
        verdict = _verdict(journal)
        journal("preflight.done")
        with pytest.raises(support.NoSuchJournalLine):
            verdict.record(items.Item.PREFLIGHT, support.PASS, "journal.jsonl:999")

    def test_evidence_that_names_another_file_is_refused(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        with pytest.raises(support.NotAJournalReference):
            verdict.record(items.Item.PREFLIGHT, support.PASS, "elsewhere.jsonl:1")

    def test_an_item_gets_one_row_on_one_axis(self, journal: support.Journal) -> None:
        """§7: one row per run-level check, one row per lane per item."""
        verdict = _verdict(journal)
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        with pytest.raises(support.RowAlreadyWritten):
            verdict.record(items.Item.ROSTER, support.FAIL, journal("roster.read"), lane="claude")

    def test_the_same_item_on_the_other_lane_is_a_different_row(
        self, journal: support.Journal
    ) -> None:
        verdict = _verdict(journal)
        for lane in items.LANES:
            verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane=lane)
        assert verdict.missing == () or "roster" not in " ".join(verdict.missing)

    def test_a_skip_cannot_overwrite_a_row_either(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="claude")
        with pytest.raises(support.RowAlreadyWritten):
            verdict.skip(items.Item.ROSTER, "blocked by claude/roster", lane="claude")

    def test_a_lane_this_run_never_selected_is_refused(self, journal: support.Journal) -> None:
        """`_rows` used to accept any string, so a typo grew a lane nobody ran."""
        verdict = _verdict(journal)
        with pytest.raises(support.NoSuchLane):
            verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"), lane="gemini")

    def test_a_lane_item_without_a_lane_is_refused(self, journal: support.Journal) -> None:
        """The axis is not optional: `roster` is a fact about one lane."""
        verdict = _verdict(journal)
        with pytest.raises(support.WrongAxis):
            verdict.record(items.Item.ROSTER, support.PASS, journal("roster.read"))

    def test_a_run_level_check_with_a_lane_is_refused(self, journal: support.Journal) -> None:
        verdict = _verdict(journal)
        with pytest.raises(support.WrongAxis):
            verdict.record(items.Item.PROBE, support.PASS, journal("probe.done"), lane="claude")
