"""Item 0 — the machine is arranged, or the run refuses (#350).

`preflight` verifies the machine, not the product (`docs/acceptance-design.md`
§5). Every check is a read, never a turn; all but one are free or a single call;
and each caught a real wasted run once. **Any failure is REFUSED**: the run exits
non-zero, writes a valid `verdict.json` naming the refusal, and never starts an
engine. Refusing is the one place "no over-defence" does not apply — a refusal
costs seconds and prevents a five-minute red whose cause was the environment.

It is a **run-level** check (§2): it runs once, before any lane starts, and it
carries its own row in the verdict's `run` block. Like `probe`, it is selectable
like any item — `--step preflight` is "tell me whether this machine is arranged",
and it spends nothing.

This test is that row's reading. It costs nothing to collect by default: the
preflight fixture has already run by the time any test does — every other test
depends on it — so this reads a row that is already there and states its result
where a reader looks for results, beside the lanes and the probe. A run-level
check that no test reads is a check whose red has to be inferred from some other
test's error.

The checks themselves are **#351's**. What this module settles is that preflight
is a selectable item with a row of its own, graded from the verdict the way every
other item is.
"""

from __future__ import annotations

import pytest
from items import Item

pytestmark = [pytest.mark.acceptance, pytest.mark.covers(Item.PREFLIGHT)]


def test_the_machine_is_arranged(preflight, verdict) -> None:  # noqa: ANN001
    (row,) = [one for one in verdict.document()["run"] if one["item"] == str(Item.PREFLIGHT)]
    assert row["verdict"] == "PASS", row["evidence"]
