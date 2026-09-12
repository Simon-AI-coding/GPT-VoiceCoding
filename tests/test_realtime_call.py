"""The bridge-owned Live Call, against a scripted app-server and a fake audio path.

No real codex runs, no microphone opens and nothing dials a network. What is
under test is the part that decides things: the signalling conversation, the
classification of a `speak`, what happens to a thread when a Delegated Turn goes
wrong, and which of the four ways a call can stop are reported as which event.

The payload shapes are the ones codex 0.148.0 really uses — read out of its own
generated protocol bindings and out of the binary's request table
(`thread/realtime/{start,appendSpeech,stop}`, `ThreadRealtimeStartParams`'s
`realtimeStartInstructions` and `transport`), not invented here.

The edge cases the build issue named each have a test: an `ensure_call` on top
of a call that is already up, a `speak` with nothing to speak into, a call that
drops mid-`speak`, a Delegated Turn that never answers, and an `end_call` in the
middle of the handshake.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codex_fake import FakeAppServer, FakeRemoteError
from fakes import FakeCompanionChannel, instruction_context
from gpt_voicecoding.adapters.call.realtime import (
    APPROVAL_POLICY,
    CODEX_RESPONSE_ITEM_PREFIX,
    CODEX_RESPONSES_AS_ITEMS,
    DEFAULT_REALTIME_MODEL,
    DELEGATION_ACK_FILLER,
    INCLUDE_STARTUP_CONTEXT,
    SANDBOX,
    TURN_FAILED,
    TURN_NOT_STARTED,
    DelegatedTurnError,
    RealtimeCallAdapter,
    RealtimeCallSettings,
    SettingsError,
    ThreadGoneError,
    cues,
    realtime_call,
    webrtc,
)
from gpt_voicecoding.adapters.call.realtime.adapter import _item_text
from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, attach
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.codex_app_server.wire import RemoteError
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard
from gpt_voicecoding.seams.call import (
    CALL_AGENT_REMARK_ALLOWANCE_BYTES,
    CODEX_BYTES_PER_TOKEN,
    HANDOVER_BUDGET_BYTES,
    REALTIME_ASSISTANT_OUTPUT_TOKEN_BUDGET,
    RETURN_LEG_BUDGET_BYTES,
    CallDropped,
    CallEnded,
    CallStarted,
    CallState,
    Cue,
    Dial,
    SpokenBrief,
    SpokenRosterBrief,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.control_plane import Action, Request
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import RequestId, SessionName
from gpt_voicecoding.seams.verify import VerifyOutcome
from realtime_fake import (
    ANSWER_SDP,
    OFFER_SDP,
    FakeCueOutput,
    FakeTransport,
    SharedAppServer,
    delegated_script,
    realtime_script,
)

THREAD = "01a02110-d18f-74a0-916d-de1208e9977a"
CALL_AGENT_INSTRUCTIONS = "speak the Session Name; never invent a detail"
CALL_VOICE_PROSE = "Speak in short sentences. Wait to be asked before giving detail."
DELEGATED_RULES = "act only through the control-plane CLI"

#: `[delegate] model` as this suite states it. One value for the Call Agent and
#: for every Delegated Turn (#270), so the two assertions read the same name.
DELEGATED_MODEL = "a-model-the-user-chose"


def dial(*hand_over: object) -> Dial:
    """What Bridge Core hands this adapter: two audiences and a hand-over."""
    return Dial(voice=CALL_VOICE_PROSE, agent=CALL_AGENT_INSTRUCTIONS, hand_over=tuple(hand_over))


def brief(newest: str) -> SpokenBrief:
    """One Session Brief in Briefing's own words, as the seam carries it."""
    return SpokenBrief(
        name=SessionName("voicecoding", "the dial"),
        agent="codex",
        state="waiting for your decision",
        newest=newest,
        decision=("asked: ship it?", "option: yes", "option: no"),
        answerable_here="from here",
        last_activity_at="not read",
    )


#: One of every hand-over kind, in shapes assembly labels differently. Seam
#: carriers, not assembled text: the two budget invariants below run each of
#: these through `_item_text` and measure the result against what it was charged.
HANDOVER_ITEM_EXAMPLES = (
    SpokenRosterBrief(
        counts="sessions: 1 waiting for your decision, 1 running, 1 finished",
        rows=(
            "build — codex:abc — running",
            "docs — claude:def:12 — finished",
            "voicecoding · the dial — codex:ghi — waiting for your decision",
        ),
    ),
    brief("it stopped on a question"),
    SpokenBrief(
        name="a",
        agent="codex",
        state="running",
        newest="nothing said yet",
        decision=(),
        answerable_here="at the terminal",
        last_activity_at="not read",
    ),
    SpokenBrief(
        name="voicecoding · the dial",
        agent="codex",
        state="waiting for your decision",
        newest="I got this far",
        decision=("asked: Which base?",),
        answerable_here="from here",
        last_activity_at="not read",
    ),
)


_names = iter(range(10_000))


class Sink:
    """The event sink, recording what the adapter raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind. See `test_codex_agent`."""
    home = Path("/tmp") / f"vc-call-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "app-server.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: Any) -> RealtimeCallSettings:
    """Settings whose waits are short enough for a test to actually spend them."""
    return RealtimeCallSettings(
        workspace=Path("/tmp"),
        connect_timeout_seconds=0.4,
        request_timeout_seconds=2.0,
        delegated_turn_timeout_seconds=0.4,
        **overrides,
    )


async def riding(
    server: FakeAppServer,
    sink: Sink,
    *,
    transport: FakeTransport | None = None,
    settings: RealtimeCallSettings | None = None,
    cue_player: FakeCueOutput | None = None,
    delegated_turn_model: str = DELEGATED_MODEL,
    delegated_turn_effort: str | None = None,
) -> tuple[RealtimeCallAdapter, FakeTransport]:
    """An adapter wired to a scripted app-server, exactly as the root wires it."""
    audio = transport or FakeTransport()
    adapter = RealtimeCallAdapter(
        delegated_turn_model=delegated_turn_model,
        delegated_turn_effort=delegated_turn_effort,
        sink=sink,
        settings=settings or quick(),
        transport_factory=lambda: audio,
        cue_player=cue_player or FakeCueOutput(),
    )
    shared = SharedAppServer(connection=None)
    connection = await attach(
        server.path,
        version="0",
        settings=CodexSettings(request_timeout_seconds=2.0),
        on_notification=shared.heard,
        experimental=True,
    )
    shared.connection = connection
    adapter.use_app_server(shared)  # type: ignore[arg-type]
    await adapter.connect()
    return adapter, audio


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


class _Stream:
    """What `sounddevice.RawOutputStream` is, as far as `CuePlayer` can tell."""

    def __init__(self, blocks_on: tuple[threading.Event, threading.Event] | None) -> None:
        self._blocks_on = blocks_on

    def start(self) -> None:
        return None

    def write(self, _pcm: bytes) -> None:
        if self._blocks_on is not None:
            writing, release = self._blocks_on
            writing.set()
            release.wait(2.0)

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Streams:
    """A stand-in device. Only the *first* stream opened holds its write open."""

    def __init__(self, *, blocks_on: tuple[threading.Event, threading.Event] | None) -> None:
        self._blocks_on = blocks_on
        self.opened: list[dict[str, Any]] = []

    def __call__(self, **parameters: Any) -> _Stream:
        self.opened.append(parameters)
        return _Stream(self._blocks_on if len(self.opened) == 1 else None)


@contextmanager
def _sounddevice(streams: _Streams) -> Iterator[None]:
    """`sounddevice`, for the length of a test. CI does not install the real one."""
    stood_in = types.SimpleNamespace(RawOutputStream=streams)
    was = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = stood_in  # type: ignore[assignment]
    try:
        yield
    finally:
        if was is None:
            del sys.modules["sounddevice"]
        else:
            sys.modules["sounddevice"] = was


class TestBringingACallUp:
    def test_a_project_only_name_carries_no_empty_task_line(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial(replace(brief("done"), name=SessionName("Project"))))
                text = server.calls_to("thread/realtime/start")[0]["initialItems"][0]["text"]
                assert "  project: Project" in text
                assert "  task:" not in text
                await adapter.aclose()

        asyncio.run(scenario())

    def test_running_engine_refreshes_catalog_and_forgets_agent_after_restart(
        self, socket_path: Path
    ) -> None:
        from fakes import FakeAgent
        from gpt_voicecoding.config import load
        from gpt_voicecoding.control_plane.client import ask
        from gpt_voicecoding.engine.composition import Engine
        from gpt_voicecoding.seams.agent import SessionStopped
        from test_engine import CODEX, configured

        async def scenario() -> None:
            config = load(configured(socket_path.parent))
            async with FakeAppServer(socket_path) as server:
                for generation, catalog in enumerate(
                    (
                        [],
                        [
                            {
                                "model": "new-model",
                                "hidden": False,
                                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                            }
                        ],
                    )
                ):
                    server.answers("model/list", {"data": catalog})
                    realtime_script(server, thread_id=f"call-{generation}")
                    shared = SharedAppServer(connection=None)
                    connection = await attach(
                        server.path,
                        version="0",
                        settings=CodexSettings(request_timeout_seconds=2.0),
                        on_notification=shared.heard,
                        experimental=True,
                    )
                    shared.connection = connection

                    def agent_factory(shared=shared, **arguments):
                        agent = FakeAgent(**arguments)
                        agent.app_server = shared
                        return agent

                    def call_factory(**arguments):
                        return RealtimeCallAdapter(
                            **arguments,
                            settings=quick(),
                            transport_factory=FakeTransport,
                            cue_player=FakeCueOutput(),
                        )

                    factories = {
                        "fakes:FakeCall": call_factory,
                        "fakes:FakeAgent": agent_factory,
                        "fakes:FakeCompanionChannel": FakeCompanionChannel,
                    }
                    engine = Engine.assemble(config, factory_of=factories.__getitem__)
                    await engine.start()
                    try:
                        assert len(server.calls_to("model/list")) == generation + 1
                        for _ in range(2):
                            models = await ask(Request(Action.MODELS), path=engine.socket_path)
                            assert models.data["models"] == (
                                [{"model": "new-model", "efforts": ["high"]}] if generation else []
                            )
                        assert len(server.calls_to("model/list")) == generation + 1
                        assert (
                            "call_agent"
                            not in (await ask(Request(Action.STATUS), path=engine.socket_path)).data
                        )
                        for name in ("duty", "voice"):
                            assert (
                                await ask(
                                    Request(Action.SWITCH, {"name": name, "on": True}),
                                    path=engine.socket_path,
                                )
                            ).ok
                        await engine.core.dispatch(SessionStopped(target=CODEX))
                        status = await ask(Request(Action.STATUS), path=engine.socket_path)
                        assert status.data["call_id"]
                        assert status.data["call_agent"]["model"] == "gpt-5"
                        assert (
                            server.calls_to("thread/realtime/start")[-1]["threadId"]
                            == f"call-{generation}"
                        )
                        assert len(server.calls_to("thread/start")) == generation + 1
                    finally:
                        await engine.aclose()
                        await connection.aclose()

        asyncio.run(scenario())

    def test_user_dial_replaces_agent_system_dial_continues_and_forget_is_between_calls(
        self, socket_path: Path
    ) -> None:
        from gpt_voicecoding.seams.agent import SessionStopped
        from test_engine import CODEX

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server)
                adapter, audio = await riding(server, Sink())
                now = [0.0]
                core = BridgeCore(
                    state=BridgeState(
                        switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
                    ),
                    call=adapter,
                    channel=FakeCompanionChannel(),
                    agents={},
                    clock=lambda: now[0],
                    instruction_context=instruction_context(),
                )
                plane = ControlPlane(core)
                assert (await plane.handle(Request(Action.LIVE))).ok
                refusal = await plane.handle(Request(Action.FORGET_CALL_AGENT))
                assert not refusal.ok
                assert (await plane.handle(Request(Action.LIVE))).ok
                audio.closed = False
                now[0] += core.status().cool_down_remaining
                for name in ("duty", "voice"):
                    assert (
                        await plane.handle(Request(Action.SWITCH, {"name": name, "on": True}))
                    ).ok
                await core.dispatch(SessionStopped(target=CODEX))
                assert (await plane.handle(Request(Action.STATUS))).data["call_id"]
                assert len(server.calls_to("thread/start")) == 1
                assert (await plane.handle(Request(Action.LIVE))).ok
                audio.closed = False
                assert (await plane.handle(Request(Action.LIVE))).ok
                assert len(server.calls_to("thread/start")) == 2
                assert (await plane.handle(Request(Action.LIVE))).ok
                assert (await plane.handle(Request(Action.FORGET_CALL_AGENT))).ok
                assert "call_agent" not in (await plane.handle(Request(Action.STATUS))).data
                audio.closed = False
                now[0] += core.status().cool_down_remaining
                await core.dispatch(SessionStopped(target=CODEX))
                assert (await plane.handle(Request(Action.STATUS))).data["call_id"]
                assert len(server.calls_to("thread/start")) == 3
                await adapter.aclose()

        asyncio.run(scenario())

    def test_status_carries_live_call_agent_usage(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server)
                adapter, _ = await riding(server, Sink(), delegated_turn_effort="high")
                core = BridgeCore(
                    state=BridgeState(
                        switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
                    ),
                    call=adapter,
                    channel=FakeCompanionChannel(),
                    agents={},
                    instruction_context=instruction_context(),
                )
                plane = ControlPlane(core)
                assert "call_agent" not in (await plane.handle(Request(Action.STATUS))).data
                await plane.handle(Request(Action.LIVE))
                usage = {
                    "totalTokens": 36000,
                    "inputTokens": 30000,
                    "outputTokens": 6000,
                    "reasoningOutputTokens": 2000,
                    "cachedInputTokens": 12000,
                }
                await server.notify_all(
                    "thread/tokenUsage/updated",
                    {
                        "threadId": "thread-1",
                        "tokenUsage": {"total": usage, "last": usage, "modelContextWindow": 60000},
                    },
                )
                # A reply on the same connection follows the notification in wire order.
                server.answers("model/list", {"data": []})
                await core.models()
                reply = await plane.handle(Request(Action.STATUS))
                agent = reply.data["call_agent"]
                assert agent["effort"] == "high"
                assert agent["context_percent"] == 50
                assert agent["total"] == {
                    "input": 30000,
                    "output": 6000,
                    "reasoning": 2000,
                    "cached": 12000,
                }
                assert agent["last"] == agent["total"]
                assert reply.data["engine_version"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_control_plane_dial_uses_voice_model_and_shared_effort(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server)
                adapter, _ = await riding(
                    server,
                    Sink(),
                    settings=quick(voice="ember", realtime_model="configured-realtime"),
                    delegated_turn_effort="high",
                )
                core = BridgeCore(
                    state=BridgeState(
                        switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
                    ),
                    call=adapter,
                    channel=FakeCompanionChannel(),
                    agents={},
                    instruction_context=instruction_context(),
                )
                reply = await ControlPlane(core).handle(Request(Action.LIVE))
                assert reply.ok
                assert (
                    server.calls_to("thread/start")[0]["config"]["model_reasoning_effort"] == "high"
                )
                dial = server.calls_to("thread/realtime/start")[0]
                assert dial["voice"] == "ember"
                assert dial["model"] == "configured-realtime"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_model_catalog_is_cached_at_startup(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                server.answers(
                    "model/list",
                    {
                        "data": [
                            {
                                "model": "chosen-model",
                                "hidden": False,
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low"},
                                    {"reasoningEffort": "high"},
                                ],
                            },
                            {
                                "model": "hidden-model",
                                "hidden": True,
                                "supportedReasoningEfforts": [],
                            },
                        ]
                    },
                )
                adapter, _ = await riding(server, Sink())
                core = BridgeCore(
                    state=BridgeState(
                        switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
                    ),
                    call=adapter,
                    channel=FakeCompanionChannel(),
                    agents={},
                    instruction_context=instruction_context(),
                )
                await core.models()
                plane = ControlPlane(core)
                for _ in range(2):
                    reply = await plane.handle(Request(Action.MODELS))
                    assert reply.data == {
                        "models": [{"model": "chosen-model", "efforts": ["low", "high"]}]
                    }
                assert len(server.calls_to("model/list")) == 1
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_handshake_is_the_route_the_prototype_proved(self, socket_path: Path) -> None:
        """Thread, offer, realtime start, SDP answer, started, audio up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.UP
                assert snapshot.call_id == THREAD
                start = server.calls_to("thread/realtime/start")[0]
                assert start["threadId"] == THREAD
                assert start["transport"] == {"type": "webrtc", "sdp": OFFER_SDP}
                assert start["realtimeStartInstructions"] == CALL_AGENT_INSTRUCTIONS
                assert audio.answers == [ANSWER_SDP]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_dial_reaches_two_audiences_through_three_slots(self, socket_path: Path) -> None:
        """ADR 0018's mapping, pinned: `prompt`, `realtimeStartInstructions`, `initialItems`.

        The slot-swap proved which half each reaches — `realtimeStartInstructions`
        0/6 on the Voice, `prompt` 6/6, `initialItems` 5/6 and silent (#175 Q4,
        #179). This is the only place in the system that knows any of these three
        names, and each hand-over item becomes exactly one entry under
        `role: developer`.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(
                    dial(
                        SpokenRosterBrief(
                            counts="sessions: 1 finished, 1 running",
                            rows=(
                                "build — codex:abc — running",
                                "voicecoding · the dial — codex:def — finished",
                            ),
                        ),
                        brief("it finished"),
                    )
                )

                start = server.calls_to("thread/realtime/start")[0]
                assert start["prompt"] == CALL_VOICE_PROSE
                assert start["realtimeStartInstructions"] == CALL_AGENT_INSTRUCTIONS
                assert [item["role"] for item in start["initialItems"]] == [
                    "developer",
                    "developer",
                ]
                assert start["initialItems"][0]["text"] == (
                    "sessions: 1 finished, 1 running\n"
                    "  build — codex:abc — running\n"
                    "  voicecoding · the dial — codex:def — finished"
                )
                assert start["initialItems"][1]["text"].endswith("  last activity: not read")
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_three_dial_time_switches_are_this_adapters_constants(
        self, socket_path: Path
    ) -> None:
        """No caller varies them, so they are pinned here rather than on the `Dial`.

        `delegationAckFiller` off is Round 1 Q9's wordiness removed;
        `includeStartupContext` off keeps a 5,300-token scan of the user's recent
        threads out of the Voice's prompt (ADR 0018 as amended by #179);
        `codexResponsesAsItems` on with a prefix is the recorded decision and
        **buys no observability** — two live spoken calls produced no item
        carrying an agent answer, so nothing here is built on it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["delegationAckFiller"] is DELEGATION_ACK_FILLER is False
                assert start["includeStartupContext"] is INCLUDE_STARTUP_CONTEXT is False
                assert start["codexResponsesAsItems"] is CODEX_RESPONSES_AS_ITEMS is True
                assert start["codexResponseItemPrefix"] == CODEX_RESPONSE_ITEM_PREFIX
                await adapter.aclose()

        asyncio.run(scenario())

    def test_no_item_reaches_the_wire_larger_than_it_was_budgeted_at(self) -> None:
        """The invariant `HANDOVER_BUDGET_BYTES` is a promise about, checked here.

        The seam counts a hand-over before this module assembles it, so the count
        has to be an upper bound on what assembly produces — otherwise a `Dial`
        the seam accepted is a request the wire refuses, which is an error and
        not a truncation. It was not one: counting the words without the labels
        around them let 8,192 budgeted bytes reach the wire as 8,242 (#194
        review). Asserted against the real assembly rather than against a
        restatement of it, so a longer label fails here rather than on a call.
        """
        for item in HANDOVER_ITEM_EXAMPLES:
            assembled = len(_item_text(item).encode("utf-8"))
            assert assembled <= item.size_in_bytes, f"{type(item).__name__} overflows its budget"

    def test_every_item_leaves_room_for_the_rounding_codex_does_on_it(self) -> None:
        """The slack `HANDOVER_BUDGET_BYTES` spends on codex rounding each item up.

        codex counts `ceil(bytes / 4)` **per item** and sums those, so a hand-over
        of exactly the budget could still be refused if the seam's count were only
        an upper bound: 128 items each rounded up would add ninety-six tokens the
        total count never sees. It cannot, because `_bytes_of` charges
        `WIRE_LINE_OVERHEAD_BYTES` for every carried string, which is far more
        than the at-most three bytes each item's rounding costs. Asserted against
        the real assembly for the same reason the test above is: a longer label
        eats this margin, and it should fail here rather than on a call.
        """
        for item in HANDOVER_ITEM_EXAMPLES:
            assembled = len(_item_text(item).encode("utf-8"))
            spare = item.size_in_bytes - assembled
            assert spare >= CODEX_BYTES_PER_TOKEN - 1, (
                f"{type(item).__name__} has {spare} bytes of slack, too few to round up in"
            )

    def test_the_return_leg_ceiling_is_codexs_own_budget_less_what_rides_with_it(self) -> None:
        """#302: the ceiling on the answer the Call Agent hands back to the Voice.

        Derived, never chosen. codex cuts the Call Agent's answer at
        `REALTIME_ASSISTANT_OUTPUT_TOKEN_BUDGET` estimated tokens at
        `ceil(bytes / 4)`, and it prepends `CODEX_RESPONSE_ITEM_PREFIX` and two
        newlines *before* truncating, so those bytes are ours out of the budget.
        The prefix is measured from the constant rather than written as a
        literal, so a change to it moves this ceiling with it.
        """
        assert RETURN_LEG_BUDGET_BYTES == (
            REALTIME_ASSISTANT_OUTPUT_TOKEN_BUDGET * CODEX_BYTES_PER_TOKEN
            - len((CODEX_RESPONSE_ITEM_PREFIX + "\n\n").encode("utf-8"))
            - CALL_AGENT_REMARK_ALLOWANCE_BYTES
        )

    def test_the_return_leg_ceiling_leaves_the_call_agent_room_for_a_remark(self) -> None:
        """The allowance is a deliberate over-estimate, in `WIRE_LINE_OVERHEAD_BYTES`' manner.

        On the 2026-09-09 10:48 tracer run the Call Agent returned engine results
        verbatim and its own remarks were separate messages of 43-70 bytes, so
        256 is roughly four times the widest one observed. It is an allowance and
        not a measurement; this holds the direction it may be wrong in.
        """
        assert CALL_AGENT_REMARK_ALLOWANCE_BYTES >= 70
        assert RETURN_LEG_BUDGET_BYTES < HANDOVER_BUDGET_BYTES

    def test_a_user_opened_dial_carries_exactly_one_item(self, socket_path: Path) -> None:
        """#167 Q6: a call the user opened gets no hand-over, only why it exists."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start.get("initialItems", []) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voice_thread_is_pinned_approval_free_in_a_full_sandbox(
        self, socket_path: Path
    ) -> None:
        """The trade recorded in legacy issue #19, asserted rather than reviewed.

        Approval-free is a decision already taken, and the sandbox is the only
        one in which the control-plane CLI's `AF_UNIX` connect succeeds. Neither
        is a configuration key, so neither may drift without this test noticing.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                started = server.calls_to("thread/start")[0]
                assert started["approvalPolicy"] == APPROVAL_POLICY == "never"
                assert started["sandbox"] == SANDBOX == "danger-full-access"
                assert started["cwd"] == "/tmp"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_realtime_model_the_backend_still_accepts_is_sent(self, socket_path: Path) -> None:
        """The model rides at the top level of the start, and it is not codex's default.

        codex serializes a `session.model` of its own on this path no matter how
        it is configured, and on 2026-08-22 the backend stopped accepting the
        value it picks. The refusal names the field — `Field \u0060session.model\u0060
        is not allowed for this Codex realtime session` — but what is refused is
        the *value*; the allowlist moved (#35, openai/codex#40140). Stating a
        model that is still on the allowlist is what brings the call up.

        This value is granted by the far side, not derived here. When it expires
        the call fails the same way again: **re-run the probe and re-derive the
        model — do not loosen this assertion.** A test that stopped checking
        which model was sent would let the next expiry look like our bug.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["model"] == DEFAULT_REALTIME_MODEL == "gpt-live-1-codex"
                assert "session" not in start, "the model rides at the top level"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_live_thread_is_started_on_the_delegated_turns_model(
        self, socket_path: Path
    ) -> None:
        """The Call Agent's model is the live thread's, and it is `[delegate] model` (#270).

        There is no Call Agent model field anywhere on this wire: the acting
        half of a call runs on whatever backs the thread the call is on. A
        `thread/start` that named none handed it to `~/.codex/config.toml`, a
        file this product does not write — and on 2026-09-07 that file said
        `gpt-6-astra`, which the running daemon refused on all seventeen turns.
        So the model is stated, and it is the one value the user already sets
        for a Delegated Turn.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                started = server.calls_to("thread/start")[0]
                assert started["model"] == DELEGATED_MODEL
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voices_model_and_the_call_agents_are_two_different_models(
        self, socket_path: Path
    ) -> None:
        """One call, two models, and neither slot may be filled with the other's.

        `thread/realtime/start`'s `model` overrides the *realtime* model for the
        session — that is the Voice. `thread/start`'s is the thread's backing
        model — that is the Call Agent. They are set from different settings and
        this asserts both at once, because the failure that cost the user a
        whole call was one of them being left unsaid.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(
                    server, Sink(), settings=quick(realtime_model="gpt-live-2-later")
                )

                await adapter.ensure_call(dial())

                assert server.calls_to("thread/start")[0]["model"] == DELEGATED_MODEL
                assert server.calls_to("thread/realtime/start")[0]["model"] == "gpt-live-2-later"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_stated_realtime_model_overrides_the_default(self, socket_path: Path) -> None:
        """The operator's escape hatch when the allowlist moves again, exercised."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(
                    server, Sink(), settings=quick(realtime_model="gpt-live-2-later")
                )

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["model"] == "gpt-live-2-later"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_refused_start_says_which_realtime_model_was_asked_for(
        self, socket_path: Path, caplog
    ) -> None:
        """The upstream words verbatim, plus the value this engine actually sent.

        The 2026-08-22 outage was an upstream refusal of the model *value*
        reported as a refusal of the *field*, and our own failure line never
        said which model had gone out — so the log agreed with the misleading
        reading for two days (#35). Upstream still speaks for itself; we add
        only what upstream declined to mention.
        """
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.call.realtime.adapter")

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError(
                        '{"detail":"Field `session.model` is not allowed for '
                        'this Codex realtime session"}'
                    )

                server.answers("thread/realtime/start", refuse)
                adapter, _ = await riding(server, Sink())

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.DOWN
                logged = " ".join(record.getMessage() for record in caplog.records)
                assert "Field `session.model` is not allowed" in logged
                assert DEFAULT_REALTIME_MODEL in logged
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_already_up_is_reported_not_reopened(self, socket_path: Path) -> None:
        """`ensure_call` is idempotent for the adapter; the invariant is Core's."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)

                first = await adapter.ensure_call(dial())
                second = await adapter.ensure_call(dial())

                assert first == second
                assert len(server.calls_to("thread/start")) == 1
                assert len(sink.of(CallStarted)) == 1
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_that_never_connects_leaves_nothing_running(self, socket_path: Path) -> None:
        """The audio never arrives, so the thread is stopped and the state is down."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink, transport=FakeTransport(connects=False))

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.DOWN
                assert audio.closed
                assert server.calls_to("thread/realtime/stop")
                assert sink.of(CallStarted) == []
                assert (await adapter.call_state()).state is CallState.DOWN
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_is_never_opened_on_no_instructions(self) -> None:
        """Nothing here invents house rules when the hub generated none.

        The check left this adapter with #194: a `Dial` refuses its own empty
        halves at construction (`seams/call.py`), so an argument this method
        could refuse cannot be built. One rule, one place, and an earlier one
        than the wire.
        """
        with pytest.raises(ValueError):
            Dial(voice="", agent=CALL_AGENT_INSTRUCTIONS)
        with pytest.raises(ValueError):
            Dial(voice=CALL_VOICE_PROSE, agent="   ")

    def test_hanging_up_during_the_handshake_abandons_it(self, socket_path: Path) -> None:
        """`end_call` while connecting: the attempt stops, and nothing is reported up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport(connects=False)
                adapter, _ = await riding(server, sink, transport=audio)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                ended = await adapter.end_call()
                snapshot = await opening

                assert ended.state is CallState.DOWN
                assert snapshot.state is CallState.DOWN
                assert audio.closed
                # Nothing ever started, so nothing is announced as having ended.
                assert sink.of(CallStarted) == []
                assert sink.of(CallEnded) == []
                assert sink.of(CallDropped) == []
                await adapter.aclose()

        asyncio.run(scenario())


class TestHangingUpMidHandshake:
    """`end_call` during connection setup, at each step it can arrive at."""

    def test_a_hang_up_before_the_thread_exists_stops_the_handshake(
        self, socket_path: Path
    ) -> None:
        """The window that matters: `thread/start` has been sent and not answered.

        An attempt that only became visible once the thread had a name would
        leave `end_call` nothing to end here — and the handshake would carry on
        and bring up a call the user had already hung up.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)

                slow = asyncio.Event()

                async def dawdle(_params: dict) -> dict:
                    await slow.wait()
                    return {"thread": {"id": THREAD}}

                server.answers("thread/start", dawdle)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                ended = await adapter.end_call()
                slow.set()
                snapshot = await opening

                assert ended.state is CallState.DOWN
                # The invariant: an `ensure_call` that was hung up never comes
                # back UP. Anything weaker and the hang-up is only advisory.
                assert snapshot.state is CallState.DOWN
                assert (await adapter.call_state()).state is CallState.DOWN
                assert audio.closed
                assert sink.of(CallStarted) == []
                # The thread `thread/start` did create is not left behind.
                assert server.calls_to("thread/realtime/stop") == [{"threadId": THREAD}]
                assert server.calls_to("thread/realtime/start") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hang_up_before_the_sdp_answer_stops_it(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                server.answers("thread/realtime/start", {})  # no SDP ever comes back
                sink = Sink()
                adapter, audio = await riding(server, sink)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                await adapter.end_call()
                snapshot = await opening

                assert snapshot.state is CallState.DOWN
                assert audio.closed
                assert sink.of(CallStarted) == []
                await adapter.aclose()

        asyncio.run(scenario())


class TestSpeaking:
    def test_the_opening_is_one_append_with_the_focus_before_the_roster(self, socket_path):
        async def scenario():
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())
                try:
                    result = await adapter.speak(
                        (brief("Ready."), SpokenRosterBrief(counts="the others: 1 finished")),
                        request_id=rid(),
                    )
                    assert result.is_delivered
                    (append,) = server.calls_to("thread/realtime/appendSpeech")
                    assert append["text"].startswith(
                        "voicecoding · the dial — codex — waiting for your decision\n"
                        "  project: voicecoding\n  task: the dial\n"
                    )
                    assert append["text"].endswith("\n\nthe others: 1 finished")
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())

    def test_speaking_into_a_live_call_is_delivered(self, socket_path: Path) -> None:
        """The brief goes out assembled, and every word in it is Briefing's (#194).

        What this adapter adds is the labels and the order — `newest:`, the
        decision lines under the header, `answer:` last. The five state words,
        the omission wording and the decision's own sentences all arrive already
        chosen, so nothing here can describe a Session a second way.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                receipt = await adapter.speak((brief("that session stopped"),), request_id=rid())

                assert receipt.outcome is Delivery.DELIVERED
                assert server.calls_to("thread/realtime/appendSpeech") == [
                    {
                        "threadId": THREAD,
                        "text": (
                            "voicecoding · the dial — codex — waiting for your decision\n"
                            "  project: voicecoding\n"
                            "  task: the dial\n"
                            "  newest: that session stopped\n"
                            "  asked: ship it?\n"
                            "  option: yes\n"
                            "  option: no\n"
                            "  answer: from here\n"
                            "  last activity: not read"
                        ),
                    }
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_speaking_with_no_call_up_fails_closed(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                receipt = await adapter.speak((brief("anyone there"),), request_id=rid())

                assert receipt.outcome is Delivery.FAILED
                assert "no call is up" in receipt.reason
                assert server.calls_to("thread/realtime/appendSpeech") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_refused_speech_is_a_failure_in_codex_own_words(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no realtime session on that thread")

                server.answers("thread/realtime/appendSpeech", refuse)
                receipt = await adapter.speak((brief("hello"),), request_id=rid())

                assert receipt.outcome is Delivery.FAILED
                assert "no realtime session on that thread" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_speech_accepted_onto_audio_that_had_gone_is_unknown(self, socket_path: Path) -> None:
        """The one grading rule this seam exists to get right.

        The app-server took the words, so calling it FAILED would make Bridge
        Core re-route a notice that may well have been heard — which is exactly
        the duplicate-call bug this adapter's contract is written against.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())
                await adapter.ensure_call(dial())

                def go_quiet_then_accept(_params: dict) -> dict:
                    audio.go_quiet()
                    return {}

                server.answers("thread/realtime/appendSpeech", go_quiet_then_accept)
                receipt = await adapter.speak((brief("you are needed"),), request_id=rid())

                assert receipt.outcome is Delivery.UNKNOWN
                assert "already gone" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_an_app_server_that_dies_mid_speech_is_unknown_not_failed(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                async def die(_params: dict) -> dict:
                    await server.drop_everyone()
                    return {}

                server.answers("thread/realtime/appendSpeech", die)
                receipt = await adapter.speak((brief("you are needed"),), request_id=rid())

                assert receipt.outcome is Delivery.UNKNOWN
                await adapter.aclose()

        asyncio.run(scenario())


class TestHowACallStops:
    def test_ending_a_call_is_announced_and_idempotent(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)
                await adapter.ensure_call(dial())

                first = await adapter.end_call()
                second = await adapter.end_call()

                assert first.state is second.state is CallState.DOWN
                assert audio.closed
                assert [event.call_id for event in sink.of(CallEnded)] == [THREAD]
                assert sink.of(CallDropped) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_already_gone_still_ends_without_raising(self, socket_path: Path) -> None:
        """Bridge Core's ledger needs a clean answer, not an exception."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no realtime session on that thread")

                server.answers("thread/realtime/stop", refuse)
                snapshot = await adapter.end_call()

                assert snapshot.state is CallState.DOWN
                assert "already gone" in sink.of(CallEnded)[0].detail
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_realtime_session_closing_is_a_drop(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/closed",
                    {"threadId": THREAD, "reason": "the backend hung up"},
                )
                await asyncio.sleep(0.05)

                dropped = sink.of(CallDropped)
                assert [event.call_id for event in dropped] == [THREAD]
                assert "the backend hung up" in dropped[0].detail
                assert (await adapter.call_state()).state is CallState.DOWN
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_audio_going_away_is_a_drop_reported_once(self, socket_path: Path) -> None:
        """A call dropped mid-`speak` is news, and it is news exactly once."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)
                await adapter.ensure_call(dial())

                audio.lose("the peer connection failed")
                await asyncio.sleep(0.05)
                receipt = await adapter.speak((brief("you are needed"),), request_id=rid())

                assert len(sink.of(CallDropped)) == 1
                assert receipt.outcome is Delivery.FAILED
                assert "no call is up" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_whose_audio_went_quiet_is_not_reported_as_up(self, socket_path: Path) -> None:
        """`call_state` reads the connection, not this adapter's own bookkeeping."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())
                await adapter.ensure_call(dial())

                audio.go_quiet()

                assert (await adapter.call_state()).state is CallState.CONNECTING
                await adapter.aclose()

        asyncio.run(scenario())


class TestWhatTheCallRaisesUpward:
    def test_the_users_speech_goes_up_as_a_transcript(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hand_off_is_written_down_and_raises_no_event_of_its_own(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ADR 0018: no `HandoffRequested` event. The closed event set stays closed.

        A `handoff_request` is the one observable proof the acting half was
        reached — the model's own claim to have acted is trusted by nothing
        (8/8 false hang-up claims, #179). So it is logged, and the engine still
        acts only on the Call Agent's own `bridgectl` run. What it *does* raise
        is the user's own sentence, which the item carries and which the seam
        has always published; there is still no event for the hand-off itself.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "thread/realtime/itemAdded",
                        {
                            "threadId": THREAD,
                            "item": {
                                "type": "handoff_request",
                                "handoff_id": "item_EJELbGbAr6yo",
                                "input_transcript": "hang up",
                            },
                        },
                    )
                    await asyncio.sleep(0.05)

                assert "item_EJELbGbAr6yo" in caplog.text
                assert "hang up" in caplog.text
                assert sink.events == [CallStarted(call_id=THREAD), UserSpeech(text="hang up")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_sentence_a_hand_off_routed_is_what_the_user_said(self, socket_path: Path) -> None:
        """The carrier that arrives in time, on the call this engine actually makes.

        Measured on this machine: with `delegationAckFiller` off, a request that
        routed produced ten user deltas, a `handoff_request`, `bridgectl live`
        and a closed call in eleven seconds, and **no `transcript/done` at any
        point**. Waiting for `done` loses the user's words outright — and with
        them the Silence Ceiling's only reason to hold the call open.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("那个你", "把", "电话挂", "了吧"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "user", "delta": delta},
                    )
                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "item_1",
                            "input_transcript": "那个你把电话挂了吧",
                        },
                    },
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == [UserSpeech(text="那个你把电话挂了吧")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_done_that_follows_the_same_utterance_does_not_say_it_twice(
        self, socket_path: Path
    ) -> None:
        """One utterance, one event, whichever carrier got there first.

        The two carriers spell the same audio differently — run
        `20260902T093755Z` had two lanes write `结束通话` and `结束通 话` from one
        four-second recording — so they are compared with the spaces taken out.
        A notice heard twice is worse than one heard once, and this seam's whole
        grading discipline exists to stop it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "item_1",
                            "input_transcript": "我想让你结束通话",
                        },
                    },
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "我想让你结束通 话"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == [UserSpeech(text="我想让你结束通话")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_what_the_deltas_spelled_goes_up_when_the_call_ends_on_them(
        self, socket_path: Path
    ) -> None:
        """Nothing claimed the utterance, and the call is over: raise it anyway.

        The third carrier, and the last moment the words are this side's to
        report. Without it a call the Call Agent ends four seconds after routing
        takes the user's sentence with it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("现在有哪些", "需要我的", "事情"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "user", "delta": delta},
                    )
                await asyncio.sleep(0.05)
                assert sink.of(UserSpeech) == []

                await adapter.end_call()

                assert sink.of(UserSpeech) == [UserSpeech(text="现在有哪些需要我的事情")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voices_own_deltas_are_never_the_users_words(self, socket_path: Path) -> None:
        """This system does not read its own speech back to itself."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "that session"},
                )
                await asyncio.sleep(0.05)
                await adapter.end_call()

                assert sink.of(UserSpeech) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_an_item_that_is_not_a_hand_off_is_not_written_down(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The arm reads one item type. Everything else on that method is noise."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "thread/realtime/itemAdded",
                        {"threadId": THREAD, "item": {"type": "input_audio_buffer.speech_started"}},
                    )
                    await asyncio.sleep(0.05)

                assert "handed work to the Call Agent" not in caplog.text
                await adapter.aclose()

        asyncio.run(scenario())

    def test_this_systems_own_voice_is_not_raised_as_the_users(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voices_own_speech_goes_up_as_a_span_not_a_transcript(
        self, socket_path: Path
    ) -> None:
        """One edge each way, from the only two unconditional assistant signals (#184).

        `transcript/delta` and `transcript/done` are what v3 reaches
        (`docs/research/2026-09-01-assistant-speaking-signal.md` §1d), and both
        come from the app-server's unconditional path — unlike the `item/*`
        family, whose existence depends on the operator's history mode.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("that ", "session ", "stopped"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "assistant", "delta": delta},
                    )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_what_the_voice_said_is_written_down_and_raised_to_nobody(
        self, socket_path: Path, caplog
    ) -> None:
        """#197: a run that cannot listen reads the Voice's own words off the log.

        The *event* stays the span edge — nothing in this system acts on its own
        speech, and the seam's event set is closed — so the words go to the log
        and nowhere else.
        """
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.call.realtime.adapter")

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "你上次的回复没送到"},
                )
                await asyncio.sleep(0.05)

                assert "没送到" in caplog.text
                # No span was open, so the `done` closes nothing and raises
                # nothing: the words went to the log alone.
                assert sink.of(VoiceSpeech) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_second_utterance_raises_its_own_start(self, socket_path: Path) -> None:
        """`done` releases the latch, so the next answer is a span of its own."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for _ in range(2):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "assistant", "delta": "more"},
                    )
                    await server.notify_all(
                        "thread/realtime/transcript/done",
                        {"threadId": THREAD, "role": "assistant", "text": "more"},
                    )
                    # Two *utterances*, which means the first one finished being
                    # heard before the second began. A delta arriving while the
                    # previous answer is still playing is one continuous stretch
                    # of speech, and is proved separately below.
                    await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [
                    True,
                    False,
                    True,
                    False,
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_stop_edge_waits_for_the_audio_to_finish_playing(self, socket_path: Path) -> None:
        """`speaking=False` means finished *playing*, not finished generating (#195).

        The lag between the two is the jitter prefetch, this transport's own
        playback buffer and the device, and none of it is visible above the Call
        seam — so publishing the generating edge made every gap-waiter add a
        settle window computed from numbers it could not see (#184's shape).
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport()
                audio.playback_drained_now = False
                adapter, _ = await riding(server, sink, transport=audio)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "that "},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                # Generated, not yet heard: the span is still open, which is what
                # holds the Silence Ceiling on a call somebody is listening to.
                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]

                audio.playback_drained_now = True
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                assert audio.drain_waits == [quick().voice_playout_wait_seconds]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_delta_while_the_answer_is_still_playing_invents_no_gap(
        self, socket_path: Path
    ) -> None:
        """Generating again over its own playout is one stretch of speech, not two (#195).

        The seam publishes a span, and a gap in it is what the Silence Ceiling
        and the mid-call gap-waiter act on. A stop-then-start published here
        would be a gap the user never heard.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport()
                audio.playback_drained_now = False
                adapter, _ = await riding(server, sink, transport=audio)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "one "},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "one"},
                )
                await asyncio.sleep(0.05)
                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "two "},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "two"},
                )
                audio.playback_drained_now = True
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_users_own_speech_is_a_span_as_well_as_a_transcript(
        self, socket_path: Path
    ) -> None:
        """The user's counterpart of `VoiceSpeech`, raised from the deltas (#195).

        Until this event the user's half reached the ceiling only as the finished
        `UserSpeech(text)`, which since #194 often lands at hand-off or teardown
        — so a user who talked for a whole ceiling without the Voice answering
        was judged silent. The transcript still travels; it is what the engine
        writes down, and a span carries no words.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True]

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voice_answering_closes_the_users_span(self, socket_path: Path) -> None:
        """One of the four ends, and the one that needs no `done`: they are not talked over."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is codex doing"},
                )
                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "it is "},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hand_off_carrying_the_utterance_closes_the_users_span(
        self, socket_path: Path
    ) -> None:
        """The carrier that arrives when no `done` ever does (#179, measured)."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "end the call"},
                )
                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "h1",
                            "input_transcript": "end the call",
                        },
                    },
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_users_own_transcript_is_never_the_voice_speaking(self, socket_path: Path) -> None:
        """The role is the wire's word and this adapter is the only translator of it."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is"},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(VoiceSpeech) == []
                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())


class TestWhenTheCallAgentsTurnFails:
    """#270: a hand-off that dies is answered, and the answer says why.

    The Voice-to-Call-Agent hop happens inside codex, so the only trace of it on
    this side is a `turn/completed` on the *live* thread. It used to reach
    nobody: `_turn_heard` routes only for threads the adapter is delegating on,
    and the live thread is never one. Seventeen turns refused with a 400 left no
    log line and no word, the Voice invented a Session to fill the gap, and the
    call died on the Silence Ceiling.
    """

    @staticmethod
    async def failed(server: FakeAppServer, *, message: str, turn_id: str = "turn-1") -> None:
        """One Call Agent turn, dead the way the recorded one was."""
        await server.notify_all(
            "turn/completed",
            {
                "threadId": THREAD,
                "turn": {
                    "id": turn_id,
                    "status": "failed",
                    "error": {"type": "invalid_request_error", "message": message},
                },
            },
        )
        await asyncio.sleep(0.05)

    def test_a_failed_turn_is_logged_and_told_to_the_voice_with_its_reason(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The user learns *why*, not only that. The reason is codex's own words.

        It goes out on `appendSpeech` — the wire call `speak` uses, not `speak`
        itself: this sentence is the adapter's own and not a `SpokenBrief`
        anybody assembled, so there is no brief to build and no receipt to
        classify. Nothing is raised upward: the seam's event set is closed, the
        same way it is for the hand-off going out (ADR 0018).
        """
        refusal = "The 'gpt-6-astra' model requires a newer version of Codex."

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await self.failed(server, message=refusal)

                said = [call["text"] for call in server.calls_to("thread/realtime/appendSpeech")]
                assert len(said) == 1
                assert said[0].startswith(TURN_FAILED)
                assert refusal in said[0]

                warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
                assert len(warnings) == 1
                assert refusal in warnings[0].getMessage()
                assert "turn-1" in warnings[0].getMessage()

                assert sink.events == [CallStarted(call_id=THREAD)]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_turn_that_did_not_fail_is_written_down_and_nothing_is_said(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The answer to a working hand-off reaches the Voice inside codex.

        Saying anything here would be this side narrating a turn it never saw
        the content of. What the log gets is the proof that the acting half is
        answering at all, which is the thing no other record on this machine
        carries.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "turn/completed",
                        {"threadId": THREAD, "turn": {"id": "turn-9", "status": "completed"}},
                    )
                    await asyncio.sleep(0.05)

                assert server.calls_to("thread/realtime/appendSpeech") == []
                written = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
                assert [line for line in written if "turn-9" in line and "completed" in line]
                assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_every_failure_is_told_because_there_is_no_retry_to_wait_for(
        self, socket_path: Path
    ) -> None:
        """No dedupe. This engine cannot re-hand-off — only the user can, by asking again.

        So each failure is already final, and Simon's rule 6.1 says a final
        failure reaches the user with its reason. Coalescing the second one
        would leave a user who asked twice hearing about it once.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                await self.failed(server, message="the same 400 again", turn_id="turn-1")
                await self.failed(server, message="the same 400 again", turn_id="turn-2")

                said = [call["text"] for call in server.calls_to("thread/realtime/appendSpeech")]
                assert len(said) == 2
                assert said[0] == said[1]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_failure_with_no_message_in_it_still_reaches_the_user(
        self, socket_path: Path
    ) -> None:
        """A reason nobody gave is said as one, rather than swallowing the failure.

        The shape of an `error` is the far side's to decide, so this side reads
        it defensively and still speaks: silence is the defect being fixed.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "turn/completed",
                    {"threadId": THREAD, "turn": {"status": "failed", "error": "went wrong"}},
                )
                await asyncio.sleep(0.05)

                said = [call["text"] for call in server.calls_to("thread/realtime/appendSpeech")]
                assert said == [f"{TURN_FAILED}: no reason given"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_delegated_turns_completion_is_untouched_by_any_of_this(
        self, socket_path: Path
    ) -> None:
        """The other `turn/completed` path, still classified where it always was.

        A Delegated Turn's failure is already answered — it becomes a
        `DelegatedTurnError` the caller reads — so routing it through the live
        thread's branch as well would say it twice, once into a call the user
        may not even be on.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())
                delegated_script(server, thread_id="delegated-1", says="the answer")

                reply = await adapter.delegate(
                    "read the roster",
                    model=DELEGATED_MODEL,
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.text == "the answer"
                assert server.calls_to("thread/realtime/appendSpeech") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_failure_after_the_call_is_over_is_written_down_and_not_spoken(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing left to tell, and nothing raised — but still written down (#270).

        A call hung up while its last hand-off was still running produces
        exactly this, and the line is the only trace that turn leaves anywhere
        on this machine. It is not routed to the failure branch, because the
        thread it names is nobody's now: there is no call to speak into and no
        Delegated Turn waiting on it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())
                await adapter.end_call()
                spoken_before = len(server.calls_to("thread/realtime/appendSpeech"))

                with caplog.at_level(logging.INFO):
                    await self.failed(server, message="too late to matter")

                assert len(server.calls_to("thread/realtime/appendSpeech")) == spoken_before
                written = [r.getMessage() for r in caplog.records]
                assert [line for line in written if THREAD in line and "after its call was" in line]

        asyncio.run(scenario())

    def test_a_retired_delegated_turns_late_completion_is_left_exactly_as_it_was(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other unmatched `turn/completed`, and this ticket does not touch it.

        A Delegated Turn that timed out is dropped from `_delegating` by
        `_retire`, so a completion arriving afterwards matches nothing — the
        same shape as the ended call's above, and a different thread. The
        after-the-call line is narrowed to the call's own thread precisely so
        this one keeps its old silence; whether it deserves a line of its own is
        a question for a ticket that asks it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "turn/completed",
                        {
                            "threadId": "delegated-and-long-gone",
                            "turn": {"id": "turn-8", "status": "failed"},
                        },
                    )
                    await asyncio.sleep(0.05)

                assert server.calls_to("thread/realtime/appendSpeech") == []
                assert not [line for line in caplog.messages if "turn-8" in line]
                assert not [line for line in caplog.messages if "delegated-and-long-gone" in line]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_failed_status_carrying_no_error_payload_is_still_a_failure(
        self, socket_path: Path
    ) -> None:
        """Either field is enough, because neither is this side's to guarantee.

        Reading only `error` would let a turn codex called `failed` pass as an
        answer and leave the user in the silence this ticket exists to end.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "turn/completed",
                    {"threadId": THREAD, "turn": {"id": "turn-3", "status": "failed"}},
                )
                await asyncio.sleep(0.05)

                said = [call["text"] for call in server.calls_to("thread/realtime/appendSpeech")]
                assert said == [f"{TURN_FAILED}: no reason given"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_completion_this_side_cannot_read_is_written_down_and_not_spoken(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A shape with no status and no error is neither, and is not guessed at.

        Speaking about a failure that may not be one would put words in the
        user's ear that no record supports — the exact habit ADR 0018 forbids
        the Voice. The line in the log is what a reader follows instead.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-4"}}
                    )
                    await asyncio.sleep(0.05)

                assert server.calls_to("thread/realtime/appendSpeech") == []
                assert [line for line in caplog.messages if "turn-4" in line]
                assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_refused_appendSpeech_does_not_take_the_call_down(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failure to say the failure is written down and goes no further (#270).

        The user is already being let down once. Letting this raise on the
        notification path would turn that into a dropped call, which is the
        larger of the two harms and the one they did not ask for.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                def refuse(_params: dict[str, Any]) -> dict[str, Any]:
                    raise FakeRemoteError("this session takes no speech")

                server.answers("thread/realtime/appendSpeech", refuse)

                with caplog.at_level(logging.INFO):
                    await self.failed(server, message="a 400 nobody will hear about")

                assert adapter.snapshot().state is CallState.UP
                assert sink.of(CallDropped) == []
                assert "could not be told" in caplog.text
                await adapter.aclose()

        asyncio.run(scenario())


class TestTheDelegatedTurn:
    def test_the_callers_model_and_instructions_reach_the_thread(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, model="claude-sonnet-5", says="the diff is small")
                adapter, _ = await riding(server, Sink(), delegated_turn_effort="high")

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="claude-sonnet-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                started = server.calls_to("thread/start")[0]
                assert started["model"] == "claude-sonnet-5"
                assert started["config"]["model_reasoning_effort"] == "high"
                assert started["developerInstructions"] == DELEGATED_RULES
                assert started["approvalPolicy"] == APPROVAL_POLICY
                assert started["sandbox"] == SANDBOX
                assert reply.text == "the diff is small"
                assert reply.model == "claude-sonnet-5"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_model_reported_is_the_one_the_server_says_it_ran(self, socket_path: Path) -> None:
        """Echoing back the caller's own argument would tell it nothing."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, model="gpt-5-codex")
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="an-alias",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.model == "gpt-5-codex"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_thread_does_not_outlive_the_turn(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")
                adapter, _ = await riding(server, Sink())

                await adapter.delegate(
                    "summarise the diff",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert server.calls_to("thread/unsubscribe") == [{"threadId": "delegated-1"}]
                # It finished by itself, so there is nothing to interrupt —
                # whichever order codex answered the request and announced the
                # completion in.
                assert server.calls_to("turn/interrupt") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_turn_that_completed_before_its_own_response_is_not_interrupted(
        self, socket_path: Path
    ) -> None:
        """`turn/completed` can land before the `turn/start` reply it belongs to.

        The notification arrives on the reader task while this side is still
        awaiting the response, so anything that recorded "finished" by clearing
        the turn id would have it written straight back — and a turn that had
        already completed would be interrupted on the way out.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")

                async def finish_before_answering(_params: dict) -> dict:
                    await server.notify_all(
                        "item/completed",
                        {
                            "threadId": "delegated-1",
                            "turnId": "turn-1",
                            "item": {"type": "agentMessage", "id": "i-1", "text": "done"},
                        },
                    )
                    await server.notify_all(
                        "turn/completed",
                        {
                            "threadId": "delegated-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    )
                    await asyncio.sleep(0.05)
                    return {"turn": {"id": "turn-1"}}

                server.answers("turn/start", finish_before_answering)
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.text == "done"
                assert server.calls_to("turn/interrupt") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_opening_a_conversation_starts_a_thread_and_runs_no_turn(
        self, socket_path: Path
    ) -> None:
        """ADR 0021 §7: the opening line is Core's own words, so nothing is asked of the model."""

        async def scenario() -> str:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1")
                adapter, _ = await riding(server, Sink())

                thread_id = await adapter.open_conversation(
                    model="gpt-5", instructions=DELEGATED_RULES
                )

                assert server.calls_to("turn/start") == []
                await adapter.aclose()
                return thread_id

        assert asyncio.run(scenario()) == "assistant-1"

    def test_a_resumed_turn_runs_on_the_thread_it_was_given_and_starts_no_other(
        self, socket_path: Path
    ) -> None:
        """A reply to any message of a conversation continues that same thread (ADR 0021 §7)."""

        async def scenario() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1", says="two")
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "and the second one?",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                    resume="assistant-1",
                )

                answer = reply.text
                resumed = server.calls_to("thread/resume")
                started = server.calls_to("thread/start")
                await adapter.aclose()
                return answer, resumed, started

        answer, resumed, started = asyncio.run(scenario())

        assert answer == "two"
        assert resumed == [{"threadId": "assistant-1"}]
        assert started == []

    def test_a_resumed_turn_leaves_its_thread_alive_for_the_next_reply(
        self, socket_path: Path
    ) -> None:
        """ADR 0021 §7 amends the one-turn rule: the turn unsubscribes, the thread stays."""

        async def scenario() -> list[dict[str, Any]]:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1")
                adapter, _ = await riding(server, Sink())

                await adapter.delegate(
                    "again",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                    resume="assistant-1",
                )

                interrupts = server.calls_to("turn/interrupt")
                assert server.calls_to("thread/unsubscribe") == [{"threadId": "assistant-1"}]
                await adapter.aclose()
                return interrupts

        assert asyncio.run(scenario()) == []

    def test_the_first_turn_runs_on_a_thread_that_has_no_rollout_to_resume(
        self, socket_path: Path
    ) -> None:
        """A conversation opens without a turn, so its thread has done nothing yet (#265).

        Codex refuses `thread/resume` on a thread with no rollout — the fact the
        Codex Session adapter already reads as a *not yet* — and the first reply
        of every conversation meets exactly that. It must run the turn, not
        report the conversation ended.
        """

        async def scenario() -> str:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1", says="the first answer")

                def no_rollout(_params: dict) -> dict:
                    raise FakeRemoteError("no rollout found")

                server.answers("thread/resume", no_rollout)
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "what changed?",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                    resume="assistant-1",
                )

                answer = reply.text
                assert server.calls_to("turn/start")
                await adapter.aclose()
                return answer

        assert asyncio.run(scenario()) == "the first answer"

    def test_a_resume_that_names_a_different_thread_is_refused(self, socket_path: Path) -> None:
        """The Codex Session adapter makes the same check for the same reason."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1")
                server.answers("thread/resume", {"thread": {"id": "somebody-elses"}})
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError, match="different thread"):
                    await adapter.delegate(
                        "what changed?",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                        resume="assistant-1",
                    )

                assert server.calls_to("turn/start") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_thread_that_cannot_be_resumed_is_its_own_failure(self, socket_path: Path) -> None:
        """Distinguishable from a model answer, so Core can send the start-again hint (#265)."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1")

                def gone(_params: dict) -> dict:
                    raise FakeRemoteError("no thread with that id")

                server.answers("thread/resume", gone)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(ThreadGoneError):
                    await adapter.delegate(
                        "still there?",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                        resume="assistant-1",
                    )

                assert server.calls_to("turn/start") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_busy_thread_comes_back_as_codexs_own_refusal(self, socket_path: Path) -> None:
        """A refused `turn/start` is the answer in codex's own words (§7).

        Core never issues one on a thread it is already running (#268), so this
        is the seam's contract rather than a case the hub can reach.
        """

        async def scenario() -> str:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="assistant-1")

                def busy(_params: dict) -> dict:
                    raise FakeRemoteError("a turn is already running on that thread")

                server.answers("turn/start", busy)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "and again",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                        resume="assistant-1",
                    )

                await adapter.aclose()
                return str(refusal.value)

        said = asyncio.run(scenario())

        assert "a turn is already running on that thread" in said

    def test_a_turn_cancelled_by_a_shutdown_is_interrupted_and_unsubscribed(
        self, socket_path: Path
    ) -> None:
        """#268: a turn runs beside the dispatch loop, so a shutdown cancels it here.

        The same teardown a timeout gets, reached the other way. It has to run on
        this path too: a bridge-owned thread runs approval-free in a full
        sandbox, so a turn an engine walked away from would go on acting on the
        user's machine with nothing watching it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")
                server.answers("turn/start", {"turn": {"id": "turn-1"}})  # never completes
                adapter, _ = await riding(server, Sink())

                turn = asyncio.create_task(
                    adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )
                )
                while not server.calls_to("turn/start"):
                    await asyncio.sleep(0.05)

                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await turn

                assert server.calls_to("turn/interrupt") == [
                    {"threadId": "delegated-1", "turnId": "turn-1"}
                ]
                assert server.calls_to("thread/unsubscribe") == [{"threadId": "delegated-1"}]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_turn_that_never_answers_is_a_classified_failure_and_leaks_nothing(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")
                server.answers("turn/start", {"turn": {"id": "turn-1"}})  # never completes
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                assert "did not finish" in str(refusal.value)
                # Unsubscribing only stops this engine hearing about the turn. A
                # bridge-owned thread runs approval-free in a full sandbox, so a
                # turn left running would keep acting on the user's machine and
                # spending their money with nothing watching it.
                assert server.calls_to("turn/interrupt") == [
                    {"threadId": "delegated-1", "turnId": "turn-1"}
                ]
                assert server.calls_to("thread/unsubscribe") == [{"threadId": "delegated-1"}]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_failed_turn_says_what_codex_said(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")

                def fail_the_turn(_params: dict) -> dict:
                    async def finish() -> None:
                        await server.notify_all(
                            "turn/completed",
                            {
                                "threadId": "delegated-1",
                                "turn": {
                                    "id": "turn-1",
                                    "status": "failed",
                                    "error": {"message": "the model is over its limit"},
                                },
                            },
                        )

                    asyncio.ensure_future(finish())
                    return {"turn": {"id": "turn-1"}}

                server.answers("turn/start", fail_the_turn)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                assert "over its limit" in str(refusal.value)
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_start_codex_refuses_is_answered_in_the_adapters_own_words(
        self, socket_path: Path
    ) -> None:
        """#269: the one-shot start is guarded, so a `>` is never answered with silence.

        The words are this adapter's own sentence and carry nothing of the
        refusal: what the user can do about a turn that never got a thread is
        the same whatever the wire said, and the log is where the reason goes.
        """

        async def scenario() -> str:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server)

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no model with that name is configured")

                server.answers("thread/start", refuse)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                assert server.calls_to("turn/start") == []
                await adapter.aclose()
                return str(refusal.value)

        said = asyncio.run(scenario())

        assert said == TURN_NOT_STARTED
        assert "no model with that name" not in said

    def test_a_start_that_never_answers_is_the_same_sentence(self, socket_path: Path) -> None:
        """The other family: past the wire with no answer, rather than refused on it."""

        async def scenario() -> str:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server)
                slow = asyncio.Event()

                async def dawdle(_params: dict) -> dict:
                    await slow.wait()
                    return {"thread": {"id": "delegated-1"}}

                server.answers("thread/start", dawdle)
                settings = replace(quick(), request_timeout_seconds=0.2)
                adapter, _ = await riding(server, Sink(), settings=settings)

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                slow.set()
                assert server.calls_to("turn/start") == []
                await adapter.aclose()
                return str(refusal.value)

        assert asyncio.run(scenario()) == TURN_NOT_STARTED

    def test_a_conversation_that_cannot_be_opened_still_raises_the_wire_failure(
        self, socket_path: Path
    ) -> None:
        """`open_conversation` is the menu path, and Core answers it with its own hint.

        Unchanged by #269: the guard is on the turn's own start, and turning
        this into a `DelegatedTurnError` would tell the hub a turn had failed
        where no turn was ever run.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server)

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no model with that name is configured")

                server.answers("thread/start", refuse)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(RemoteError):
                    await adapter.open_conversation(model="gpt-5", instructions=DELEGATED_RULES)

                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_delegated_turn_needs_no_call_to_be_up(self, socket_path: Path) -> None:
        """It arrives from the Companion Channel too, where there is no call at all."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, says="nothing is running")
                adapter, _ = await riding(server, Sink())

                assert (await adapter.call_state()).state is CallState.DOWN
                reply = await adapter.delegate(
                    "what is running",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.text == "nothing is running"
                await adapter.aclose()

        asyncio.run(scenario())


class TestTheTransportItIsLent:
    def test_it_refuses_to_open_without_an_app_server(self) -> None:
        adapter = RealtimeCallAdapter(
            delegated_turn_model=DELEGATED_MODEL, transport_factory=FakeTransport
        )

        with pytest.raises(AppServerError) as refusal:
            asyncio.run(adapter.connect())

        assert "never handed" in str(refusal.value)

    def test_it_takes_an_app_server_once(self) -> None:
        adapter = RealtimeCallAdapter(
            delegated_turn_model=DELEGATED_MODEL, transport_factory=FakeTransport
        )
        adapter.use_app_server(SharedAppServer(connection=None))  # type: ignore[arg-type]

        with pytest.raises(AppServerError):
            adapter.use_app_server(SharedAppServer(connection=None))  # type: ignore[arg-type]

    def test_verify_reports_what_is_loaded_and_whether_the_far_side_answers(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                server.answers("thread/loaded/list", {"data": []})
                adapter, _ = await riding(server, Sink())

                result = await adapter.verify()

                assert result.outcome is VerifyOutcome.PASS
                assert result.loaded.endswith(":RealtimeCallAdapter")
                await adapter.aclose()

        asyncio.run(scenario())

    def test_verify_fails_when_there_is_no_app_server_to_ask(self) -> None:
        adapter = RealtimeCallAdapter(
            delegated_turn_model=DELEGATED_MODEL, transport_factory=FakeTransport
        )

        result = asyncio.run(adapter.verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert result.detail


class TestWhatThisSpokeMayBeTold:
    def test_voice_defaults_to_cove_and_accepts_a_configured_name(self) -> None:
        assert RealtimeCallSettings.of(None).voice == "cove"
        assert RealtimeCallSettings.of({"voice": " ember "}).voice == "ember"
        with pytest.raises(SettingsError, match="voice name"):
            RealtimeCallSettings.of({"voice": " "})

    def test_an_unknown_setting_refuses_to_start(self) -> None:
        with pytest.raises(SettingsError) as refusal:
            RealtimeCallSettings.of({"conect_timeout_seconds": 1.0})

        assert "conect_timeout_seconds" in str(refusal.value)

    def test_the_two_speaking_span_timings_are_settings(self) -> None:
        """Neither is a measurement, so neither is pinned (#195).

        `user_quiet_seconds` is the one end of the user's span this adapter
        invents, and whether user deltas arrive during or after the speech is
        what #212 settles — a value nobody can change without a release is a
        value that measurement cannot correct. `voice_playout_wait_seconds`
        bounds a wait on somebody else's audio path, which is exactly the kind
        of number that differs by machine.
        """
        dialled = RealtimeCallSettings.of(
            {"user_quiet_seconds": 2.5, "voice_playout_wait_seconds": 45.0}
        )

        assert dialled.user_quiet_seconds == 2.5
        assert dialled.voice_playout_wait_seconds == 45.0
        for name in ("user_quiet_seconds", "voice_playout_wait_seconds"):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({name: 0})

    def test_neither_the_approval_policy_nor_the_sandbox_is_a_setting(self) -> None:
        """Both are pinned. A key for either would invite half the trade to be broken."""
        for name in ("approval_policy", "sandbox", "approvalPolicy"):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({name: "on-request"})

    def test_the_realtime_model_is_a_setting_because_the_peer_can_revoke_it(self) -> None:
        """Unlike the approval policy and the sandbox, this value is not ours to pin.

        Those two are mechanism identity: this repository verifies them and they
        move only when our code moves. The realtime model's validity is granted
        and withdrawn by the backend — it moved once inside five days with no
        client change (#35) — so it is an environment fact with a default, and
        the operator gets a one-line escape hatch instead of a wait for our next
        release.
        """
        assert RealtimeCallSettings().realtime_model == DEFAULT_REALTIME_MODEL
        assert RealtimeCallSettings.of({"realtime_model": "gpt-live-2-later"}).realtime_model == (
            "gpt-live-2-later"
        )

    def test_an_empty_realtime_model_refuses_to_start(self) -> None:
        for value in ("", "   ", 7):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({"realtime_model": value})

    def test_the_workspace_defaults_to_a_directory_that_always_exists(self) -> None:
        assert RealtimeCallSettings().cwd == Path.home()

    def test_a_stated_workspace_is_used(self) -> None:
        assert RealtimeCallSettings.of({"workspace": "/tmp/somewhere"}).cwd == Path(
            "/tmp/somewhere"
        )

    def test_the_factory_builds_the_adapter_from_the_table(self) -> None:
        adapter = realtime_call(
            delegated_turn_model=DELEGATED_MODEL,
            settings={"connect_timeout_seconds": 5.0},
            transport_factory=FakeTransport,
        )

        assert isinstance(adapter, RealtimeCallAdapter)


class TestTheCuesItPlays:
    """`play_cue`, and why the player is the adapter's rather than a call's (#186).

    Nothing here makes a sound. The stream lives in the audio module behind
    `CueOutput` for exactly that reason: what is worth grading is which moment
    got which sound, that it went out on the configured device, that it can go
    out with no call left, and that a device which will not open takes nothing
    down with it.
    """

    @staticmethod
    def played(player: FakeCueOutput, count: int = 1, *, within: float = 5.0) -> None:
        """Wait for the cue worker to have reached `count` cues.

        `play_cue` deliberately does not wait, so every test that reads what went
        out has to. Counted rather than "anything yet", because the worker plays
        in order and a test that looked once would read the cue before the one
        it asked about.
        """
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if len(player.attempts) >= count:
                return
            time.sleep(0.005)
        raise AssertionError(f"only {len(player.attempts)} of {count} cues were played")

    def test_each_moment_is_played_as_its_own_sound(self, socket_path: Path) -> None:
        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for number, cue in enumerate(Cue, start=1):
                    await adapter.play_cue(cue)
                    self.played(player, number)
                    assert player.buffers[-1] == cues.render(cue)
                return player

        player = asyncio.run(scenario())
        assert player.buffers == [cues.render(cue) for cue in Cue]

    def test_a_cue_goes_to_the_output_device_the_call_was_configured_with(self) -> None:
        """One `output_device` setting, and a cue honours the one the call does.

        Built the way the composition root builds it — no player handed in — so
        this also asserts that an adapter which was told nothing about cues
        still has one, on the machine's own default output.
        """
        stated = RealtimeCallAdapter(
            delegated_turn_model=DELEGATED_MODEL,
            settings=quick(output_device=4),
            transport_factory=FakeTransport,
        )
        streams = _Streams(blocks_on=None)
        with _sounddevice(streams):
            stated.play_now(Cue.CONNECTED)
        assert streams.opened[0]["device"] == 4

        default = RealtimeCallAdapter(
            delegated_turn_model=DELEGATED_MODEL, settings=quick(), transport_factory=FakeTransport
        )
        with _sounddevice(streams):
            default.play_now(Cue.CONNECTED)
        assert streams.opened[-1]["device"] is None

    def test_the_ended_cue_plays_after_the_calls_own_audio_has_closed(
        self, socket_path: Path
    ) -> None:
        """The whole reason the player is per adapter and not per call.

        A cue mixed into the call's own playback buffer could not mark the end
        of a call: by the time there is an end to mark, that stream is shut.
        """

        async def scenario() -> tuple[FakeTransport, FakeCueOutput]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, audio = await riding(server, Sink(), cue_player=player)
                await adapter.ensure_call(dial())
                await adapter.end_call()
                assert audio.closed
                await adapter.play_cue(Cue.ENDED)
                self.played(player)
                return audio, player

        audio, player = asyncio.run(scenario())
        assert audio.closed
        assert player.buffers == [cues.render(Cue.ENDED)]

    def test_the_adapter_writes_down_the_device_and_the_span_it_played(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The engine logs no line for `CallStarted` or `CallEnded`, so this is
        the only witness the acceptance harness has that a cue went out."""

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(device=9)
                adapter, _ = await riding(server, Sink(), cue_player=player)
                with caplog.at_level(logging.INFO):
                    await adapter.play_cue(Cue.CONNECTED)
                    self.played(player)
                return player

        asyncio.run(scenario())
        written = [record.getMessage() for record in caplog.records]
        line = next(said for said in written if cues.cue_phrase(Cue.CONNECTED) in said)
        assert "output device 9" in line
        assert "14400 frames" in line
        assert "0.300s" in line

    def test_a_device_that_will_not_open_takes_nothing_down_with_it(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No output device, no audio library, somebody unplugged the speakers.

        A cue is feedback about something that already happened; there is no
        recovery to attempt and nothing above the seam that could attempt one.
        """

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(fails="no such output device")
                adapter, _ = await riding(server, Sink(), cue_player=player)
                with caplog.at_level(logging.INFO):
                    await adapter.play_cue(Cue.ENDED)
                    self.played(player)
                assert (await adapter.call_state()).state is CallState.DOWN
                return player

        asyncio.run(scenario())
        written = [record.getMessage() for record in caplog.records]
        assert any("no such output device" in said for said in written)
        assert not any(cues.cue_phrase(Cue.ENDED) in said for said in written)

    def test_a_cue_opens_the_stream_on_the_speakers_own_parameters(self) -> None:
        """One shape for the whole audio path, so a cue needs no second opinion.

        The PCM is synthesised at these numbers (`cues.py`), so a stream opened
        at any other would play the cue at the wrong pitch and speed.
        """
        player = webrtc.CuePlayer(device=6)
        streams = _Streams(blocks_on=None)

        with _sounddevice(streams):
            player.play(cues.render(Cue.CONNECTED))

        assert streams.opened == [
            {
                "samplerate": webrtc.SAMPLE_RATE,
                "channels": webrtc.CHANNELS,
                "dtype": webrtc.SAMPLE_FORMAT,
                "blocksize": webrtc.FRAME_SAMPLES,
                "device": 6,
            }
        ]

    def test_asking_for_a_cue_does_not_wait_for_it(self, socket_path: Path) -> None:
        """A cue costs 320-620 ms of wall time on the real path (#174), and the
        arm that asks for one is Bridge Core's dispatch. It is not held."""
        writing = threading.Event()
        release = threading.Event()

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                player.while_playing = lambda: (writing.set(), release.wait(2.0))
                adapter, _ = await riding(server, Sink(), cue_player=player)

                await adapter.play_cue(Cue.CONNECTED)
                # Returned while the write is still in the device. If `play_cue`
                # had waited, this line would not run until `release` was set.
                assert writing.wait(2.0)
                assert not release.is_set()
                release.set()

        asyncio.run(scenario())

    def test_a_call_that_drops_the_moment_it_came_up_is_still_heard_in_order(
        self, socket_path: Path
    ) -> None:
        """The case a thread a cue could not keep (#186).

        The two cues mark the two ends of one call, so their order is the claim.
        A call that goes away within one cue's wall time of coming up asks for
        the second before the first has finished playing — 320-620 ms on the
        real path (#174) — and two threads racing for the device would have the
        user hear the call end before they heard it start. The player is fed by
        one worker, so it cannot.

        The stand-in device holds each write open long enough that an unordered
        implementation would have to interleave to pass.
        """

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                player.while_playing = lambda: time.sleep(0.05)
                adapter, _ = await riding(server, Sink(), cue_player=player)

                # No gap at all between them: both are asked for before the
                # first has reached the device.
                await adapter.play_cue(Cue.CONNECTED)
                await adapter.play_cue(Cue.ENDED)
                deadline = time.monotonic() + 5.0
                while len(player.buffers) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert player.buffers == [cues.render(cue) for cue in (Cue.CONNECTED, Cue.ENDED)]

    def test_every_cue_asked_for_is_played_once_and_in_order(self, socket_path: Path) -> None:
        """A burst longer than any real call, to prove the queue drains in order."""
        asked = [Cue.CONNECTED, Cue.EVENT, Cue.EVENT, Cue.ENDED, Cue.CONNECTED, Cue.ENDED]

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for cue in asked:
                    await adapter.play_cue(cue)
                deadline = time.monotonic() + 5.0
                while len(player.buffers) < len(asked) and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert player.buffers == [cues.render(cue) for cue in asked]

    def test_one_worker_plays_every_cue_rather_than_a_thread_each(self, socket_path: Path) -> None:
        """The mechanism the order rests on, asserted rather than assumed."""

        async def scenario() -> list[threading.Thread]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                on: list[threading.Thread] = []
                player.while_playing = lambda: on.append(threading.current_thread())
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for cue in Cue:
                    await adapter.play_cue(cue)
                deadline = time.monotonic() + 5.0
                while len(player.buffers) < len(list(Cue)) and time.monotonic() < deadline:
                    time.sleep(0.005)
                return on

        on = asyncio.run(scenario())
        assert len(on) == len(list(Cue))
        assert len({thread.name for thread in on}) == 1

    def test_the_cue_worker_never_holds_a_closing_engine_open(self, socket_path: Path) -> None:
        """A tone is not a reason for an engine to wait, so the worker is a daemon.

        It also never ends by itself — it has to outlive every call, because the
        cue that matters most is the one that marks a call that has gone — so a
        non-daemon worker would be a process that could not exit at all.
        """

        async def scenario() -> list[threading.Thread]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                started: list[threading.Thread] = []
                player.while_playing = lambda: started.append(threading.current_thread())
                adapter, _ = await riding(server, Sink(), cue_player=player)
                await adapter.play_cue(Cue.CONNECTED)
                self.played(player)
                return started

        started = asyncio.run(scenario())
        assert started and all(thread.daemon for thread in started)

    def test_a_cue_that_could_not_be_played_does_not_stop_the_ones_after_it(
        self, socket_path: Path
    ) -> None:
        """One worker for every cue means one failure could have silenced the rest."""

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(fails="no such output device")
                adapter, _ = await riding(server, Sink(), cue_player=player)
                await adapter.play_cue(Cue.CONNECTED)
                deadline = time.monotonic() + 5.0
                while len(player.attempts) < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                player.fails = ""
                await adapter.play_cue(Cue.ENDED)
                while len(player.buffers) < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert player.buffers == [cues.render(Cue.ENDED)]


class TestWhatALostConnectionReleases:
    """A connection that went away by itself still gives its devices back.

    Found by #186's review, and it is #186's problem: `ENDED` is played on a
    drop, out of the adapter's own player, and it must not go out into a
    microphone that a dead call left open. The bug was older than the cue —
    `_note` set `_closing` to keep itself from reporting one loss twice, and
    `_closing` is also what makes `aclose` idempotent, so the `aclose` the
    adapter runs after a drop returned at its first line and stopped nothing.

    **Built without `aiortc`, deliberately.** `webrtc.py` is the one module CI
    cannot exercise — the voice extra is not installed there — and that is
    exactly why a defect in it survived. `_note` and `aclose` touch four
    attributes between them and none of them is a peer connection, so the object
    is assembled here rather than constructed, and the rule gets a test that runs
    on every push instead of on one laptop.
    """

    class Stopped:
        def __init__(self) -> None:
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

    class Connection:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    def transport(self) -> Any:
        made = object.__new__(webrtc._WebRtcTransport)
        made._pc = self.Connection()
        made._microphone = self.Stopped()
        made._speaker = self.Stopped()
        made._connected = asyncio.get_event_loop().create_future()
        made._connected.set_result(None)
        made._closing = False
        made._reported = False
        made._on_lost = None
        return made

    def test_a_drop_is_reported_and_the_devices_are_still_released(self) -> None:
        async def scenario() -> tuple[Any, list[str]]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)

            made._note("failed")
            # What the adapter does next, on the task it spawns for it.
            await made.aclose()
            return made, losses

        made, losses = asyncio.run(scenario())
        assert len(losses) == 1
        assert made._microphone.stops == 1
        assert made._speaker.stops == 1
        assert made._pc.closed == 1

    def test_one_loss_is_reported_once_however_many_readings_say_so(self) -> None:
        async def scenario() -> list[str]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)
            made._note("failed")
            made._note("closed")
            return losses

        assert len(asyncio.run(scenario())) == 1

    def test_a_close_this_side_asked_for_is_never_reported_as_a_loss(self) -> None:
        async def scenario() -> list[str]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)
            await made.aclose()
            made._note("closed")
            return losses

        assert asyncio.run(scenario()) == []

    def test_closing_twice_releases_the_devices_once(self) -> None:
        async def scenario() -> Any:
            made = self.transport()
            await made.aclose()
            await made.aclose()
            return made

        made = asyncio.run(scenario())
        assert made._microphone.stops == 1
        assert made._pc.closed == 1


class _Clock:
    """`time.monotonic`, moved by hand, so a 60ms bound costs no wall time.

    Swapped in for `webrtc.time` rather than for the `time` module itself: what
    is under test is one module's arithmetic on one clock, and patching the
    process's clock to prove it would be a far larger claim than the one being
    made.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TrackEnded(Exception):
    """What the far side going away looks like from inside `recv`."""


class _Resampled:
    """One resampled plane, padded the way `av`'s really is."""

    def __init__(self, samples: int) -> None:
        self.samples = samples
        self.planes = [b"\xff" * (samples * webrtc.SAMPLE_BYTES + 64)]


class _InboundTrack:
    """The Voice's audio, arriving on a schedule the test writes.

    Each entry is the gap *before* that frame arrives, so a list is the inbound
    pacing the remote peer — or a starved event loop — would have produced.
    Running out of them is the call going away, which is what `_playing` returns
    on, so a scenario ends by itself with every frame accounted for.
    """

    def __init__(self, gaps: list[float], clock: _Clock) -> None:
        self._gaps = list(gaps)
        self._clock = clock

    async def recv(self) -> Any:
        if not self._gaps:
            raise _TrackEnded
        self._clock.advance(self._gaps.pop(0))
        return object()


class _PaddingTrack:
    """A peer that never stops sending — the stall #301 is about.

    `_InboundTrack` yields a fixed list and then ends, which is a peer that
    stops. This one keeps yielding for as long as anything receives, so the
    playback buffer never reaches zero and the inbound stream never goes
    quiet: the two facts the pre-#301 check needed, neither of which a padding
    peer ever supplies.

    Paced at one frame per `FRAME_SECONDS` of *real* time rather than spun as
    fast as the loop will go, because a wait runs beside this one and a peer
    that never yields would starve it. The test's own clock is advanced by the
    same amount, so the inbound stream reads as continuous there too.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.frames = 0
        self._wanted = 0
        self._enough: asyncio.Event | None = None

    async def recv(self) -> Any:
        await asyncio.sleep(webrtc.FRAME_SECONDS)
        self._clock.advance(webrtc.FRAME_SECONDS)
        self.frames += 1
        if self._enough is not None and self.frames >= self._wanted:
            self._enough.set()
        return object()

    async def sent(self, frames: int) -> None:
        """Return once this many frames are buffered, so a span starts holding a known amount.

        The speaker buffers each frame before its next `recv` yields, so a
        waiter released here always sees the frame that released it already
        counted — which is what makes the span's audio an exact number of
        frames and not a race.
        """
        self._wanted = frames
        self._enough = asyncio.Event()
        if self.frames < frames:
            await self._enough.wait()


@contextmanager
def _av(*, samples: int = 0) -> Iterator[None]:
    """`av`, for the length of a test. CI does not install the real one."""
    resampler = types.SimpleNamespace(
        resample=lambda _frame: [_Resampled(samples)] if samples else []
    )
    stood_in = types.SimpleNamespace(AudioResampler=lambda **_parameters: resampler)
    was = sys.modules.get("av")
    sys.modules["av"] = stood_in  # type: ignore[assignment]
    try:
        yield
    finally:
        if was is None:
            del sys.modules["av"]
        else:
            sys.modules["av"] = was


class _PlayoutSeam:
    """The speaker, driven directly, on a clock the test moves by hand.

    Shared by the two classes below because they drive the same seam: one asks
    what a span *reports*, the other what *closes* it, and a second copy of the
    setup would let those two drift into two seams.
    """

    def speaker(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Clock]:
        clock = _Clock()
        monkeypatch.setattr(webrtc, "time", clock)
        return webrtc._Speaker(silent=True, device=None), clock

    def buffering(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Clock]:
        """A speaker with a real playback buffer, on a device that never drains it."""
        clock = _Clock()
        monkeypatch.setattr(webrtc, "time", clock)
        return webrtc._Speaker(silent=False, device=None), clock

    def heard(self, speaker: Any, gaps: list[float], clock: _Clock) -> None:
        """Play one stretch of inbound audio into the speaker, to its end."""

        async def scenario() -> None:
            speaker.attach(_InboundTrack(gaps, clock))
            await speaker._task

        with _sounddevice(_Streams(blocks_on=None)), _av(samples=webrtc.FRAME_SAMPLES):
            asyncio.run(scenario())

    def transport(self, speaker: Any) -> Any:
        made = object.__new__(webrtc._WebRtcTransport)
        made._speaker = speaker
        made._events_seen = set()
        return made

    def line(self, caplog: Any) -> str:
        """The one stop-edge line the wait wrote."""
        return next(
            said for said in (record.getMessage() for record in caplog.records) if "drained" in said
        )


class TestWhatAPlayoutSaysAboutItself(_PlayoutSeam):
    """#230: a stalled stop edge has to be explainable from the engine log alone.

    `drained` was two facts about the far side and the device, and the timeout
    line reported neither. A remote peer that keeps RTP flowing through silence
    and an event loop starved into delivering frames in bursts produce the same
    180s wait and the same single line, so no run could tell the two apart. The
    largest gap in the window is the fact that separates them, and it is only
    worth anything beside the frame count, the trailing gap and the bytes still
    buffered.

    Since #301 those facts decide nothing — the span's end is computed from the
    audio it began with — but they are still measured and still reported on both
    exits, which is what this class is about and why it outlived the rule.

    Built without `aiortc`, `av` or `sounddevice` — CI installs none of them —
    and on a clock the test moves by hand.
    """

    def drained(self, transport: Any, timeout_seconds: float) -> None:
        asyncio.run(transport.playback_drained(timeout_seconds))

    def test_a_timed_out_playout_says_what_the_inbound_stream_did(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The wait ran out. Say so, with numbers — that is the whole of #230.

        Since #301 the only way to reach the bound is a span holding more audio
        than the caller allowed time for, so that is the shape here: five frames
        the device never took, 100 ms of unheard audio behind a 20 ms bound. What the
        line has to carry is unchanged.
        """
        speaker, clock = self.buffering(monkeypatch)
        self.heard(speaker, [0.02] * 5, clock)

        with caplog.at_level(logging.INFO):
            self.drained(self.transport(speaker), 0.02)

        line = next(
            said for said in (record.getMessage() for record in caplog.records) if "drained" in said
        )
        held = 5 * webrtc.FRAME_SAMPLES * webrtc.SAMPLE_BYTES
        assert "had not drained" in line
        assert "5 inbound frames" in line
        assert "last 0.000s ago" in line
        assert "largest gap 0.020s" in line
        assert f"{held} bytes still buffered" in line

    def test_an_ordinary_stop_edge_says_the_same_four_things(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clean run and a stalled run are only comparable if both are written down."""
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 5, clock)

        with caplog.at_level(logging.INFO):
            self.drained(self.transport(speaker), 5.0)

        line = next(
            said for said in (record.getMessage() for record in caplog.records) if "drained" in said
        )
        assert "had not drained" not in line
        assert "5 inbound frames" in line
        assert "last 0.000s ago" in line
        assert "largest gap 0.020s" in line
        assert "0 bytes still buffered" in line

    def test_a_burst_after_a_silence_is_the_fact_that_separates_the_two_causes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Frames kept arriving *and* there was a 1.4s hole: the loop was starved.

        A count on its own cannot say this — eleven frames is eleven frames
        whether they came evenly or in two bursts — and the trailing gap cannot
        either, because the burst refreshed it. The largest gap is the whole
        reason it is measured.
        """
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 5 + [1.4] + [0.02] * 5, clock)

        playout = speaker.playout

        assert playout.frames == 11
        assert playout.largest_gap_seconds == pytest.approx(1.4)
        assert playout.since_last_frame_seconds == pytest.approx(0.0)

    def test_each_playout_is_measured_over_its_own_span(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking the window is what closes it, so two stop edges on one call compare."""
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02, 0.02], clock)

        first = speaker.take_playout()
        self.heard(speaker, [0.02, 0.3], clock)
        second = speaker.take_playout()
        third = speaker.take_playout()

        assert (first.frames, first.largest_gap_seconds) == (2, pytest.approx(0.02))
        assert (second.frames, second.largest_gap_seconds) == (2, pytest.approx(0.3))
        assert (third.frames, third.largest_gap_seconds) == (0, None)

    def test_the_silence_between_two_stretches_of_speech_is_not_a_gap_in_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user's whole turn sits between two windows, and is not a fault.

        Measuring from the previous window's last frame would make that idle the
        maximum on every healthy call — an eight-second "largest gap" on a turn
        that went perfectly — and a real 1.4s hole inside a stall would never be
        the maximum again. The one measurement that separates the two causes
        would then separate nothing.
        """
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 3, clock)
        speaker.take_playout()

        # The user speaks for eight seconds; nothing inbound arrives, and then
        # the Voice's next stretch comes back cleanly paced.
        self.heard(speaker, [8.0] + [0.02] * 3, clock)
        second = speaker.take_playout()

        assert second.frames == 4
        assert second.largest_gap_seconds == pytest.approx(0.02)
        assert second.since_last_frame_seconds == pytest.approx(0.0)

    def test_what_is_still_queued_for_the_device_is_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of `drained`, and the alternative cause triage named.

        A device callback that stalls with residual bytes queued leaves `drained`
        False with no `dropped` line either, so the timeout log has to say how
        much is still waiting. This stream never calls its callback, which is
        that stall exactly.
        """
        _, clock = self.speaker(monkeypatch)
        # The one test here that plays out for real: a silent run buffers
        # nothing, and nothing is what would be under test.
        speaker = webrtc._Speaker(silent=False, device=None)

        async def scenario() -> None:
            speaker.attach(_InboundTrack([0.02] * 3, clock))
            await speaker._task

        with _sounddevice(_Streams(blocks_on=None)), _av(samples=480):
            asyncio.run(scenario())

        playout = speaker.playout

        assert playout.frames == 3
        assert playout.buffered_bytes == 3 * 480 * webrtc.SAMPLE_BYTES

    def test_looking_at_the_window_does_not_empty_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reading and closing are separate, so reading is safe.

        Part 2 of #230 will want a second reader of these numbers, and a look
        that silently emptied the window would take them out of the stop edge's
        own line — the one place the ticket asks for them.
        """
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 4, clock)

        assert speaker.playout.frames == 4
        assert speaker.playout.frames == 4
        assert speaker.take_playout().frames == 4
        assert speaker.playout.frames == 0

    def test_one_frame_is_one_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A log line is prose, and "1 inbound frames" is a line nobody proofread."""
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02], clock)

        assert "1 inbound frame," in str(speaker.playout)

    def test_a_playout_that_never_heard_a_frame_says_so(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No audio ever arrived is a different fact from a gap of zero.

        `drained` answers True for it — there is nothing playing that has not
        finished — so this is the shape of a stop edge on a call whose inbound
        audio never reached this side at all, and the line must not read as a
        healthy one.
        """
        speaker, _ = self.speaker(monkeypatch)

        with caplog.at_level(logging.INFO):
            self.drained(self.transport(speaker), 5.0)

        line = next(
            said for said in (record.getMessage() for record in caplog.records) if "drained" in said
        )
        assert "0 inbound frames" in line
        assert "none has arrived" in line
        assert "no gap measured" in line


class TestWhatClosesTheVoicesSpan(_PlayoutSeam):
    """#301: the engine decides the span's end from the audio it already holds.

    #235 made the server's word the rule and kept quiet as the fallback. Across
    91 spans of the engine log the server's word fired **0** times — the
    `output_audio_buffer.*` family does not exist on this wire, which this
    repo's own research had established four days earlier — quiet closed 84, and
    the bound fired 7. Every one of those 7 reports 1920 or 3840 bytes still
    buffered: a peer that pads its stream leaves a standing backlog of two to
    four frames, so neither the buffer-empty gate nor the quiet rule can ever
    become true while it pads.

    So the span now ends when the audio the Voice generated has had time to be
    heard: what the speaker is still holding when the wait begins, divided
    by the stream's own byte rate. Audio arriving after that is not the Voice
    speaking. The bound stays as a genuine last resort, and both exits still
    report what the inbound stream did (#230).

    Built without `aiortc`, `av` or `sounddevice`, on a clock moved by hand, the
    way `TestWhatAPlayoutSaysAboutItself` is.
    """

    def drained_line(self, transport: Any, timeout_seconds: float, caplog: Any) -> str:
        with caplog.at_level(logging.INFO):
            asyncio.run(transport.playback_drained(timeout_seconds))
        return self.line(caplog)

    def test_a_peer_that_never_stops_sending_no_longer_holds_the_span_open(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The regression test: the 2026-09-09 stall, and it is red without the rule.

        The peer keeps padding for the whole wait, so the buffer never reaches
        zero and the inbound stream never goes quiet — the two facts the old
        check needed. The span closes anyway, on the audio it began with, and
        long before the 5s bound.
        """
        speaker, clock = self.buffering(monkeypatch)
        padding = _PaddingTrack(clock)

        async def scenario() -> None:
            speaker.attach(padding)
            await padding.sent(2)
            await self.transport(speaker).playback_drained(5.0)
            speaker.stop()

        with caplog.at_level(logging.INFO):
            with _sounddevice(_Streams(blocks_on=None)), _av(samples=webrtc.FRAME_SAMPLES):
                asyncio.run(scenario())

        line = self.line(caplog)
        assert "had not drained" not in line
        assert webrtc.SpanClosedBy.HEARD.value in line
        # It began with the two frames the peer had already sent, and closed on
        # those — while the peer went on sending, which is the whole point: the
        # buffer never emptied and the stream never went quiet.
        assert f"began with {2 * webrtc.FRAME_SAMPLES * webrtc.SAMPLE_BYTES} bytes" in line
        assert padding.frames > 2
        # The clean exit carries the whole record, not just the field that
        # closed it: the inbound facts are the sentinel for the next stall and
        # a run is only diagnosable against a clean one (#230).
        assert f"{padding.frames} inbound frames" in line
        assert "last 0.000s ago" in line
        assert "largest gap 0.020s" in line
        assert "bytes still buffered" in line

    def test_a_peer_that_stops_closes_no_sooner_than_the_audio_it_left_behind(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The span is a duration, not a formality: the user hears all of it.

        Three frames of 20 ms are 60 ms of audio nobody has heard yet when the
        wait begins, and returning before then would cut the Voice off — the
        one thing the old buffer-empty gate did get right.
        """
        speaker, clock = self.buffering(monkeypatch)
        self.heard(speaker, [0.02] * 3, clock)
        unheard = 3 * webrtc.FRAME_SECONDS

        started = time.monotonic()
        line = self.drained_line(self.transport(speaker), 5.0, caplog)
        waited = time.monotonic() - started

        assert waited >= unheard
        assert "had not drained" not in line
        assert webrtc.SpanClosedBy.HEARD.value in line

    def test_a_span_that_begins_holding_nothing_closes_at_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing held is no wait: the device has already played all it was given."""
        speaker, _ = self.buffering(monkeypatch)

        started = time.monotonic()
        line = self.drained_line(self.transport(speaker), 5.0, caplog)
        waited = time.monotonic() - started

        assert waited < webrtc.FRAME_SECONDS
        assert webrtc.SpanClosedBy.HEARD.value in line
        assert "began with 0 bytes" in line

    def test_a_silent_run_closes_the_way_it_always_did(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No speaker, so nothing to wait out — and the inbound facts still reported.

        Under the old rule this needed the peer to go quiet first. It no longer
        does, which is the same outcome by a shorter road: there is no audio
        trailing anywhere on a run with no device.
        """
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 5, clock)

        line = self.drained_line(self.transport(speaker), 5.0, caplog)

        assert webrtc.SpanClosedBy.HEARD.value in line
        assert "began with 0 bytes" in line
        assert "5 inbound frames" in line
        assert "last 0.000s ago" in line

    def test_a_span_longer_than_the_bound_is_the_one_way_the_bound_still_fires(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The last resort, and it still says everything the diagnosis needs.

        Under the new rule nothing else can reach it: a span whose audio fits
        inside the bound always closes on that. So the bound firing means
        the engine was holding more audio than the caller allowed time for,
        and the line has to carry both numbers.
        """
        speaker, clock = self.buffering(monkeypatch)
        self.heard(speaker, [0.02] * 3, clock)

        line = self.drained_line(self.transport(speaker), 0.02, caplog)

        assert "had not drained" in line
        assert webrtc.SpanClosedBy.HEARD.value not in line
        began = 3 * webrtc.FRAME_SAMPLES * webrtc.SAMPLE_BYTES
        assert f"began with {began} bytes" in line
        assert "3 inbound frames" in line
        assert "last 0.000s ago" in line
        assert "largest gap 0.020s" in line
        assert f"{began} bytes still buffered" in line

    def test_the_events_channel_still_names_each_type_it_carries(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The sentinel that answered #235's question, kept to answer the next one.

        Nothing on this channel closes a span any more, so the channel is read
        for one purpose only: to say, once per type per call, what the backend
        actually sends. A codex protocol change should be noticed in the log
        rather than silently absorbed.
        """
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 3, clock)
        transport = self.transport(speaker)

        heard = transport._read_channel_event
        with caplog.at_level(logging.INFO):
            heard('{"type": "turn.created", "event_id": "e1"}')
            heard(b'{"type": "turn.done", "response_id": "r1"}')
            heard('{"type": "turn.done", "response_id": "r2"}')

        carried = [
            said for said in (record.getMessage() for record in caplog.records) if "carried" in said
        ]
        assert len(carried) == 2
        assert any("turn.created" in said for said in carried)
        assert any("turn.done" in said for said in carried)

    def test_what_is_not_an_event_on_the_channel_is_not_named(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        speaker, clock = self.speaker(monkeypatch)
        self.heard(speaker, [0.02] * 3, clock)
        transport = self.transport(speaker)

        with caplog.at_level(logging.INFO):
            transport._read_channel_event("not json")
            transport._read_channel_event("[1, 2]")
            transport._read_channel_event('{"event_id": "e1"}')
            transport._read_channel_event('{"type": 7}')

        said = [line.getMessage() for line in caplog.records]
        assert [line for line in said if "carried" in line] == []
