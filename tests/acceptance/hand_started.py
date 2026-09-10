"""The pty: start a Session by hand, type into it, stop it (#350).

**Responsibilities held here** (§9's `hand_started.py`): starting the ordinary
`claude` / `codex` binary on a **controlling terminal**, typing a turn into it,
stopping it, and the Codex boot turn of §3.

All of it is **#352's**. The launch rules it must satisfy are §4.4's, and they
are launch rules rather than any step's (#73): the binary resolved on the login
shell's PATH and never a shell function; `login_tty` in a separately exec'd shim
and never `preexec_fn`, because the harness is threaded; the environment
scrubbed of `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID` and `CLAUDE_EFFORT`;
`HOME` and `PATH` extended and never replaced; `start_new_session=True` even
though the shim calls `setsid()`; never waiting on a Codex rollout file before
typing; and a settle between the text and the submit
(`deadlines.SUBMIT_SETTLE_SECONDS`).

**The screen is never parsed** to judge anything. Both TUIs redraw with cursor
addressing; the pty log is evidence for a human inside a failure message.
"""

from __future__ import annotations

from typing import Any


class Session:
    """A Session the product will list, started by hand in a pty (§4.4)."""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the hand-started Session is #352's")
