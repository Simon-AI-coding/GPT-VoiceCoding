"""The five lane items, written once and walked by both lanes (#350).

**Responsibilities held here** (§9's `journey.py`): the lane as a value, the
engine-log lines the items read, and the walk of §2's five per-lane items. The
walk itself is **#353's** — this ticket lays the lane value and the contract the
walk is written against.

The lane is a value rather than a module per lane. The two used to be separate
files whose bodies were the same forty lines with one name changed, and the same
forty lines is where a fix applied to one lane and not the other comes from. So
the difference between the lanes lives entirely in `Lane`, and pytest's own
parametrisation names the lane in the test id (`test_lanes.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

import items

#: The engine's own line for a Stop it announced (`core/bridge.py`), and the
#: only engine-log line this harness matches at all (§2 item 2 reads the send
#: record beside it; §2 item 5 grades no log line whatsoever).
#:
#: **The record is several lines** (#189): a Stop Notice is a Session Brief and
#: the log carries `Briefing.text` whole, so `Session stopped:` opens the first
#: line and the brief's labelled lines follow. Matched line by line, so this
#: matches once per Stop — the header.
#:
#: `tests/test_bridge.py` asserts the product still writes a first line this
#: matches. That check is the reason this constant is public, and it is why it
#: stayed in this module through #350's rewrite.
ENGINE_STOP_LINE = r"(?i)Session stopped:"


@dataclass(frozen=True)
class Lane:
    """One lane: one engine, one hand-started Session, one bot, one chat (§2).

    The model pins are the lane's, not a step's (§3). The Codex lane carries
    **no** other launch argument: any `-c` override makes codex-tui run its own
    core instead of joining the shared daemon, and the lane's Session vanishes
    from `roster` (#232).
    """

    name: str
    #: The binary a Session is started from, resolved on the login shell's PATH
    #: and never through a shell function (§4.4).
    binary: str
    #: What the lane's turns are billed to (§3).
    model: str
    #: Claude's alone; Codex takes no second pin, for the reason above.
    effort: str | None = None


LANES: tuple[Lane, ...] = (
    Lane(name=items.LANES[0], binary="claude", model="sonnet", effort="medium"),
    Lane(name=items.LANES[1], binary="codex", model="gpt-5.6-luna"),
)


def lane(name: str) -> Lane:
    """The lane a `--lane` name stands for."""
    return next(one for one in LANES if one.name == name)


def walk(*_: object, **__: object) -> None:
    """Walk this lane's selected items, recording a row for each (§2, §6).

    **#353's**, with the three turns of §3 and the four items that read them;
    #352 arranges the engine, the Session and the workspace it walks on.
    """
    raise NotImplementedError("the lane walk is #353's")
