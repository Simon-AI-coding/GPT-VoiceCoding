"""What this product itself spent of the user's Codex allowance, one line per fact (#377).

**A durable history, and a different store from `state.json`.** The persistence
module keeps switch state and nothing else, and says a history would be a
different decision; this is that decision (ADR 0032). Every usage fact the
engine is told about Codex work *it* started — a Call Agent or Delegated Turn
token reading, the Voice's own realtime usage, the account's rate-limit reading
— is appended here the moment it arrives, so a call that ends badly loses
nothing already heard.

**One JSON object per line, one file per calendar month, never pruned.** The
reader is a person or an agent with `jq`, so a line states what it is (`kind`)
and when (`at`, ISO 8601 with its offset, so it lines up with `engine.log` and
`~/.codex/sessions`). The month is the local one, read from the same clock that
stamps the line, so a line and the file it lands in never disagree.

**Bookkeeping never costs the thing it is keeping books on.** `record` does not
raise. A line that cannot be written is one warning in the log, and further
failures stay quiet until a line is written again — a full disk would otherwise
put a warning in the log for every heartbeat of every call.

It carries ids and numbers only. What the Voice or the user said never reaches
this file; the callers hand it usage payloads and nothing else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: `usage-2026-09.jsonl`: the prefix, the month in `MONTH_FORMAT`, the suffix.
FILE_PREFIX = "usage-"
FILE_SUFFIX = ".jsonl"
MONTH_FORMAT = "%Y-%m"

#: Reads the current local time, with its offset. Injected so a test can cross a month.
WallTime = Callable[[], datetime]


def local_now() -> datetime:
    """Now, in this machine's own timezone, offset attached."""
    return datetime.now().astimezone()


class UsageKind(StrEnum):
    """What a line records."""

    #: A `thread/tokenUsage/updated` reading for a thread this product runs.
    CODEX_THREAD = "codex_thread"
    #: A `session.usage.updated` event off a Live Call's realtime channel.
    REALTIME = "realtime"
    #: An `account/rateLimits/updated` reading.
    RATE_LIMITS = "rate_limits"


class UsageRole(StrEnum):
    """Which of this product's threads a `CODEX_THREAD` line is about."""

    CALL_AGENT = "call_agent"
    DELEGATED_TURN = "delegated_turn"


class UsageLedger:
    """Append-only, monthly, and silent about its own failures beyond one warning."""

    def __init__(self, directory: Path, *, now: WallTime = local_now) -> None:
        self._directory = directory
        self._now = now
        self._failing = False

    def path_for(self, moment: datetime) -> Path:
        return self._directory / f"{FILE_PREFIX}{moment.strftime(MONTH_FORMAT)}{FILE_SUFFIX}"

    def record(self, kind: UsageKind, **fields: Any) -> None:
        """Append one line. Fields that are None are left out. Never raises."""
        moment = self._now()
        line = {"at": moment.isoformat(), "kind": kind.value}
        line.update({name: value for name, value in fields.items() if value is not None})
        try:
            text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
            path = self.path_for(moment)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as ledger:
                ledger.write(text + "\n")
        except (OSError, TypeError, ValueError) as failed:
            if not self._failing:
                self._failing = True
                _log.warning("the usage ledger could not be written: %s", failed)
            return
        self._failing = False
