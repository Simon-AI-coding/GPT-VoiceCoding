"""The item names, the lanes, and how a run selects among them (#350).

**Responsibilities held here:** the closed enumeration of §2's items, the two
lane names, the prerequisite table of §6, and `select` / `select_lanes` — the
resolution of `--step` and `--lane` into what a run walks and what it grades.

The names are a contract (`docs/acceptance-design.md` §2): build and bug tickets
cite them verbatim, and renaming one moves a ticket's exit criterion. So they are
spelled in exactly one place — here — and every other module imports them.
`tests/test_harness_contract.py` fails if a second module grows its own list.

This module is a **leaf**: it imports nothing of the harness, so `support`,
`journey` and `conftest` can all reach it without a cycle. It is also the module
the *fast* suite reads, which is why the selection logic lives here rather than
in `conftest.py` — a conftest is loaded by path and is not an importable name
(`tests/test_layout.py`). That is a deliberate departure from the spec's
suggested layout at §9, which parks the options in the conftest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Item(StrEnum):
    """Everything this harness grades, spelled exactly as §2's table spells it.

    A `StrEnum` because the name is the value in three places at once: the
    `--step` a person types, the row key in `verdict.json`, and the constant a
    build ticket imports. One spelling, three uses, no translation table.
    """

    PREFLIGHT = "preflight"
    PROBE = "probe"
    ROSTER = "roster"
    STOP_NOTICE = "stop notice"
    RELAY = "relay"
    APPROVAL = "approval"
    COMPANION_INBOUND = "companion inbound"


#: The two run-level checks. Neither needs an engine or a Session, both run once
#: per run before any lane starts, and both carry their own row in the verdict's
#: `run` block (§2, §7).
RUN_ITEMS: tuple[Item, ...] = (Item.PREFLIGHT, Item.PROBE)

#: The five per-lane items, in the order a lane walks them (§2's table).
LANE_ITEMS: tuple[Item, ...] = (
    Item.ROSTER,
    Item.STOP_NOTICE,
    Item.RELAY,
    Item.APPROVAL,
    Item.COMPANION_INBOUND,
)

#: Every item, run-level first. The order here is the order a run walks.
ITEMS: tuple[Item, ...] = RUN_ITEMS + LANE_ITEMS

#: The two lanes (§4). A lane is a name in the verdict and a value in `journey`.
LANES: tuple[str, ...] = ("claude", "codex")


#: What each item needs arranged beneath it before it can be read (§6).
#:
#: `roster` grounds everything on a lane: an item read on a Session the product
#: never listed is an item read on nothing. `approval` grades the permission the
#: *relay* turn raised, so it stands on `relay`. `companion inbound` replies to
#: the Stop Notice, so `stop notice` is its reply anchor.
#:
#: The acknowledge turn is **not** a row here. §3 gives each item its own turn
#: and `stop notice` drives that one itself — a turn shared between items makes
#: one item's failure look like another's, and a turn is not something a verdict
#: has a result for.
PREREQUISITES: dict[Item, tuple[Item, ...]] = {
    Item.PREFLIGHT: (),
    Item.PROBE: (),
    Item.ROSTER: (),
    Item.STOP_NOTICE: (Item.ROSTER,),
    Item.RELAY: (Item.ROSTER,),
    Item.APPROVAL: (Item.RELAY,),
    Item.COMPANION_INBOUND: (Item.STOP_NOTICE,),
}


class UnknownName(Exception):
    """A selector named something outside its closed set, and carries the set.

    One base so the pytest layer converts both refusals with one `except`, and
    so a third selector — if one is ever needed — is refused in the same words.
    """


class UnknownItem(UnknownName):
    """`--step` named something that is not an item. It carries the ones there are."""


class UnknownLane(UnknownName):
    """`--lane` named something that is not a lane. It carries the ones there are."""


def _chosen(
    asked: Sequence[str] | None,
    known: tuple[str, ...] | tuple[Item, ...],
    *,
    refusal: type[UnknownName],
    kind: str,
) -> tuple[Any, ...]:
    """Resolve names against a closed set: dedupe, refuse a stranger, keep order.

    Both selectors are the same four steps — nothing asked means everything, a
    name is matched **exactly**, an unknown one is refused carrying the whole
    set, and the answer comes back in the set's declared order rather than the
    order someone typed. Written once so a typo in one selector cannot be
    refused in different words from a typo in the other (§6).
    """
    wanted = tuple(dict.fromkeys(asked or ()))
    if not wanted:
        return tuple(known)
    spelled = {str(one): one for one in known}
    unknown = [name for name in wanted if name not in spelled]
    if unknown:
        raise refusal(
            f"no such acceptance {kind}: {', '.join(repr(name) for name in unknown)}. "
            f"The {kind}s are: {', '.join(spelled)}."
        )
    return tuple(one for one in known if str(one) in set(wanted))


@dataclass(frozen=True)
class Selection:
    """What a run grades, and what it merely arranges to reach it.

    The two tuples are kept apart all the way into `verdict.json` for one reason
    (§6): a reader who sees `approval` green must also see that `roster` and
    `relay` ran ungraded beneath it, or a green item reads as a green lane.
    """

    #: Graded. What the run promised to observe, in `ITEMS` order.
    selected: tuple[Item, ...]
    #: Ungraded. The prerequisite closure of `selected`, minus anything selected.
    setup: tuple[Item, ...]

    @property
    def items(self) -> tuple[Item, ...]:
        """Everything to walk, in `ITEMS` order — the table's order is the walk's."""
        chosen = set(self.selected) | set(self.setup)
        return tuple(item for item in ITEMS if item in chosen)

    @property
    def whole_run(self) -> bool:
        """Every item graded: the pre-merge run rather than one ticket's item."""
        return self.selected == ITEMS

    def graded(self, item: Item) -> bool:
        return item in self.selected


def select(names: Sequence[str] | None = None) -> Selection:
    """Resolve `--step` into what to walk and what to grade.

    No names is the full run: every item graded, nothing as setup. Any name is
    matched against `Item` **exactly** — these spellings are what every build
    ticket cites, so a near miss is a refusal carrying the whole list rather than
    a run that quietly walks one fewer (§6).
    """
    selected = _chosen(names, ITEMS, refusal=UnknownItem, kind="item")
    if selected == ITEMS:
        return Selection(selected=ITEMS, setup=())
    chosen = set(selected)
    needed: set[Item] = set()
    pending = list(selected)
    while pending:
        for one in PREREQUISITES[pending.pop()]:
            if one not in needed:
                needed.add(one)
                pending.append(one)
    setup = tuple(item for item in ITEMS if item in needed - chosen)
    return Selection(selected=selected, setup=setup)


def select_lanes(names: Sequence[str] | None = None) -> tuple[str, ...]:
    """Resolve `--lane` into the lanes this run walks, in `LANES` order.

    A lane nobody selected must not be *started*, so this is answered before
    collection rather than filtered after it (`conftest.pytest_generate_tests`).
    """
    return _chosen(names, LANES, refusal=UnknownLane, kind="lane")
