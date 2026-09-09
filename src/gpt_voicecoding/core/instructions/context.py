"""What generation is told, and the things it refuses to invent.

The CLI location and the engine it reaches are knowable outside instruction
generation. The composition root therefore hands them in rather than letting
prose remember or rediscover them. Bridge Core reads no file and probes no
filesystem.

The refusal matters more than the plumbing. A generated instruction that names
a CLI which is not there is an invented detail, and inventing detail is the
first thing the catalogue's own rules forbid. So there is no fallback string:
without a real invocation there are no delegated instructions.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from gpt_voicecoding.core.instructions.blocks import InstructionError
from gpt_voicecoding.seams.control_plane import READER_FLAG, Reader


@dataclass(frozen=True, slots=True)
class ControlPlaneCli:
    """The control-plane surface a generated thread is told to act through."""

    #: Where the executable really is on this machine.
    command: Path
    #: The engine's own version, so a thread and its engine can be told apart.
    version: str
    #: Which engine to talk to. Passed explicitly, because a thread that read
    #: the configuration file itself could reach a different engine than the one
    #: that generated its instructions.
    socket_path: Path

    def __post_init__(self) -> None:
        if not self.command.is_absolute():
            raise InstructionError(
                f"the control-plane CLI must be named by where it really is; {str(self.command)!r} "
                "is somewhere only whoever ran it could resolve"
            )
        if not self.version.strip():
            raise InstructionError("the control-plane CLI must carry the engine's version")
        if not self.socket_path.is_absolute():
            raise InstructionError(
                f"the engine's socket must be an absolute path; {str(self.socket_path)!r} is not"
            )

    @property
    def invocation(self) -> str:
        """The command line, quoted so a path with spaces survives a shell."""
        return f"{shlex.quote(str(self.command))} --socket {shlex.quote(str(self.socket_path))}"

    @property
    def invocation_for_the_voice(self) -> str:
        """The same command line, marked as read on the Voice's behalf (#302).

        **The engine sets the mark, not the reader of these instructions.** The
        Call Agent's answer crosses the Live Call's return leg, where codex cuts
        anything over 4,000 bytes and says how much went but never which part;
        an engine told who is reading can fit the answer to that line instead.
        So the flag is generated into the invocation the Call Agent is handed,
        beside the `--socket` it already carries, and the Call Agent never
        learns that the mark exists — there is nothing for it to decide.

        A second property rather than a parameter on the one above, so that the
        set which does *not* cross that leg — a Delegated Turn's — keeps the
        unmarked line by construction rather than by remembering to ask for it.

        Both halves come from the seam's vocabulary — the flag word and the
        reader — because Bridge Core may not reach into the control plane's
        parser for either (ADR 0001) and a word spelled out in two places is a
        word free to drift.
        """
        return f"{self.invocation} {READER_FLAG} {Reader.VOICE}"


@dataclass(frozen=True, slots=True)
class InstructionContext:
    """Everything generation is parameterised by. Handed in, never discovered."""

    cli: ControlPlaneCli
