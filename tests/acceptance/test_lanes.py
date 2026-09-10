"""Both lanes, against real agents this harness started by hand (#350).

## Two layers, and there is no third

* **Per ticket, one item.** A build ticket names an item, and that is what a
  developer runs while building it:

      .venv/bin/python -m pytest -m acceptance tests/acceptance \
          --lane codex --step approval

  The item is graded. Its prerequisites (`items.PREREQUISITES`) run first as
  **ungraded setup**, on a fresh engine and a hand-started Session of their own,
  because an item read on ground the run did not arrange is an item read on
  nothing. `verdict.json` names both kinds, so a green item is never mistaken
  for a green lane (§6).

* **Before merging, the whole thing.** No options: both run-level checks, then
  five items on both lanes in parallel. A human triggers it; it never runs in CI.

## One test, parametrised, rather than one module per lane

The lane is a value (`journey.Lane`), so the difference between the lanes lives
entirely in that value, and pytest's own parametrisation names the lane in the
test id. The walking happens off this test and the test joins it: both lanes are
started together, one thread each, so the run costs one lane's wall clock rather
than the sum of two, and the verdict is still one file with a block per lane.

The join is #352's and the walk is `journey`'s. What is settled here is the
shape: one parametrised test, and a lane graded on the rows it wrote.
"""

from __future__ import annotations

import items
import pytest
import support

pytestmark = [pytest.mark.acceptance, pytest.mark.covers(*items.LANE_ITEMS)]


def test_the_lane(lane, lane_runs, verdict: support.Verdict) -> None:  # noqa: ANN001
    recorded = verdict.document()["lanes"].get(lane.name, [])
    assert recorded, f"the {lane.name} lane recorded nothing at all"
    failed = [row for row in recorded if row["verdict"] != str(support.PASS)]
    assert not failed, "\n".join(
        f"{row['item']}{'' if row['graded'] else ' (setup)'}: {row['verdict']} — {row['evidence']}"
        for row in failed
    )
