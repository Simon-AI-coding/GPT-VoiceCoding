"""The Telegram **user**-account client (#350).

**Responsibilities held here** (§9's `telegram_person.py`): one Telethon client
for both lanes, reading a message **by the id the product issued**, replying to
an id, the cross-process session lock of §5, and the one-time `login` a person
runs.

All of it is **#351's** (the client, the lock, the login) and **#353's** (the
reply that drives `companion inbound`).

Two rules this module exists to hold:

* **`telethon` is imported here and nowhere else.** It is the `acceptance` extra,
  it is a forbidden import for Bridge Core and the seams
  (`tests/test_architecture.py`), and it must not reach the bundle. Imported
  *inside* the functions that need it, so this module — and the suite that
  collects it — loads on a machine that never installed the extra.
* **Every chat read is by product-issued message id** (§9). The harness never
  searches the chat and never matches a message by the Session's name: one
  engine bridges every Session on the machine, so a search would grade a
  stranger. Reading by id removes the search rather than filtering it.
"""

from __future__ import annotations

from typing import Any


class PersonConnection:
    """One SQLite session, one client, two peers (§8)."""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the user-account client is #351's")


class PersonSessionLock:
    """The cross-process lock that refuses a second run (§5)."""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the session lock is #351's")


def login(*_: Any, **__: Any) -> Any:
    """The one-time login a person runs before any of this works (§8)."""
    raise NotImplementedError("the one-time login is #351's")
