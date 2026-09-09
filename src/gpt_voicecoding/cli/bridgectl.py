"""``bridgectl`` — a control-plane surface, and nothing more.

It dials the engine's socket, sends one request, prints what came back, and
exits. It holds **no policy and no state**: every question is answered by the
hub, every refusal is the hub's own words, and nothing is cached between runs —
a second copy of the truth is how the reference implementation's status line
came to report a configuration file instead of an engine.

There is no per-command argument parser here either. The command line is parsed
by `control_plane.commands`, the same parser the Companion Channel's `/`
grammar uses, so the two surfaces cannot drift into two command sets.

Three exits, and they mean different things: the engine answered (0), the engine
refused (1), or there was no engine to ask (2). A surface that collapsed the
last two would tell a user their switch does not exist when in fact nothing is
running.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.config import ConfigError, default_config_path, load
from gpt_voicecoding.control_plane.client import (
    DEFAULT_TIMEOUT_SECONDS,
    EngineSilent,
    EngineUnreachable,
    ask,
)
from gpt_voicecoding.control_plane.commands import CommandError, build_request, render
from gpt_voicecoding.seams.control_plane import READER_FLAG, USAGE, Action

#: The global flags that take a value beside them, as opposed to standing
#: alone. Written down because anything reading a `bridgectl` command line has
#: to skip the *value* as well as the flag to find the action: the probe
#: stand-in in `scripts/realtime_instruction_tracer.py` decides from that action
#: whether a command may reach somebody's real Session, and a reader that took
#: `voice` for a verb would forward a `relay` it meant to stop.
VALUE_FLAGS: tuple[str, ...] = ("--config", "--socket", "--timeout", READER_FLAG)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNREACHABLE = 2


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bridgectl",
        description="Talk to a running GPT-VoiceCoding engine.",
        epilog="commands:\n  " + "\n  ".join(USAGE[action] for action in Action),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None, help="the engine's configuration")
    parser.add_argument(
        "--socket", type=Path, default=None, help="the engine's socket, instead of reading config"
    )
    # Beside `--socket` rather than among a command's own arguments: it says who
    # the answer is *for*, which is a fact about this run and not about what is
    # being asked. The engine fits a `history` page or a Session Brief marked
    # this way to the Live Call's return leg (#302); every other action carries
    # it and ignores it. A human may pass it to see what the Voice would get.
    parser.add_argument(
        READER_FLAG,
        default=None,
        help="who the answer is for, when that changes what can be carried",
    )
    # No default here: which deadline applies depends on the action, which is not
    # known until the command line has been parsed. `None` means "not asked for",
    # so an operator's own number still outranks whatever the action would pick.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait, instead of the deadline this action carries",
    )
    parser.add_argument("command", help="one of the commands listed below")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="whatever it takes")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``bridgectl`` console script."""
    arguments = parse(argv)

    try:
        request = build_request(arguments.command, arguments.arguments, reader=arguments.reader)
    except CommandError as unreadable:
        print(str(unreadable), file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        socket_path = _socket_path(arguments)
    except ConfigError as refusal:
        print(f"{refusal}. Point at a running engine with --socket.", file=sys.stderr)
        return EXIT_UNREACHABLE

    timeout = arguments.timeout if arguments.timeout is not None else DEFAULT_TIMEOUT_SECONDS
    try:
        reply = asyncio.run(ask(request, path=socket_path, timeout=timeout))
    except EngineSilent as unanswered:
        print(str(unanswered), file=sys.stderr)
        return EXIT_UNREACHABLE
    except EngineUnreachable as down:
        print(str(down), file=sys.stderr)
        return EXIT_UNREACHABLE

    rendered = render(reply)
    if reply.ok:
        print(rendered)
        return EXIT_OK
    print(rendered, file=sys.stderr)
    return EXIT_REFUSED


def _socket_path(arguments: argparse.Namespace) -> Path:
    """Where the engine is. Told directly, or read from the same file it read."""
    if arguments.socket is not None:
        return Path(arguments.socket)
    return load(arguments.config or default_config_path()).socket_path
