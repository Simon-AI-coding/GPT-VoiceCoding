"""The composition root: configuration in, one running engine out.

This is the only place in the system that knows how the parts fit together, and
the only place allowed to import an adapter — which it does by name, from
configuration, so a deployment's own wiring stays its own. Everything else is
handed what it needs: Bridge Core gets adapters as Protocols, adapters get the
event sink, the control plane gets the hub.

Assembled here and nowhere else:

- the adapters configuration named, constructed with the one sink they speak
  upward through;
- the one Bridge Core, over the one `BridgeState`, restored from the one state
  file before anything is served;
- the control plane, and the inbound-text grammar built from *its* command set,
  so `/status` in the Companion Channel and `bridgectl status` are one command;
- the Delegated Turn handler, carrying the model from configuration — the cost
  lever is a user-facing setting and nothing here may default it;
- the generation context for Bridge Core's two instruction sets: where the
  control-plane CLI really is on this machine, which engine it reaches, and its
  version. This root states those facts rather than letting generated prose
  guess them — configuration first, then the console script beside this
  interpreter, and a refusal when neither is really there;
- the two loops the engine needs to be alive: one that drains events into the
  hub's dispatch, and one that advances the hub's two ceilings on a timer.

**Headless is the real shape.** Nothing here knows about the menu-bar shell; the
shell is another control-plane surface plus process parenthood (ADR 0005). An
engine started from a terminal is the same engine.

The engine never daemonises: the shell spawns it as a direct child and expects
it to stay one.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.companion_channel.telegram import TelegramCompanionChannel
from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramBinding
from gpt_voicecoding.config import ConfigError, EngineConfig
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.control_plane.commands import CommandError, build_request, render
from gpt_voicecoding.control_plane.progress_publication import ProgressPublication
from gpt_voicecoding.control_plane.server import ControlPlaneServer
from gpt_voicecoding.core.bridge import BridgeCore, ControlAnswer
from gpt_voicecoding.core.briefing import TELEGRAM_BINDING_CONFIRMATION
from gpt_voicecoding.core.errors import BridgeCoreError
from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.core.instructions import ControlPlaneCli, InstructionContext
from gpt_voicecoding.core.persistence import StateStore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.router import Classification, TextGrammar
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.core.turns import DelegatedAnswer
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import AgentAdapter, ProgressCapture
from gpt_voicecoding.seams.call import CallAdapter, DelegatedTurnError, ThreadGoneError
from gpt_voicecoding.seams.companion_channel import CompanionChannel
from gpt_voicecoding.seams.connection import Connectable
from gpt_voicecoding.seams.control_plane import Action
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, new_request_id
from gpt_voicecoding.usage_ledger import UsageLedger

_log = logging.getLogger(__name__)

#: How often the hub's two ceilings are advanced. Mechanism, not policy: the
#: durations themselves are configuration, this is only how finely they are read.
DEFAULT_TICK_SECONDS = 1.0

#: How often the hub asks every lane what Sessions exist.
#:
#: Derived from what the answer costs and from what being late costs. One pass
#: is a `claude agents --json` (one process), one `ps`, and one `lsof` per Codex
#: TUI — cheap, but not free, and paid whether or not anything changed. Being
#: late costs the user a Session that is running and not yet listed, which is
#: the whole product being briefly wrong about the machine.
#:
#: Five seconds is a Session appearing within one breath of being started, at a
#: cost the machine does not notice. The real-environment acceptance gives an
#: empty roster 30 s before it takes that as the answer
#: (`tests/acceptance/journey.py`, `DISCOVERY_SECONDS`), which is six passes of
#: headroom — enough that a slow pass, or one that overlapped and was skipped,
#: cannot be read as a Session that is not there.
DEFAULT_DISCOVERY_SECONDS = 5.0

#: The console script `pyproject.toml` installs beside the interpreter.
CONTROL_PLANE_CLI_NAME = "bridgectl"

#: What a Delegated Turn is answered with when the hub generated no instructions
#: for its thread. Unreachable from this root, which refuses to assemble without
#: a control-plane CLI to name; stated rather than asserted so the failure is a
#: sentence the user hears instead of a traceback in the log.
NO_DELEGATED_INSTRUCTIONS = (
    "I can't take a delegated turn right now — this engine generated no instructions for one"
)

#: How an adapter factory is called. One argument, because the sink is the one
#: thing every adapter needs and the only thing this root can honestly supply.
Factory = Callable[..., Any]


class EngineAssemblyError(Exception):
    """Configuration named something this engine cannot load or construct."""


def import_factory(reference: str) -> Factory:
    """Resolve one `module:attribute` reference. The only import of an adapter."""
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise EngineAssemblyError(f"cannot import {reference}: {error}") from None
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise EngineAssemblyError(
            f"cannot load {reference}: {module_name} has no {attribute!r}"
        ) from None


@dataclass(frozen=True, slots=True)
class Adapters:
    """Everything behind a seam, as this engine actually constructed it."""

    call: CallAdapter
    channel: CompanionChannel
    agents: dict[AgentKind, AgentAdapter]

    def all(self) -> tuple[object, ...]:
        """Everything behind a seam, in the order it is opened."""
        return (self.call, self.channel, *self.agents.values())

    def connectable(self) -> tuple[Connectable, ...]:
        """The ones with something of their own to open and close."""
        return tuple(held for held in self.all() if isinstance(held, Connectable))


class Engine:
    """One assembled engine: a hub, its adapters, and the socket it answers on."""

    def __init__(
        self,
        *,
        config: EngineConfig,
        core: BridgeCore,
        adapters: Adapters,
        plane: ControlPlane,
        server: ControlPlaneServer,
    ) -> None:
        self._config = config
        self.core = core
        self.adapters = adapters
        self.plane = plane
        self._server = server
        self._loops: list[asyncio.Task[None]] = []

    @property
    def socket_path(self) -> Path:
        return self._server.path

    @classmethod
    def assemble(
        cls, config: EngineConfig, *, factory_of: Callable[[str], Factory] = import_factory
    ) -> Engine:
        """Build one engine from configuration. Nothing is constructed twice."""
        events = EventQueue()
        progress_publication = ProgressPublication()
        _refuse_a_page_the_wire_cannot_carry(config, progress_publication)
        progress_capture = progress_publication.capture
        adapters = _adapters(
            config,
            events,
            factory_of,
            progress_capture=progress_capture,
        )
        _share_the_app_server(adapters)

        state = BridgeState(
            switches=Switchboard(),
            sessions=SessionRegistry(policy=config.policy),
            relays=RelayQueue(),
            store=StateStore(config.state_path),
        )
        if not state.restore():
            for name in (SwitchName.DUTY, SwitchName.VOICE, SwitchName.MESSAGE):
                state.switches.flip(name, True)

        # The hub needs a control handler, and the control plane needs the hub.
        # The knot is tied with one late binding rather than by giving either of
        # them a way to be half-built.
        held: dict[str, ControlPlane] = {}
        # The Delegated Turn's thread instructions are Bridge Core's, generated
        # by the hub this closure is being built for, so the hub is read at call
        # time rather than captured before it exists.
        hub: dict[str, BridgeCore] = {}

        async def control(found: Classification) -> ControlAnswer:
            return await _answer_text(held["plane"], found)

        async def delegate(found: Classification) -> DelegatedAnswer:
            """One Delegated Turn, and its failure said in the failure's own words.

            **A failure is an answer here, not an exception** (ADR 0021 §7). The
            hub's job is to tell the user what happened, and the words a refused
            or lost turn carries are exactly that; letting the error out instead
            would reach the drain loop, be logged, and leave the user with
            silence where they asked a question. The one failure that is not
            words is a thread that cannot be resumed — Core answers that with
            its own fixed hint, so it travels as a fact and not as prose.
            """
            instructions = hub["core"].instructions
            if instructions is None:  # unreachable from here: this root always generates them
                return DelegatedAnswer(text=NO_DELEGATED_INSTRUCTIONS)
            try:
                reply = await adapters.call.delegate(
                    found.text,
                    model=config.delegated_turn_model,
                    instructions=instructions.delegated.text,
                    request_id=new_request_id(),
                    resume=found.thread_id,
                )
            except ThreadGoneError:
                return DelegatedAnswer(thread_gone=True)
            except DelegatedTurnError as refusal:
                return DelegatedAnswer(text=str(refusal))
            return DelegatedAnswer(text=reply.text)

        async def open_conversation() -> str:
            """Start one Assistant Conversation's thread, on the same seam and model."""
            instructions = hub["core"].instructions
            if instructions is None:  # unreachable from here, as above
                raise BridgeCoreError(NO_DELEGATED_INSTRUCTIONS)
            return await adapters.call.open_conversation(
                model=config.delegated_turn_model,
                instructions=instructions.delegated.text,
            )

        core = BridgeCore(
            state=state,
            call=adapters.call,
            channel=adapters.channel,
            agents=adapters.agents,
            events=events,
            policy=config.policy,
            # The grammar's command words are the action set itself, so the
            # channel cannot recognise a command the control plane lacks, nor
            # miss one it has.
            grammar=TextGrammar(control_commands=frozenset(str(name) for name in Action)),
            control=control,
            delegate=delegate,
            open_conversation=open_conversation,
            inventory=_inventory(config),
            instruction_context=_instruction_context(config),
        )
        hub["core"] = core
        telegram = (
            adapters.channel if isinstance(adapters.channel, TelegramCompanionChannel) else None
        )
        plane = ControlPlane(
            core,
            progress_publication=progress_publication,
            telegram_binding=TelegramBinding(
                confirmation=TELEGRAM_BINDING_CONFIRMATION,
                pause=telegram.pause_polling if telegram is not None else None,
                resume=telegram.connect if telegram is not None else None,
            ),
        )
        held["plane"] = plane

        return cls(
            config=config,
            core=core,
            adapters=adapters,
            plane=plane,
            server=ControlPlaneServer(
                plane=plane,
                path=config.socket_path,
                max_bytes=progress_publication.max_bytes,
                progress_publication=progress_publication,
            ),
        )

    async def start(
        self,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        discovery_seconds: float = DEFAULT_DISCOVERY_SECONDS,
    ) -> None:
        """Connect the adapters, serve the control plane, and start the two loops.

        Adapters open before the socket does, so a surface that reaches a
        serving engine reaches one whose seams are actually filled. An adapter
        that fails to open stops the start: an engine answering `status` over
        seams that never connected is the reference implementation's outage
        wearing a healthy face.

        **A start that fails closes whatever it already opened.** Otherwise the
        third adapter raising leaves the first two holding a socket and a reader
        task that nothing will ever close — the caller sees an exception and has
        no handle to clean up with, because the engine never finished being one.
        """
        opened: list[Connectable] = []
        try:
            for adapter in self.adapters.connectable():
                await adapter.connect()
                opened.append(adapter)
            await self.core.models()
            await self._server.start()
        except BaseException:
            await self._closing(opened)
            await self._server.aclose()  # a no-op unless this engine bound it
            raise

        self._loops = [
            asyncio.create_task(self._dispatching(), name="bridge-core-dispatch"),
            asyncio.create_task(self._ticking(tick_seconds), name="bridge-core-tick"),
            asyncio.create_task(self._discovering(discovery_seconds), name="bridge-core-discovery"),
        ]

    async def run(
        self,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        discovery_seconds: float = DEFAULT_DISCOVERY_SECONDS,
    ) -> None:
        """Serve until cancelled. What `python -m gpt_voicecoding.engine` runs."""
        await self.start(tick_seconds=tick_seconds, discovery_seconds=discovery_seconds)
        try:
            await asyncio.gather(*self._loops)
        except asyncio.CancelledError:
            raise
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop answering, stop looping, close the adapters, leave no socket behind.

        In the reverse order of opening, and every adapter is given its turn
        even if an earlier one objected: a shutdown that abandoned the rest on
        the first raise is how a reader task outlives the engine that owned it.

        **Every phase says it is starting, before it starts.** #96 was diagnosed
        from an engine log whose last line was an ordinary discovery tick: the
        shutdown wrote nothing at all, so "it never began" and "it began and
        hung" were the same file, and the session that read it went looking in
        the wrong half of the code. A line per phase makes the next hang name
        itself — the last line written is the phase that did not finish.
        """
        _log.info("stopping: cancelling the loops")
        for loop in self._loops:
            loop.cancel()
        for loop in self._loops:
            try:
                await loop
            except asyncio.CancelledError:
                pass
        self._loops = []
        # **Before the adapters, because cancelling is what reaches their
        # teardown** (#268). A Delegated Turn runs beside the dispatch loop now,
        # so a shutdown can find one in flight; cancelling it raises inside
        # `CallAdapter.delegate`, whose `finally` interrupts the turn and
        # unsubscribes from the thread. Close the Call adapter first and there
        # is no connection left to interrupt over, and a bridge-owned thread —
        # which runs approval-free in a full sandbox — would go on acting on the
        # user's machine. No answer is sent for a cancelled turn: the loop that
        # would dispatch one is already gone.
        _log.info("stopping: cancelling the Delegated Turns in flight")
        await self.core.turns.aclose()
        _log.info("stopping: closing the control plane")
        await self._server.aclose()
        _log.info("stopping: closing the adapters")
        await self._closing(self.adapters.connectable())
        _log.info("stopped: every loop cancelled, the socket gone and the adapters closed")

    async def _closing(self, opened: Sequence[Connectable]) -> None:
        """Close what is open, newest first, and give every one of them its turn.

        Newest first is also **most owned first**: the Agent adapters go before
        the Companion Channel and the Call, and the Codex one — the only adapter
        here that owns a *process* — goes first of all. That ordering is what
        makes a later hang survivable: whatever else stalls on the way out, the
        app-server this engine spawned has already been told to go (#96).
        """
        for adapter in reversed(list(opened)):
            _log.info("stopping: closing %s", type(adapter).__name__)
            try:
                await adapter.aclose()
            except Exception:
                _log.exception("closing %s raised", type(adapter).__name__)

    async def _dispatching(self) -> None:
        """One event at a time, in arrival order. The hub decides what each means."""
        while True:
            event = await self.core.events.next_event()
            try:
                await self.core.dispatch(event)
            except Exception:  # one bad event must not stop the engine
                _log.exception("dispatching %s raised", type(event).__name__)

    async def _ticking(self, seconds: float) -> None:
        """Advance Core's Relay, approval, and Live Call silence ceilings."""
        while True:
            await asyncio.sleep(seconds)
            try:
                await self.core.tick()
            except Exception:
                _log.exception("advancing the ceilings raised")

    async def _discovering(self, seconds: float) -> None:
        """The other: ask every lane what Sessions exist, one pass at a time.

        A loop of its own rather than a counter inside the tick, because the two
        cadences answer to different things — one to the ceilings the user
        configured, one to how fast a Session should appear — and a loop that
        did both would tie them together at whichever is finer.

        **Never two passes at once.** `await` here can outlast the interval: a
        `claude` that is slow to answer, or a machine with many Codex TUIs to
        `lsof`. Overlapping passes would have two readings of one machine racing
        to write the same rows, so a pass that is still running simply means the
        next one does not start — the sleep happens first, and this loop is the
        only caller.
        """
        while True:
            await asyncio.sleep(seconds)
            try:
                await self.core.discover()
            except Exception:
                _log.exception("a discovery pass raised")


async def _answer_text(plane: ControlPlane, found: Classification) -> ControlAnswer:
    """One inbound command, answered in words — the Companion Channel's surface.

    `ok` travels with the words because the words alone cannot be told apart:
    a refusal is prose like any other answer, and Bridge Core anchors on one
    and not the other (`ControlAnswer`). An unreadable command line never
    reached the plane, so it is a refusal too.
    """
    try:
        request = build_request(found.command, shlex.split(found.text))
    except (CommandError, ValueError) as unreadable:
        return ControlAnswer(str(unreadable), ok=False)
    reply = await plane.handle(request)
    return ControlAnswer(render(reply), ok=reply.ok)


def _refuse_a_page_the_wire_cannot_carry(
    config: EngineConfig, publication: ProgressPublication
) -> None:
    """The dial and the ceiling meet here, and nowhere else (#171).

    `[policy] history_page_entries` says how many entries one History page holds
    and `ProgressPublication` is the only thing that knows how many slots the
    Control Plane line carries. A page dialled past that could only ever be
    published with **every** entry marked `oversize` — which would say the
    messages were too large when what was too large is the page — so the engine
    refuses to start on that configuration rather than answering with it.

    Refused here rather than in `config.py` for ADR 0016's own reason: capacity
    decides what may be asked for, and configuration does not know the capacity.
    Refused at composition rather than at the first read because a configuration
    that can only ever answer dishonestly is not a request that failed.
    """
    largest = publication.largest_page
    if config.policy.history_page_entries > largest:
        raise ConfigError(
            f"[policy] history_page_entries is {config.policy.history_page_entries}, and a "
            f"{publication.max_bytes}-byte Control Plane line carries at most {largest} "
            f"entry slots: every page would be published with every entry marked oversize"
        )


def _adapters(
    config: EngineConfig,
    sink: EventSink,
    factory_of: Callable[[str], Factory],
    *,
    progress_capture: ProgressCapture,
) -> Adapters:
    def built(seam: str, reference: str) -> Any:
        """Construct one adapter with the sink, and its settings table if it has one.

        The table is forwarded **opaque**: this root never reads a key inside it,
        because only the adapter knows what its own keys mean and a root that
        parsed them would be the hub growing adapter-shaped knowledge (ADR 0001).
        A seam with no table is called exactly as it always was, so an adapter
        that takes only the sink needs no change to keep working.

        The Call seam is handed `[delegate] model` the same way, and beside the
        table rather than inside it (#270). It is the Delegated Turn's model and
        the Call Agent's both — one value, stated once, in this root's own
        configuration — so it travels like `progress_capture` does: known before
        construction, and therefore an argument to it. A second key under
        `[adapters.settings.call]` would be the same number written twice.

        The usage ledger (#377) is handed over the same way: it is written beside
        the state file, in the engine's own directory, and always on.
        """
        factory = factory_of(reference)
        settings = config.adapters.settings_for(seam)
        arguments: dict[str, Any] = {"sink": sink}
        if seam.startswith("agent."):
            arguments["progress_capture"] = progress_capture
        if seam == "call":
            arguments["delegated_turn_model"] = config.delegated_turn_model
            arguments["delegated_turn_effort"] = config.delegated_turn_effort
            arguments["usage_ledger"] = UsageLedger(config.state_path.parent)
        if settings is not None:
            arguments["settings"] = settings
        try:
            return factory(**arguments)
        except TypeError as error:
            asked = " and its settings table" if settings is not None else ""
            raise EngineAssemblyError(
                f"{reference} could not be constructed with the arguments the {seam} seam "
                f"is given (the event sink{asked}): {error}"
            ) from None

    return Adapters(
        call=built("call", config.adapters.call),
        channel=built("companion_channel", config.adapters.companion_channel),
        agents={
            agent: built(f"agent.{agent}", reference)
            for agent, reference in config.adapters.agents.items()
        },
    )


def _share_the_app_server(adapters: Adapters) -> None:
    """Hand the Call adapter the app-server the Codex Agent adapter owns.

    One `codex app-server` exists in this engine, and the Codex Agent adapter is
    the component that spawns, owns and reaps it. The Call adapter's realtime
    route rides the same process rather than starting a second one, so somebody
    has to introduce them — and the only place allowed to know two adapters at
    once is this root.

    The wiring is deliberately shaped so nothing about it can be half-done:

    - it happens after construction and before any `connect`, so no adapter can
      be asked to open before it holds what it needs;
    - a Call adapter that wants a transport and finds no Codex Agent adapter
      configured stops the assembly by name, rather than starting an engine
      whose voice surface can never come up;
    - a Call adapter that wants none is left alone, so a null or fake
      implementation needs to know nothing about any of this.

    Ownership itself does not move. The Call adapter is handed the component,
    not a socket path, precisely so it has nothing it could reopen: when that
    process dies, what the Call adapter sees is its own connection ending, which
    it reports upward as a dropped call and nothing more.
    """
    call = adapters.call
    consumer = getattr(call, "use_app_server", None)
    if consumer is None:
        return
    codex = adapters.agents.get(AgentKind.CODEX)
    provider = getattr(codex, "app_server", None) if codex is not None else None
    if provider is None:
        raise EngineAssemblyError(
            f"[adapters] call names {type(call).__module__}:{type(call).__name__}, which rides "
            "the codex app-server the Codex Agent adapter owns. Name one in "
            "[adapters.agents] codex, or configure a Call adapter that needs none"
        )
    consumer(provider)


def _instruction_context(config: EngineConfig) -> InstructionContext:
    """The real control-plane CLI and parser form generated instructions may name.

    Stated, then derived, then refused. Configuration wins because the bundle
    moves the binary and is the only thing that knows where to; otherwise the
    console script beside this interpreter is the one this installation ships,
    and it is used only after it is found to be there and runnable. A generated
    instruction naming a CLI that does not exist is an invented detail, which is
    the first thing those instructions themselves forbid — so the last branch is
    a refusal, not a guess.
    """
    stated = config.control_plane_cli
    if stated is not None:
        if not _runnable(stated):
            raise EngineAssemblyError(
                f"[delegate] cli names {stated}, which is not there or cannot be run"
            )
        command = stated
    else:
        derived = Path(sys.executable).parent / CONTROL_PLANE_CLI_NAME
        if not _runnable(derived):
            raise EngineAssemblyError(
                f"no control-plane CLI to tell a generated thread about: {derived} is not "
                "there or cannot be run. Install this package so its console script exists, "
                "or name the one this installation ships in [delegate] cli"
            )
        command = derived

    return InstructionContext(
        cli=ControlPlaneCli(
            command=command,
            version=__version__,
            socket_path=config.socket_path,
        ),
    )


def _runnable(command: Path) -> bool:
    """A file that is really there and really executable. Nothing weaker counts."""
    return command.is_file() and os.access(command, os.X_OK)


def _inventory(config: EngineConfig) -> tuple[SeamLoad, ...]:
    """The configured half of ADR 0003's question, which only this root knows.

    The loaded half is not recorded here on purpose. What this root constructed
    is what it was told to construct — recording that and calling it an
    observation is the echo the ADR exists to stop. The engine asks each adapter
    what it *is* instead, every time it is asked.
    """
    return tuple(
        SeamLoad(seam=seam, configured=reference)
        for seam, reference in config.adapters.as_mapping().items()
    )
