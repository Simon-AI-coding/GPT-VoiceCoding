"""Item 0b — the realtime contract, probed with the engine out of the way (#350).

`docs/app-bundle.md` is explicit that no voice failure may be attributed to this
engine before the contract is re-verified outside it: the realtime methods are an
alpha backend surface, absent from the official app-server docs and gated
server-side, so the contract can move without anything in this repository
changing. A probe that fails identically outside the engine has told you, in
seconds, that the engine is not the subject.

The probe is **invoked, not rewritten**: `scripts/rt_prototype.py --silent` in
the maintainer's own checkout is the script `docs/acceptance-design.md` §2 names.
Copying it here would fork a script whose whole value is that it is the one that
was actually run against the backend. It runs on the **bundle's own interpreter**,
which is where `aiortc` and `av` are: the thing being accepted is the `.app`, and
a probe run by some other Python would prove the contract for some other copy of
the stack.

It is a **run-level** check (§2): it needs no engine and no Session, it runs once
before any lane starts, and it carries its own row in the verdict's `run` block.
A red probe is FAIL for the whole run and the lanes still walk — they do not
depend on the realtime backend — so one run reports both facts.

Running the probe is **#351's**, with the credentials and the preflight that
decide whether it can run at all. What is settled here is that it is one
run-level test, selectable as `--step probe`.
"""

from __future__ import annotations

import pytest
from items import Item

pytestmark = [pytest.mark.acceptance, pytest.mark.covers(Item.PROBE)]


def test_the_realtime_contract(probe_run, verdict) -> None:  # noqa: ANN001
    """The engine-free realtime start returned audio frames (§2 item 0b)."""
    (row,) = [one for one in verdict.document()["run"] if one["item"] == str(Item.PROBE)]
    assert row["verdict"] == "PASS", (
        f"the engine-free realtime probe received no audio frames — the contract has moved, "
        f"and no voice failure this run sees may be attributed to the engine. "
        f"{row['evidence']}"
    )
