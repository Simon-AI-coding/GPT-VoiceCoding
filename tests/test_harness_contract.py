"""The acceptance harness's naming contract, checked at CI speed (#350).

The real-environment run needs this machine, these credentials and that bot, so
none of it runs in CI. But the *names* it grades by are a contract three build
tickets cite — #351, #352 and #353 all import them — and a contract is ordinary
code. #109 is what a rule with no test at CI speed costs.

Two things are pinned here:

* **the item names** (`docs/acceptance-design.md` §2), spelled verbatim, closed,
  and enumerated in exactly one module; and
* **the selection** (§6) — the prerequisite closure, the refusal a misspelling
  earns, and the graded/setup split that keeps a green item from reading as a
  green lane.

Plus the one rule that keeps every wait in one place (§7 Time): the harness's
numbers for waiting live in `deadlines`, and nowhere else.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import deadlines
import items
import pytest

ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"

#: §2's table, in its order, spelled as the spec spells it. Written out rather
#: than derived from `items`, because a test that asks the module what it thinks
#: the names are cannot catch the module changing its mind.
SPELLED = (
    "preflight",
    "probe",
    "roster",
    "stop notice",
    "relay",
    "approval",
    "companion inbound",
)


class TestTheItemNames:
    """One closed enumeration, spelled once."""

    def test_the_closed_set_is_the_spec_table(self) -> None:
        assert tuple(str(one) for one in items.ITEMS) == SPELLED

    def test_the_two_run_level_checks_and_the_five_lane_items_are_that_set_split(self) -> None:
        """§2: `preflight` and `probe` run once per run; the other five per lane."""
        assert tuple(str(one) for one in items.RUN_ITEMS) == ("preflight", "probe")
        assert len(items.LANE_ITEMS) == 5
        assert items.RUN_ITEMS + items.LANE_ITEMS == items.ITEMS

    def test_an_item_is_its_own_name(self) -> None:
        """`StrEnum`, so a row key, a `--step` value and the enum are one thing."""
        assert items.Item.STOP_NOTICE == "stop notice"
        assert f"{items.Item.COMPANION_INBOUND}" == "companion inbound"
        assert items.Item("relay") is items.Item.RELAY

    def test_the_set_is_closed(self) -> None:
        with pytest.raises(ValueError):
            items.Item("stable name")

    def test_every_item_has_a_prerequisite_row(self) -> None:
        assert set(items.PREREQUISITES) == set(items.ITEMS)

    def test_the_two_lanes_are_named_once(self) -> None:
        assert items.LANES == ("claude", "codex")

    def test_no_other_harness_module_keeps_its_own_list_of_item_names(self) -> None:
        """The enumeration is exported once and imported everywhere else.

        A second list is how two modules come to disagree about what `stop
        notice` is called, and the disagreement surfaces as a verdict row nobody
        can find. Only a *list* is an offence — a single word may legitimately be
        the product's own (`bridgectl relay` is an action name, not an item).
        """
        spelled = set(SPELLED)
        offences: list[str] = []
        for source in sorted(ACCEPTANCE.glob("*.py")):
            if source.name == "items.py":
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.List | ast.Tuple | ast.Set):
                    continue
                values = [
                    one.value
                    for one in node.elts
                    if isinstance(one, ast.Constant) and isinstance(one.value, str)
                ]
                named = spelled.intersection(values)
                if len(named) >= 2:
                    offences.append(f"{source.name}:{node.lineno} — {sorted(named)}")
        assert not offences, (
            "these spell the item enumeration a second time; import it from `items` instead: "
            + "; ".join(offences)
        )


class TestTheSelection:
    """§6 — one item, one lane, or everything."""

    def test_no_option_grades_everything_with_nothing_as_setup(self) -> None:
        chosen = items.select()
        assert chosen.selected == items.ITEMS
        assert chosen.setup == ()
        assert chosen.whole_run

    def test_a_selected_item_walks_its_prerequisite_closure_as_setup(self) -> None:
        """`approval` needs `relay`, and `relay` needs `roster` — transitively."""
        chosen = items.select(["approval"])
        assert chosen.selected == (items.Item.APPROVAL,)
        assert chosen.setup == (items.Item.ROSTER, items.Item.RELAY)
        assert not chosen.whole_run

    def test_companion_inbound_is_reached_through_its_reply_anchor(self) -> None:
        """§2 item 5 replies to the Stop Notice, so `stop notice` is its ground."""
        chosen = items.select(["companion inbound"])
        assert chosen.setup == (items.Item.ROSTER, items.Item.STOP_NOTICE)

    def test_everything_needs_roster(self) -> None:
        for item in items.LANE_ITEMS:
            if item is items.Item.ROSTER:
                continue
            assert items.Item.ROSTER in items.select([item]).setup

    def test_a_run_level_check_stands_on_nothing(self) -> None:
        """Neither needs an engine or a Session (§2), so neither has ground to arrange."""
        assert items.select(["probe"]).setup == ()
        assert items.select(["preflight"]).setup == ()

    def test_the_walk_is_in_the_tables_order(self) -> None:
        chosen = items.select(["approval", "roster"])
        assert chosen.items == (items.Item.ROSTER, items.Item.RELAY, items.Item.APPROVAL)

    def test_an_item_named_twice_is_named_once(self) -> None:
        assert items.select(["relay", "relay"]).selected == (items.Item.RELAY,)

    def test_a_prerequisite_that_was_also_asked_for_is_graded(self) -> None:
        """Setup is the closure *minus* what was selected — never both."""
        chosen = items.select(["relay", "roster"])
        assert chosen.selected == (items.Item.ROSTER, items.Item.RELAY)
        assert chosen.setup == ()
        assert chosen.graded(items.Item.ROSTER)

    def test_setup_is_walked_and_not_graded(self) -> None:
        chosen = items.select(["approval"])
        assert chosen.graded(items.Item.APPROVAL)
        assert not chosen.graded(items.Item.RELAY)
        assert items.Item.RELAY in chosen.items

    def test_a_misspelling_is_refused_with_the_list(self) -> None:
        """A typo must never silently drop an item (§6)."""
        with pytest.raises(items.UnknownItem) as refused:
            items.select(["stop-notice"])
        assert "stop-notice" in str(refused.value)
        for name in SPELLED:
            assert name in str(refused.value)

    def test_a_retired_name_is_refused_like_any_other(self) -> None:
        with pytest.raises(items.UnknownItem):
            items.select(["live call"])


class TestTheLaneSelection:
    def test_no_option_walks_both_lanes(self) -> None:
        assert items.select_lanes() == items.LANES

    def test_one_lane_walks_alone(self) -> None:
        assert items.select_lanes(["codex"]) == ("codex",)

    def test_the_lanes_stay_in_their_declared_order(self) -> None:
        assert items.select_lanes(["codex", "claude"]) == items.LANES

    def test_an_unknown_lane_is_refused_with_the_list(self) -> None:
        with pytest.raises(items.UnknownLane) as refused:
            items.select_lanes(["gemini"])
        assert "gemini" in str(refused.value)
        assert "claude" in str(refused.value) and "codex" in str(refused.value)


class TestTheDeadlines:
    """§7 Time — every wait is a deadline on something ending, named in one place."""

    def test_the_deadlines_the_ticket_names_are_all_here(self) -> None:
        for named in ("PROBE_SECONDS", "TURN_SECONDS", "REPLY_SECONDS", "ENGINE_STOP_SECONDS"):
            assert named in deadlines.DEADLINES

    def test_the_registry_is_exactly_the_modules_named_seconds(self) -> None:
        """`DEADLINES` is how a wait is looked up, so a constant outside it is unreachable."""
        spelled = {name for name in vars(deadlines) if name.endswith("_SECONDS")}
        assert spelled == set(deadlines.DEADLINES)

    def test_every_deadline_is_a_positive_number_of_seconds(self) -> None:
        assert all(value > 0 for value in deadlines.DEADLINES.values())

    def test_nothing_else_in_the_harness_carries_a_number_for_a_wait(self) -> None:
        """The one rule that keeps per-item tuning from growing back (§7).

        Checked by reading the modules rather than by trusting them: a literal
        named `…_SECONDS`, a literal passed as a timeout, or a literal slept on
        is a deadline that escaped the one place they are configured.
        """
        waiting = {"timeout", "timeout_seconds", "deadline_seconds", "seconds", "poll_seconds"}
        offences: list[str] = []
        for source in sorted(ACCEPTANCE.glob("*.py")):
            if source.name == "deadlines.py":
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    named = [
                        one.id
                        for one in node.targets
                        if isinstance(one, ast.Name) and one.id.endswith(("_SECONDS", "_TIMEOUT"))
                    ]
                    if named and isinstance(node.value, ast.Constant):
                        offences.append(f"{source.name}:{node.lineno} — {named[0]}")
                if isinstance(node, ast.Call):
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sleep"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                    ):
                        offences.append(f"{source.name}:{node.lineno} — sleep()")
                    for keyword in node.keywords:
                        if keyword.arg in waiting and isinstance(keyword.value, ast.Constant):
                            if isinstance(keyword.value.value, int | float):
                                offences.append(f"{source.name}:{node.lineno} — {keyword.arg}=")
        assert not offences, "these carry a number for a wait outside `deadlines`: " + "; ".join(
            offences
        )

    def test_the_login_shell_budget_lives_here_now(self) -> None:
        """Moved from the old `support` (#350's ruling): it is a number for a wait.

        `tests/test_app_bundle.py` reads it against the Swift the shell uses, so
        the two cannot drift — that check is the reason this constant is public.
        """
        assert deadlines.PATH_TIMEOUT_SECONDS in deadlines.DEADLINES.values()


class TestADeadlineHit:
    """A deadline hit is a FAIL that names what never ended (§7)."""

    def test_waiting_returns_the_answer_as_soon_as_it_arrives(self) -> None:
        answers = iter([None, None, "the roster row"])
        assert (
            deadlines.wait(
                "TURN_SECONDS",
                lambda: next(answers),
                what="the acknowledge turn",
                now=_Clock(),
                sleep=lambda _: None,
            )
            == "the roster row"
        )

    def test_an_expiry_names_what_never_ended_and_the_deadline_it_ran_out_of(self) -> None:
        clock = _Clock(step=deadlines.DEADLINES["TURN_SECONDS"])
        with pytest.raises(deadlines.DeadlineExpired) as expired:
            deadlines.wait(
                "TURN_SECONDS",
                lambda: None,
                what="the acknowledge turn",
                now=clock,
                sleep=lambda _: None,
            )
        assert expired.value.what == "the acknowledge turn"
        assert expired.value.deadline == "TURN_SECONDS"
        assert "the acknowledge turn" in str(expired.value)
        assert "never ended" in str(expired.value)

    def test_an_unknown_deadline_is_a_mistake_at_the_call_site(self) -> None:
        with pytest.raises(KeyError):
            deadlines.wait("SOME_SECONDS", lambda: True, what="anything")


class _Clock:
    """A monotonic clock that advances by a fixed step on every reading."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        answer = self.now
        self.now += self.step
        return answer


class TestWhatTheSuiteCollects:
    """The selection as a person meets it: `--step`, `--lane`, and a typo.

    Run out of process because it is the *pytest* layer being checked — the
    options, the parametrisation and the deselection — and this suite is already
    inside one pytest. Collection only: nothing here starts an engine, opens a
    chat or spends a turn.
    """

    def _collect(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "acceptance",
                "--collect-only",
                str(ACCEPTANCE),
                *options,
            ],
            cwd=ACCEPTANCE.parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_no_option_collects_four_the_two_lanes_and_both_run_level_checks(self) -> None:
        """The pre-merge run (#350 criterion 7, as amended): four tests.

        Each run-level check is selectable like any item (§2) and so has a test
        of its own, collected by default beside the parametrised lane test.
        """
        collected = self._collect()
        assert collected.returncode == 0, collected.stdout + collected.stderr
        assert "test_the_machine_is_arranged" in collected.stdout
        assert "test_the_lane[claude]" in collected.stdout
        assert "test_the_lane[codex]" in collected.stdout
        assert "test_the_realtime_contract" in collected.stdout

    def test_one_lane_collects_one(self) -> None:
        collected = self._collect("--lane", "codex")
        assert "test_the_lane[codex]" in collected.stdout
        assert "test_the_lane[claude]" not in collected.stdout

    def test_a_lane_item_does_not_drag_the_run_level_probe_along(self) -> None:
        collected = self._collect("--step", "approval")
        assert "test_the_lane[claude]" in collected.stdout
        assert "test_the_realtime_contract" not in collected.stdout

    def test_the_run_level_probe_walks_no_lane(self) -> None:
        collected = self._collect("--step", "probe")
        assert "test_the_realtime_contract" in collected.stdout
        assert "test_the_lane" not in collected.stdout

    def test_preflight_is_selectable_like_any_other_item(self) -> None:
        """§2: both run-level checks are selectable, and `--step preflight` spends nothing."""
        collected = self._collect("--step", "preflight")
        assert collected.returncode == 0, collected.stdout + collected.stderr
        assert "test_the_machine_is_arranged" in collected.stdout
        assert "test_the_lane" not in collected.stdout
        assert "test_the_realtime_contract" not in collected.stdout

    def test_every_item_is_covered_by_some_test(self) -> None:
        """No item may be selectable and unreachable — `--step <it>` collected nothing.

        That is what `--step preflight` did before this test existed: pytest
        exited 5 with no tests, which reads as "the machine is fine".
        """
        for item in items.ITEMS:
            collected = self._collect("--step", str(item))
            assert collected.returncode == 0, f"--step {item!s}: {collected.stdout}"
            assert "tests/acceptance/test_" in collected.stdout, (
                f"--step {item!s} collected nothing, which pytest reports as exit code 5 — "
                f"an item nobody can run reads as an item that passed"
            )

    def test_a_misspelled_item_refuses_the_run_before_it_collects(self) -> None:
        collected = self._collect("--step", "stop-notice")
        assert collected.returncode != 0
        assert "no such acceptance item" in collected.stdout + collected.stderr
        assert "stop notice" in collected.stdout + collected.stderr

    def test_a_misspelled_lane_refuses_the_run_before_it_collects(self) -> None:
        collected = self._collect("--lane", "gemini")
        assert collected.returncode != 0
        assert "no such acceptance lane" in collected.stdout + collected.stderr


def test_telethon_is_imported_in_the_user_account_client_and_nowhere_else() -> None:
    """The `acceptance` extra stays behind one module (§8).

    `telethon` is a forbidden import for Bridge Core and the seams
    (`tests/test_architecture.py`) and it must not reach the bundle, so the one
    module that speaks to a Telegram *user* account is the one place it may be
    named. Checked here as well, because the architecture test reads `src/` and
    this rule is about `tests/acceptance/`.
    """
    named = [
        f"{source.name}:{number}"
        for source in sorted(ACCEPTANCE.glob("*.py"))
        if source.name != "telegram_person.py"
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith(("import telethon", "from telethon"))
    ]
    assert not named, "telethon reaches beyond the user-account client: " + "; ".join(named)
