"""One request in, one Bridge Core verb called, one reply out.

The load-bearing test in this file is `TestWithEverySwitchOff`: ADR 0002 says
the control plane is never gated, by anything, ever, and the reference
implementation gated seven of these actions behind the Duty Switch. That
behaviour is dropped rather than ported, and this is what would catch it coming
back.

Everything else here is translation: the payload shapes the Swift shell will
implement against, and the refusals keeping their identity — Bridge Core's own
words, under a code a surface can branch on.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, instruction_context
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.control_plane.commands import render
from gpt_voicecoding.control_plane.progress_publication import ProgressPublication
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.briefing import ASSISTANT_OPENING_LINE, ASSISTANT_UNAVAILABLE_HINT
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    LaneUnavailable,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
    ReplyWindowChanged,
    SessionInspection,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.call import RETURN_LEG_BUDGET_BYTES
from gpt_voicecoding.seams.control_plane import (
    Action,
    ErrorCode,
    MalformedRequest,
    Reader,
    Reply,
    Request,
)
from gpt_voicecoding.seams.identity import (
    AgentKind,
    SessionName,
    SessionTarget,
    new_request_id,
)

WORKSPACE = Path("/tmp/workspace")
SECOND_WORKSPACE = Path("/tmp/another-workspace")
NAME = SessionName(project="GPT-VoiceCoding", task="build the control plane")
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="claude-abc", pid=1234)
SECOND_CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="def")
CODEX_ADDRESS = {"agent": "codex", "session_id": "abc", "pid": None}

#: One fixed moment, so a rendered reading is compared against a value and not a clock.
READ_AT = datetime(2026, 8, 26, 2, 44, 39, tzinfo=UTC)


def wire(reply: Reply) -> bytes:
    return json.dumps(reply.as_document(), ensure_ascii=False).encode("utf-8") + b"\n"


class Surface:
    """One assembled engine-side control plane, and the knobs a test needs."""

    def __init__(
        self,
        *,
        duty: bool = True,
        max_bytes: int = 65_536,
        page_entries: int = CorePolicy().history_page_entries,
        assistant: bool = False,
        call: FakeCall | None = None,
    ) -> None:
        self.agent = FakeAgent()
        self.call = call or FakeCall()
        self.channel = FakeCompanionChannel()
        self.state = BridgeState(
            switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
        )
        self.state.switches.flip(SwitchName.DUTY, duty)
        self.state.switches.flip(SwitchName.VOICE, duty)
        self.state.switches.flip(SwitchName.MESSAGE, duty)
        self.core = BridgeCore(
            state=self.state,
            call=self.call,
            channel=self.channel,
            agents={AgentKind.CLAUDE: self.agent, AgentKind.CODEX: self.agent},
            inventory=(SeamLoad(seam="call", configured="a.call"),),
            instruction_context=instruction_context(),
            policy=CorePolicy(history_page_entries=page_entries),
            open_conversation=self._open_conversation if assistant else None,
        )
        self.plane = ControlPlane(
            self.core,
            progress_publication=ProgressPublication(max_bytes=max_bytes),
        )

    async def _open_conversation(self) -> str:
        """What the composition root's own closure does, over the fake Call seam."""
        return await self.call.open_conversation(model="a-model", instructions="the rules")

    def ask(self, action: Action, *, reader: Reader | None = None, **payload: object) -> Reply:
        return asyncio.run(
            self.plane.handle(Request(action=action, payload=payload, reader=reader))
        )

    def register(self, target: SessionTarget = CODEX) -> Session:
        """Put one Session on the roster.

        Written straight into the registry because nothing else can: the launch
        transaction that used to seed these tests is parked (#72), and the
        discovery path that replaces it is not built yet. What these tests are
        about is what the control plane does with a roster, not how a row got
        onto one.
        """
        return self.state.sessions.register(
            Session(target=target, name=NAME, workspace=WORKSPACE, first_seen=0.0)
        )

    def open_window(self) -> None:
        """The Session says it will take a user turn now."""
        asyncio.run(self.core.dispatch(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN)))

    def dialog_on_screen(self, target: SessionTarget = CODEX, approval_id: str = "a1") -> None:
        """One Session stopped on a permission its lane is still holding.

        The product's only path to that state since #191: the Stop carries the
        dialog's handle in its `WaitingFor`, and the roster row is where the
        Approval Relay finds it.
        """
        asyncio.run(
            self.core.dispatch(
                SessionStopped(
                    target=target,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.PERMISSION,
                        tool_name="Bash",
                        approval_id=approval_id,
                    ),
                )
            )
        )


class TestWithEverySwitchOff:
    """ADR 0002, absolute: every action answers with Duty off. All of them."""

    def test_every_action_succeeds_with_duty_voice_and_message_off(self) -> None:
        surface = Surface(duty=False)
        surface.register()
        # `history` needs a lane that holds a record, or it refuses for a real
        # reason — which is not this test's subject. The subject is that no
        # *switch* refuses anything, so the lane is given one to answer from.
        surface.agent.records[CODEX] = ()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=ProgressObservation.readable(
                        has_history=False,
                        read_at=READ_AT,
                    ),
                ),
            )
        )
        surface.dialog_on_screen()

        replies = {
            Action.STATUS: surface.ask(Action.STATUS),
            Action.SWITCH: surface.ask(Action.SWITCH, name="duty", on=False),
            Action.BRIEF: surface.ask(Action.BRIEF),
            Action.HISTORY: surface.ask(Action.HISTORY, target=CODEX_ADDRESS),
            Action.LIVE: surface.ask(Action.LIVE),
            Action.RELAY: surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on"),
            Action.APPROVE: surface.ask(Action.APPROVE, approval_id="a1", verdict="allow"),
            Action.VERIFY: surface.ask(Action.VERIFY),
            Action.SESSIONS: surface.ask(Action.SESSIONS),
            Action.CONFIG: surface.ask(Action.CONFIG),
        }

        refused = {action: reply.error for action, reply in replies.items() if not reply.ok}
        assert refused == {}

    def test_the_duty_switch_can_be_turned_back_on_from_a_surface(self) -> None:
        """The forcing scenario: locked out by the switch meant to protect you."""
        surface = Surface(duty=False)

        reply = surface.ask(Action.SWITCH, name="duty", on=True)

        assert reply.ok
        assert reply.data["on"] is True
        assert reply.data["previous"] is False
        assert surface.core.status().switches.as_mapping()["duty"] is True

    def test_the_live_toggle_ends_a_call_while_the_voice_switch_is_off(self) -> None:
        surface = Surface(duty=False)
        assert surface.ask(Action.LIVE).data["state"] == "up"

        reply = surface.ask(Action.LIVE)

        assert reply.ok
        assert reply.data["state"] == "down"
        assert reply.data["call_id"] is None


class TestStatus:
    def test_question_option_descriptions_cross_the_control_plane(self) -> None:
        surface = Surface()
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=WORKSPACE,
                first_seen=0.0,
                state=SessionState.WAITING,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which base?",
                    options=(
                        Option(
                            text="main",
                            description="Merge into the default branch",
                        ),
                    ),
                ),
            )
        )

        options = surface.ask(Action.STATUS).data["sessions"][0]["waiting_for"]["options"]

        assert options == [
            {
                "text": "main",
                "description": "Merge into the default branch",
                "recommended": False,
            }
        ]

    def test_status_renders_the_whole_hub(self) -> None:
        surface = Surface()
        surface.register()

        data = surface.ask(Action.STATUS).data

        assert data["switches"]["duty"] is True
        assert data["call_id"] is None
        assert data["sessions"][0]["target"] == CODEX_ADDRESS
        assert data["sessions"][0]["name"] == str(NAME)
        # Two facts, two fields: whether the Session is still there, and what
        # it is doing. They used to be one enum, which is how a busy Session and
        # a finished one read alike.
        assert data["sessions"][0]["lifecycle"] == "live"
        assert data["sessions"][0]["state"] == "running"
        assert data["pending_relays"] == []
        # Protocol 8: no second list beside the rows. A pending permission is
        # the row's own `waiting_for`, and the panel counts those (#191).
        assert "pending_approvals" not in data

    def test_a_queued_relay_is_rendered_whole_and_carries_no_deadline(self) -> None:
        """#321: the ceiling is abolished, so a queued Relay has no `expires_at`.

        Rendered with something actually queued, because a status read on an
        empty queue is a status read that never touches this document at all.
        """
        surface = Surface()
        surface.register()
        surface.state.relays.enqueue(
            PendingRelay(
                request_id=new_request_id(),
                target=CODEX,
                kind=RelayKind.ANSWER,
                text="ship it",
                queued_at=1_000.0,
            )
        )

        (queued,) = surface.ask(Action.STATUS).data["pending_relays"]

        assert queued["text"] == "ship it"
        assert queued["queued_at"] == 1_000.0
        assert "expires_at" not in queued

    def test_the_roster_is_answerable_on_its_own(self) -> None:
        """The Roster Brief names the same Sessions `status` holds rows for."""
        surface = Surface()
        surface.register()

        rows = surface.ask(Action.BRIEF).data["roster"]["rows"]

        assert [row["target"] for row in rows] == [
            row["target"] for row in surface.ask(Action.STATUS).data["sessions"]
        ]

    def test_a_session_not_yet_read_is_not_published_as_empty_history(self) -> None:
        surface = Surface()
        surface.register()

        progress = surface.ask(Action.STATUS).data["sessions"][0]["progress"]

        assert progress == {
            "availability": "not_read",
            "has_history": None,
            "omission": "none",
            "read_at": None,
            "recent": [],
        }

    def test_status_reports_an_unreadable_source_as_lane_degradation(self) -> None:
        surface = Surface()
        surface.register()
        reason = "the daemon dropped the progress read"
        surface.state.sessions.observe(
            AgentKind.CODEX,
            LaneDiscovery(
                rows=(
                    SessionInspection(
                        target=CODEX,
                        workspace=WORKSPACE,
                        progress=ProgressObservation.unreadable(reason),
                    ),
                ),
                degraded=reason,
            ),
            now=1.0,
        )

        data = surface.ask(Action.STATUS).data

        assert data["degraded_lanes"] == {"codex": reason}
        assert data["sessions"][0]["progress"]["availability"] == "not_read"

    def test_thirty_eight_large_histories_publish_every_compact_row_within_the_limit(
        self,
    ) -> None:
        surface = Surface()
        for index in range(38):
            surface.state.sessions.register(
                Session(
                    target=SessionTarget(
                        agent=AgentKind.CODEX,
                        session_id=f"session-{index}",
                    ),
                    workspace=WORKSPACE,
                    first_seen=float(index),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(
                                ordinal=0,
                                role=ProgressRole.ASSISTANT,
                                text="x" * 20_000,
                            ),
                        ),
                        omission=ProgressOmission.NONE,
                        read_at=READ_AT,
                    ),
                )
            )

        reply = surface.ask(Action.STATUS)

        assert reply.ok
        assert len(reply.data["sessions"]) == 38
        assert all(
            row["progress"]["recent"] == [] and row["progress"]["omission"] == "status_summary"
            for row in reply.data["sessions"]
        )
        assert len(wire(reply)) <= 65_536

    def test_an_over_limit_status_skeleton_returns_a_bounded_refusal_without_losing_rows(
        self,
    ) -> None:
        surface = Surface(max_bytes=512)
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=Path("/" + "large-workspace/" * 100),
                first_seen=0.0,
            )
        )

        reply = surface.ask(Action.STATUS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert len(wire(reply)) <= 512
        assert len(surface.state.sessions.all()) == 1


class TestRefusalsKeepTheirIdentity:
    def test_an_unknown_switch(self) -> None:
        reply = Surface().ask(Action.SWITCH, name="sound", on=True)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SWITCH
        assert "sound" in reply.error.message

    def test_a_switch_state_that_is_not_a_state(self) -> None:
        reply = Surface().ask(Action.SWITCH, name="duty", on="off")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_an_unknown_session(self) -> None:
        reply = Surface().ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_claude_target_without_a_pid_never_reaches_the_hub(self) -> None:
        """A resumed Claude session forks; a target without a pid is ambiguous."""
        reply = Surface().ask(
            Action.RELAY,
            target={"agent": "claude", "session_id": "abc", "pid": None},
            text="carry on",
        )

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD
        assert "pid" in reply.error.message

    def test_a_verdict_nothing_is_waiting_for(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="never", verdict="allow")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_PENDING

    def test_a_question_prompt_id_is_not_an_approval_that_can_be_approved(self) -> None:
        surface = Surface()
        surface.register(CLAUDE)
        asyncio.run(
            surface.core.dispatch(
                SessionStopped(
                    target=CLAUDE,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                        approval_id="question-prompt",
                    ),
                )
            )
        )

        reply = surface.ask(
            Action.APPROVE,
            approval_id="question-prompt",
            verdict="allow",
        )

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_PENDING
        assert surface.agent.calls == []

    def test_a_verdict_that_is_not_one(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="a1", verdict="maybe")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_relayed_words_that_are_not_there(self) -> None:
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="   ")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD


class TestRelayingAndApproving:
    def test_the_users_words_reach_the_session(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        data = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on").data

        assert data["state"] == "delivered"
        assert [call.text for call in surface.agent.calls] == ["carry on"]

    def test_a_relay_names_the_route_it_took(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        data = surface.ask(
            Action.RELAY, target=CODEX_ADDRESS, text="carry on", route="deliver"
        ).data

        assert data["route"] == "deliver"

    def test_a_verdict_is_carried_and_answered_with_its_receipt(self) -> None:
        """The receipt is the Relay's: a state, a grade and a reason (#192)."""
        surface = Surface()
        surface.register()
        surface.dialog_on_screen()

        data = surface.ask(Action.APPROVE, approval_id="a1", verdict="allow").data

        assert data["verdict"] == "allow"
        assert data["approval_id"] == "a1"
        assert data["state"] == "delivered"
        assert data["receipt"]["outcome"] == "delivered"
        assert data["reason"] == "delivered"
        assert "closing_notice" not in data
        assert [call.verdict for call in surface.agent.calls if call.verb == "approval_relay"]

    def test_a_verdict_for_a_spawned_target_is_refused_in_its_own_words(self) -> None:
        """ "Never spoken to" includes never answered, and it reads differently."""
        child = SessionTarget(agent=AgentKind.CODEX, session_id="child-1", pid=77)
        surface = Surface()
        surface.register()
        surface.state.sessions.register(
            Session(
                target=child,
                name=NAME,
                workspace=WORKSPACE,
                first_seen=0.0,
                child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
            )
        )
        surface.state.sessions.set_stop_reading(
            child,
            waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name="Bash", approval_id="c1"),
            progress=ProgressObservation(),
            now=0.0,
        )

        reply = surface.ask(Action.APPROVE, approval_id="c1", verdict="allow")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "Child Process" in reply.error.message
        assert surface.agent.calls == []


class TestVerify:
    def test_verify_reports_every_seam_the_engine_was_asked_about(self) -> None:
        data = Surface().ask(Action.VERIFY).data

        assert data["seams"] == [
            {
                "seam": "call",
                "outcome": "pass",
                "configured": "a.call",
                "loaded": "tests.fakes.FakeCall",
                "detail": "",
            }
        ]


class TestTheCommandSetIsOne:
    def test_the_command_words_are_exactly_the_action_set(self) -> None:
        """The Companion Channel's `/` grammar and bridgectl are one command set."""
        assert ControlPlane(Surface().core).commands == {str(action) for action in Action}


@pytest.mark.parametrize("action", list(Action))
def test_every_action_is_dispatchable(action: Action) -> None:
    """A closed action set with a handler missing is a wire that lies."""
    assert action in ControlPlane(Surface().core).handlers


class TestHistory:
    """#171's verb: one page of one Session's own words, older on request."""

    SAID = (
        "the first thing",
        "the second thing",
        "the third thing",
        "the fourth thing",
        "the fifth thing",
        "the sixth thing",
        "the seventh thing",
    )

    def record(self, said: tuple[str, ...] | None = None) -> tuple[ProgressEntry, ...]:
        """One Session's whole visible record, oldest first and numbered."""
        return tuple(
            ProgressEntry(
                ordinal=index,
                role=ProgressRole.USER if index % 2 == 0 else ProgressRole.ASSISTANT,
                text=text,
            )
            for index, text in enumerate(self.SAID if said is None else said)
        )

    def surface(self, said: tuple[str, ...] | None = None, **kwargs: object) -> Surface:
        surface = Surface(**kwargs)  # type: ignore[arg-type]
        surface.register()
        surface.agent.records[CODEX] = self.record(said)
        return surface

    def test_the_newest_page_includes_the_newest_entry(self) -> None:
        """Every page is complete on its own; the engine remembers nothing (#171)."""
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.ok
        assert [entry["text"] for entry in reply.data["entries"]] == [
            "the seventh thing",
            "the sixth thing",
            "the fifth thing",
            "the fourth thing",
            "the third thing",
        ]
        assert [entry["ordinal"] for entry in reply.data["entries"]] == [6, 5, 4, 3, 2]
        assert reply.data["older"] is True

    def test_the_page_size_is_the_engines_dial_and_never_the_callers(self) -> None:
        surface = self.surface(page_entries=2)

        entries = surface.ask(Action.HISTORY, target=CODEX_ADDRESS).data["entries"]

        assert [entry["ordinal"] for entry in entries] == [6, 5]

    def test_the_cursor_asks_for_the_entries_before_it(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=2)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [1, 0]
        assert reply.data["older"] is False

    def test_a_cursor_past_the_oldest_entry_is_an_empty_page_rather_than_a_refusal(
        self,
    ) -> None:
        """An answer, not a refusal: there is simply nothing before the first thing said."""
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=0)

        assert reply.ok
        assert reply.data["entries"] == []
        assert reply.data["older"] is False
        assert reply.data["read_at"] is not None

    def test_a_cursor_above_every_ordinal_is_the_newest_page(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=9_999)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [6, 5, 4, 3, 2]

    def test_fewer_entries_than_a_page_is_the_whole_history(self) -> None:
        surface = self.surface(said=("only this", "and this"))

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [1, 0]
        assert reply.data["older"] is False

    def test_an_ordinal_names_the_same_entry_across_a_read_while_the_session_appends(
        self,
    ) -> None:
        """Both sources are append-only for what this seam keeps, so a cursor holds."""
        surface = self.surface()
        first = surface.ask(Action.HISTORY, target=CODEX_ADDRESS).data

        surface.agent.records[CODEX] = self.record((*self.SAID, "and one more"))
        after = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=2).data

        assert [entry["ordinal"] for entry in first["entries"]] == [6, 5, 4, 3, 2]
        assert [entry["text"] for entry in after["entries"]] == [
            "the second thing",
            "the first thing",
        ]

    def test_an_oversize_entry_keeps_its_slot_and_the_page_advances(self) -> None:
        """ADR 0016: named as omitted, never cut, and never silently dropped."""
        surface = self.surface(
            said=("small one", "x" * 4_000, "another small one"),
            max_bytes=2_048,
        )

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["entries"] == [
            {"ordinal": 2, "role": "user", "text": "another small one"},
            {"ordinal": 1, "role": "assistant", "omission": "oversize"},
            {"ordinal": 0, "role": "user", "text": "small one"},
        ]
        assert len(wire(reply)) <= 2_048

    def test_a_page_is_never_folded_into_the_roster(self) -> None:
        """`inspect` is the roster's read; a page is a separate one (ADR 0016)."""
        surface = self.surface()

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert surface.agent.inspections == []
        assert surface.agent.calls == []
        assert surface.ask(Action.STATUS).data["sessions"][0]["progress"] == {
            "availability": "not_read",
            "has_history": None,
            "omission": "none",
            "read_at": None,
            "recent": [],
        }

    def test_one_lane_read_per_page(self) -> None:
        """A second `inspect` for a fresher staleness check would fold it back in."""
        surface = self.surface()

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=4)

        assert surface.agent.pages == [(CODEX, 4, 5)]

    def test_a_session_nobody_registered_is_refused_by_identity(self) -> None:
        surface = Surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION
        assert surface.agent.pages == []

    def test_a_session_that_has_ended_is_a_stale_target_not_an_empty_page(self) -> None:
        surface = self.surface()
        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(), now=1.0)

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.STALE_SESSION
        assert surface.agent.pages == []

    def test_a_child_process_is_refused_before_any_lane_is_touched(self) -> None:
        """Seen, never spoken to — and never asked on its own behalf either (#68)."""
        surface = Surface()
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=WORKSPACE,
                first_seen=0.0,
                child=ChildClassification(kind=ChildKind.CHILD, parent=SECOND_CODEX),
            )
        )

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert surface.agent.pages == []

    def test_a_lane_that_could_not_look_refuses_rather_than_saying_nothing_was_said(
        self,
    ) -> None:
        """ "I could not look" and "it has said nothing" are different facts."""
        surface = self.surface()
        surface.agent.history_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "`codex` is not on PATH" in reply.error.message

    def test_a_lane_holding_no_record_refuses_rather_than_answering_an_empty_page(
        self,
    ) -> None:
        """A Codex thread the daemon does not hold, or a transcript nobody named."""
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "never infers one" in reply.error.message

    def test_a_lane_that_could_not_look_leaves_the_row_as_it_was(self) -> None:
        surface = self.surface()
        surface.agent.history_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert surface.ask(Action.STATUS).data["sessions"][0]["lifecycle"] == "live"

    def test_a_cursor_that_is_not_an_ordinal_is_an_unusable_payload(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before="newest")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_unicode_and_json_escaping_are_measured_as_actual_wire_bytes(self) -> None:
        text = ('雪"\\\n' * 90) + "done"
        measuring = self.surface(said=("small one", text))
        complete = measuring.ask(Action.HISTORY, target=CODEX_ADDRESS)
        exact_capacity = len(wire(complete))

        fitting = self.surface(said=("small one", text), max_bytes=exact_capacity)
        fits = fitting.ask(Action.HISTORY, target=CODEX_ADDRESS)

        too_small = self.surface(said=("small one", text), max_bytes=exact_capacity - 1)
        omitted = too_small.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert fits.data["entries"][0]["text"] == text
        assert len(wire(fits)) == exact_capacity
        assert omitted.data["entries"][0] == {
            "ordinal": 1,
            "role": "assistant",
            "omission": "oversize",
        }
        assert omitted.data["entries"][1]["text"] == "small one"
        assert len(wire(omitted)) <= exact_capacity - 1


class TestBrief:
    """The Briefing verb on the wire — one address, or none at all."""

    def test_state_words_travel_as_fields_on_both_briefs(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        detail = surface.ask(Action.BRIEF, target=CODEX_ADDRESS).data
        roster = surface.ask(Action.BRIEF).data

        assert detail["session"]["state_word"] == "waiting for your decision"
        assert roster["roster"]["rows"][0]["state_word"] == "waiting for your decision"

    def test_status_carries_the_same_brief_state_for_a_prose_question(self) -> None:
        from test_briefing import row, said

        surface = Surface()
        surface.state.sessions.register(row(progress=said("Which base should I use?")))

        status = surface.ask(Action.STATUS).data["sessions"][0]
        brief = surface.ask(Action.BRIEF).data["roster"]["rows"][0]

        assert status["state"] == "idle"
        assert status["waiting_for"]["kind"] == "none"
        assert status["brief_state"] == brief["state"] == "decision"

    def test_project_names_a_session_before_its_task_arrives(self) -> None:
        surface = Surface()
        row = SessionInspection(target=CODEX, workspace=WORKSPACE, project_name="Project")
        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(rows=(row,)), now=0.0)
        assert surface.ask(Action.BRIEF).data["roster"]["rows"][0]["name"] == "Project"
        row = SessionInspection(
            target=CODEX, workspace=WORKSPACE, project_name="Project", thread_name="Build the panel"
        )
        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(rows=(row,)), now=1.0)
        assert (
            surface.ask(Action.BRIEF).data["roster"]["rows"][0]["name"]
            == "Project · Build the panel"
        )

    def test_roster_activity_order_and_message_start_ignore_focus(self) -> None:
        surface = Surface()
        surface.state.sessions.register(
            Session(target=CODEX, workspace=WORKSPACE, first_seen=0.0, name=NAME)
        )
        surface.state.sessions.register(
            Session(
                target=SECOND_CODEX,
                workspace=WORKSPACE,
                first_seen=1.0,
                name=NAME,
                last_activity=READ_AT,
                progress=ProgressObservation.readable(
                    has_history=True,
                    recent=(
                        ProgressEntry(
                            ordinal=0,
                            role=ProgressRole.ASSISTANT,
                            text="The newest message\nFurther detail",
                        ),
                    ),
                    read_at=READ_AT,
                ),
            )
        )
        surface.open_window()
        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        rows = surface.ask(Action.BRIEF).data["roster"]["rows"]

        assert [row["target"]["session_id"] for row in rows] == ["def", "abc"]
        assert rows[0]["newest"] == "The newest message"
        assert rows[0]["last_activity_at"] == READ_AT.isoformat()
        assert rows[1]["newest"] is None
        assert rows[1]["last_activity_at"] is None

    def stopped_on_a_question(self) -> LaneDiscovery:
        return LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                        options=(Option(text="main", recommended=True),),
                        recommendation="main",
                    ),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(
                                ordinal=0, role=ProgressRole.ASSISTANT, text="I got this far"
                            ),
                        ),
                        read_at=READ_AT,
                    ),
                    last_activity=READ_AT,
                ),
            )
        )

    def test_with_no_address_it_answers_the_roster_brief(self) -> None:
        surface = Surface()
        surface.register()

        data = surface.ask(Action.BRIEF).data

        assert data["kind"] == "roster"
        assert data["roster"]["rows"][0]["target"] == CODEX_ADDRESS
        assert data["text"]

    def test_with_no_address_it_touches_no_lane(self) -> None:
        """The Roster Brief is a read of what the hub already holds."""
        surface = Surface()
        surface.register()

        surface.ask(Action.BRIEF)

        assert surface.agent.inspections == []

    def test_an_address_reads_that_one_session_now_through_one_inspect(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        data = surface.ask(Action.BRIEF, target=CODEX_ADDRESS).data

        assert surface.agent.inspections == [CODEX]
        assert surface.agent.calls == []
        assert data["kind"] == "session"
        assert data["session"]["state"] == "decision"
        assert data["session"]["newest"] == {"state": "said", "text": "I got this far"}
        assert data["session"]["decision"] == {
            "prompt": "Which base?",
            "options": [{"text": "main", "description": None, "recommended": True}],
            "recommendation": "main",
            "tool": None,
            "summary": None,
        }
        assert data["session"]["last_activity_at"] == READ_AT.isoformat()

    def test_the_rendered_text_travels_beside_the_structure(self) -> None:
        """One renderer (#166 B6): `bridgectl` prints this rather than composing."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        data = surface.ask(Action.BRIEF, target=CODEX_ADDRESS).data

        assert "Which base?" in data["text"]
        assert "waiting for your decision" in data["text"]

    def test_the_reading_becomes_the_rosters_truth(self) -> None:
        """Asking for a brief then for status cannot say two things."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert surface.ask(Action.STATUS).data["sessions"][0]["state"] == "waiting"

    def test_a_session_nobody_registered_is_refused_by_identity(self) -> None:
        surface = Surface()

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_session_that_has_ended_is_a_stale_target(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery()  # a lane that looked and found nothing

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.STALE_SESSION

    def test_a_lane_that_could_not_look_refuses_in_the_lanes_own_words(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.inspect_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "`codex` is not on PATH" in reply.error.message

    def test_a_spawned_child_is_refused_and_the_refusal_names_it(self) -> None:
        """Seen, never spoken to (#68) — and never briefed about either."""
        surface = Surface()
        surface.state.sessions.observe(
            AgentKind.CODEX,
            LaneDiscovery(
                rows=(
                    SessionInspection(
                        target=CODEX,
                        workspace=WORKSPACE,
                        child=ChildClassification(kind=ChildKind.CHILD, parent=SECOND_CODEX),
                    ),
                )
            ),
            now=1.0,
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert str(CODEX) in reply.error.message
        assert "Child Process" in reply.error.message

    def test_a_read_that_failed_now_is_reported_now_and_not_from_the_last_tick(self) -> None:
        """A brief is one reading taken at the moment the user is spoken to.

        The roster keeps its last readable observation on purpose; a verb that
        answers *now* must not borrow it, or the Voice reads out an earlier
        tick's message as though it had just been said.
        """
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()
        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=ProgressObservation.unreadable("the rollout could not be decoded"),
                ),
            )
        )
        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "unreadable", "text": None}
        # The failed read is a fact about the *message*, and since #320 it is
        # told there and nowhere else: the state says nothing is being asked.
        assert reply.data["session"]["state"] == "finished"
        # The roster still holds what it last read: a standing account does not
        # lose a fact because one pass could not answer.
        assert (
            surface.ask(Action.STATUS).data["sessions"][0]["progress"]["availability"] == "readable"
        )

    def test_a_running_session_whose_progress_failed_now_stays_running(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.RUNNING,
                    progress=ProgressObservation.unreadable("the daemon dropped the read"),
                ),
            )
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["state"] == "running"
        assert reply.data["session"]["newest"] == {"state": "unreadable", "text": None}

    def test_a_session_whose_progress_could_not_be_read_is_briefed_not_refused(self) -> None:
        """Where `brief` and `progress` part, and why.

        `history` exists to answer with a Session's own words and has nothing
        to say without them. A brief still has a state, a wait and a name, so an
        unreadable reading becomes a state the user is told about rather than a
        refusal that tells them nothing.
        """
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
                    progress=ProgressObservation.unreadable("the rollout could not be decoded"),
                ),
            )
        )

        brief = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)
        history = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert brief.ok
        assert brief.data["session"]["state"] == "finished"
        assert brief.data["session"]["newest"]["state"] == "unreadable"
        assert history.error is not None

    def test_a_newest_message_too_large_for_the_line_is_named_rather_than_sliced(self) -> None:
        """ADR 0016 at the wire: the brief still answers, and says what it dropped.

        `newest` travels twice — as a field and inside `text` — so a message that
        fits one publication can still overflow this one. Slicing it would hand
        the user half a sentence; refusing would hand them nothing at all, when
        the state and the decision they act on both still fit.
        """
        surface = Surface(max_bytes=2_048)
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="x" * 4_000),
                        ),
                        read_at=READ_AT,
                    ),
                ),
            )
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "oversize", "text": None}
        assert reply.data["session"]["decision"]["prompt"] == "Which base?"
        assert len(wire(reply)) <= 2_048

    def test_a_malformed_address_is_refused_rather_than_read_as_no_address(self) -> None:
        """Absent and unusable are two answers, and only one is the whole roster."""
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.BRIEF, target={"agent": "codex"})

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_the_retired_exact_progress_verb_is_an_unknown_action(self) -> None:
        """`progress` retired with protocol 7; `sessions` came back in 9 as a screen."""
        reply = asyncio.run(surface_asking_raw("progress"))

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_ACTION


async def surface_asking_raw(action: str) -> Reply:
    """One line naming an action, read the way the socket reads it."""
    try:
        request = Request.of({"action": action})
    except MalformedRequest as refused:
        return Reply.refused(None, refused.code, str(refused))
    return await Surface().plane.handle(request)


class TestTheLiveCallsReturnLeg:
    """#302: a `history` or `brief` marked for the Voice fits codex's 4,000-byte line.

    The seam a real caller crosses, asserted on what that caller sees — the reply
    document and its *rendered* text, which is what the Call Agent prints and
    hands back. Never the JSON envelope: the Voice never sees one.
    """

    CHINESE = "\u4e00\u4e2a\u5f88\u957f\u7684\u6d88\u606f"

    def page(self, reply: Reply) -> dict[str, object]:
        """One page without its reading time.

        `read_at` is the moment the lane was read, so two reads never carry the
        same one. Comparing documents for equality means comparing what the
        publication chose, and the clock is not that.
        """
        return {key: value for key, value in reply.data.items() if key != "read_at"}

    def rendered(self, reply: Reply) -> int:
        """What the Call Agent hands back, in the bytes codex counts."""
        return len(render(reply).encode("utf-8"))

    def record(self, said: tuple[str, ...]) -> tuple[ProgressEntry, ...]:
        return tuple(
            ProgressEntry(
                ordinal=index,
                role=ProgressRole.USER if index % 2 == 0 else ProgressRole.ASSISTANT,
                text=text,
            )
            for index, text in enumerate(said)
        )

    def surface(self, said: tuple[str, ...], **kwargs: object) -> Surface:
        surface = Surface(**kwargs)  # type: ignore[arg-type]
        surface.register()
        surface.agent.records[CODEX] = self.record(said)
        return surface

    def briefed(self, *, newest: str, prompt: str = "Which base?") -> Surface:
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt=prompt),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=newest),
                        ),
                        read_at=READ_AT,
                    ),
                ),
            )
        )
        return surface

    # ---------------------------------------------------------------- history

    def test_a_page_over_the_ceiling_arrives_shorter_with_every_entry_whole(self) -> None:
        """User stories 1 and 2: shorter rather than cut, and nothing from the middle."""
        surface = self.surface(tuple(f"{index}: " + "x" * 900 for index in range(6)))

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES
        entries = reply.data["entries"]
        assert entries, "a page that dropped everything would say nothing"
        assert all("omission" not in entry for entry in entries)
        assert all(entry["text"] == f"{entry['ordinal']}: " + "x" * 900 for entry in entries)

    def test_the_entries_dropped_are_the_oldest_and_the_ordinals_stay_contiguous(self) -> None:
        """User story 5: the entry before a gap is never taken as the one that followed."""
        surface = self.surface(tuple(f"{index}: " + "x" * 900 for index in range(6)))

        ordinals = [
            entry["ordinal"]
            for entry in surface.ask(
                Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE
            ).data["entries"]
        ]

        assert ordinals == list(range(max(ordinals), min(ordinals) - 1, -1))
        assert max(ordinals) == 5, "the newest entry is the one that is kept"

    def test_the_cursor_names_the_oldest_entry_still_on_the_page(self) -> None:
        """User story 3: asking again brings exactly what was left off, none skipped.

        `--before` is an exclusive bound, so naming the oldest *kept* ordinal is
        what makes the next page start at the first *dropped* one.
        """
        surface = self.surface(tuple(f"{index}: " + "x" * 900 for index in range(6)))

        page = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE).data
        assert page["older"] is True
        cursor = min(int(entry["ordinal"]) for entry in page["entries"])

        following = surface.ask(
            Action.HISTORY, target=CODEX_ADDRESS, before=cursor, reader=Reader.VOICE
        ).data

        assert max(int(entry["ordinal"]) for entry in following["entries"]) == cursor - 1

    def test_an_entry_too_large_for_the_call_keeps_its_slot_by_ordinal_and_role(self) -> None:
        """User story 4: I know where the gap is and what kind of message it was."""
        surface = self.surface(("small one", "x" * 5_000, "another small one"))

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.data["entries"] == [
            {"ordinal": 2, "role": "user", "text": "another small one"},
            {"ordinal": 1, "role": "assistant", "omission": "oversize"},
            {"ordinal": 0, "role": "user", "text": "small one"},
        ]
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES
        assert "(too large to carry)" in render(reply)

    def test_a_page_of_one_entry_that_is_itself_oversize_is_the_slot_and_the_cursor(self) -> None:
        surface = self.surface(("x" * 20_000,), page_entries=1)

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert reply.data["entries"] == [{"ordinal": 0, "role": "user", "omission": "oversize"}]
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    def test_the_last_entry_left_gives_up_its_text_rather_than_overrun_the_ceiling(self) -> None:
        """The cursor line grows when the page starts paging, and the fit must survive it.

        An entry can fit a page whose cursor says "that is the whole history"
        and not the same page once dropping an older entry has turned that line
        into "older entries remain — ask again with --before N", which is longer.
        The last entry left has nothing older to give up, so it gives up its own
        text and keeps its slot. Swept across the boundary rather than asserted
        at one size, because the overrun was 25 bytes wide.
        """
        for size in range(3_600, 3_700):
            surface = self.surface(("an older one", "x" * size))

            reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

            assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES, (
                f"a page carrying one {size}-byte entry overran the return leg"
            )

    def test_a_page_that_already_fits_is_byte_for_byte_the_unmarked_document(self) -> None:
        surface = self.surface(("a short one", "and another"))

        marked = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)
        unmarked = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert self.page(marked) == self.page(unmarked)

    def test_a_page_of_chinese_crosses_on_bytes_and_is_fitted_the_same_way(self) -> None:
        """The ceiling is bytes and a character is three of them (#302)."""
        surface = self.surface(tuple(self.CHINESE * 40 for _ in range(6)))

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES
        assert all("omission" not in entry for entry in reply.data["entries"])
        assert all(entry["text"] == self.CHINESE * 40 for entry in reply.data["entries"])

    def test_an_unmarked_page_keeps_every_entry_the_window_selected(self) -> None:
        """User story 10 and 11: the call's limits never shrink the other surface.

        A differential against the *same* page marked: the marked one is fitted
        and the unmarked one is not. Comparing an unmarked reply with a second
        unmarked reply would compare a path with itself and prove nothing.
        """
        said = tuple(f"{index}: " + "x" * 900 for index in range(6))
        unmarked = self.surface(said).ask(Action.HISTORY, target=CODEX_ADDRESS)
        marked = self.surface(said).ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert [entry["ordinal"] for entry in unmarked.data["entries"]] == [5, 4, 3, 2, 1]
        assert all("omission" not in entry for entry in unmarked.data["entries"])
        assert self.rendered(unmarked) > RETURN_LEG_BUDGET_BYTES, "unfitted, as it was"
        assert len(marked.data["entries"]) < len(unmarked.data["entries"]), "the mark is what fits"

    def test_the_wire_rule_still_binds_a_page_that_fits_the_return_leg(self) -> None:
        """Both ceilings apply on a marked request, and the tighter one binds."""
        surface = self.surface(("small one", "x" * 3_000, "another small one"), max_bytes=2_048)

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert len(wire(reply)) <= 2_048
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    @staticmethod
    def carried_whole(reply: Reply) -> bool:
        """Whether the page's one entry arrived with its text rather than as a slot."""
        return "text" in reply.data["entries"][0]

    @staticmethod
    def newest_whole(reply: Reply) -> bool:
        """Whether the brief still carries its newest message rather than naming it."""
        return reply.ok and reply.data["session"]["newest"]["state"] != "oversize"

    def largest_whole(self, build, whole=None) -> int:
        """The biggest body this rendering carries whole, found rather than assumed.

        Searched on *wholeness*, not on fit: every size fits, because that is
        what the fit is for — past the edge the entry keeps its slot and gives up
        its text. The edge is therefore the largest body still carried whole, and
        it depends on labels this test does not own, so it is measured. A
        hard-coded size would silently stop testing the edge the day a label
        changed.
        """
        whole = whole or self.carried_whole
        low, high = 1, 8_000
        while low < high:
            middle = (low + high + 1) // 2
            if whole(build(middle)):
                low = middle
            else:
                high = middle - 1
        return low

    def test_a_page_sits_just_under_exactly_at_and_over_the_ceiling(self) -> None:
        """The `<=` boundary itself, so an off-by-one on it fails here."""

        def page(size: int) -> Reply:
            return self.surface(("x" * size,), page_entries=1).ask(
                Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE
            )

        edge = self.largest_whole(page)

        # ASCII costs one byte a character, so the edge lands on the ceiling itself.
        assert self.rendered(page(edge)) == RETURN_LEG_BUDGET_BYTES, "exactly at"
        assert self.carried_whole(page(edge)), "carried whole at the ceiling"
        assert self.rendered(page(edge - 1)) == RETURN_LEG_BUDGET_BYTES - 1, "just under"
        assert self.carried_whole(page(edge - 1))
        assert page(edge + 1).data["entries"][0]["omission"] == "oversize", "over"
        assert self.rendered(page(edge + 1)) <= RETURN_LEG_BUDGET_BYTES

    def test_a_chinese_page_sits_on_the_same_boundary_measured_in_bytes(self) -> None:
        """A character is three bytes, so the edge lands on a third of the count."""

        def page(size: int) -> Reply:
            return self.surface(("中" * size,), page_entries=1).ask(
                Action.HISTORY, target=CODEX_ADDRESS, reader=Reader.VOICE
            )

        edge = self.largest_whole(page)

        # Three bytes a character, so the edge is within three of the ceiling
        # rather than on it: no whole number of characters lands exactly there.
        assert self.rendered(page(edge)) > RETURN_LEG_BUDGET_BYTES - 3
        assert self.rendered(page(edge)) <= RETURN_LEG_BUDGET_BYTES
        assert page(edge).data["entries"][0]["text"] == "中" * edge
        assert page(edge + 1).data["entries"][0]["omission"] == "oversize"
        assert len(("中" * edge).encode("utf-8")) == edge * 3

    # ------------------------------------------------------------------ brief

    def test_a_brief_whose_newest_alone_is_over_the_ceiling_names_it_absent(self) -> None:
        """User story 7: an absent message is told apart from a Session that said nothing."""
        surface = self.briefed(newest="x" * 5_000)

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "oversize", "text": None}
        assert reply.data["session"]["decision"]["prompt"] == "Which base?"
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    def test_a_brief_whose_newest_fits_but_whose_whole_does_not_gives_up_the_newest(self) -> None:
        """The newest message is the only part a brief may give up.

        Sized so the message *alone* is inside the ceiling (3,650 of 3,734) while
        the brief it sits in is not (3,773) — the case that proves the fit is
        taken on the whole answer and not on the message.
        """
        surface = self.briefed(newest="x" * 3_650)

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert len(("x" * 3_650).encode("utf-8")) <= RETURN_LEG_BUDGET_BYTES
        assert reply.data["session"]["newest"] == {"state": "oversize", "text": None}
        assert reply.data["session"]["decision"]["prompt"] == "Which base?"
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    def test_a_decision_that_alone_does_not_fit_is_refused_whole_never_partly(self) -> None:
        """User story 6: a decision I am asked to make is never missing its choices."""
        surface = self.briefed(newest="short", prompt="Which base? " + "y" * 4_000)

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "y" * 4_000 not in reply.error.message
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    def test_the_refusal_names_no_session_and_carries_no_free_text(self) -> None:
        """A Session name has no byte bound, so a refusal that carried one would not either."""
        surface = self.briefed(newest="short", prompt="Which base? " + "y" * 4_000)

        message = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE).error
        assert message is not None

        assert str(NAME.project) not in message.message
        assert str(NAME.task) not in message.message
        assert len(message.message.encode("utf-8")) <= RETURN_LEG_BUDGET_BYTES

    def test_a_brief_that_already_fits_is_byte_for_byte_the_unmarked_document(self) -> None:
        surface = self.briefed(newest="it stopped on a question")

        marked = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE)
        unmarked = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert marked.data == unmarked.data

    def test_the_roster_brief_is_never_fitted_to_the_return_leg(self) -> None:
        """User story 9: the overview at the top of a call is complete."""
        surface = self.briefed(newest="x" * 5_000)

        marked = surface.ask(Action.BRIEF, reader=Reader.VOICE)
        unmarked = surface.ask(Action.BRIEF)

        assert marked.data == unmarked.data
        assert marked.data["kind"] == "roster"

    def test_an_unmarked_brief_keeps_its_newest_message_whole(self) -> None:
        """The Companion Channel keeps what it has today, byte for byte.

        The same differential the page test makes: this brief is over the return
        leg, the marked one gives up its newest, and the unmarked one does not.
        """
        unmarked = self.briefed(newest="x" * 3_650).ask(Action.BRIEF, target=CODEX_ADDRESS)
        marked = self.briefed(newest="x" * 3_650).ask(
            Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE
        )

        assert unmarked.data["session"]["newest"] == {"state": "said", "text": "x" * 3_650}
        assert self.rendered(unmarked) > RETURN_LEG_BUDGET_BYTES, "unfitted, as it was"
        assert marked.data["session"]["newest"]["state"] == "oversize"

    def test_a_chinese_brief_crosses_on_bytes_and_gives_up_its_newest(self) -> None:
        surface = self.briefed(newest=self.CHINESE * 400)

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "oversize", "text": None}
        assert self.rendered(reply) <= RETURN_LEG_BUDGET_BYTES

    def test_a_brief_sits_just_under_exactly_at_and_over_the_ceiling(self) -> None:
        """The same three-point boundary the page has, on the other rendering.

        Past the edge a brief gives up its newest message — the only part it may
        give up — so "over" is the message named absent rather than the brief
        refused; a decision that alone will not fit is the separate case above.
        """

        def brief(size: int) -> Reply:
            return self.briefed(newest="x" * size).ask(
                Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE
            )

        edge = self.largest_whole(brief, self.newest_whole)

        assert self.rendered(brief(edge)) == RETURN_LEG_BUDGET_BYTES, "exactly at"
        assert brief(edge).data["session"]["newest"]["text"] == "x" * edge
        assert self.rendered(brief(edge - 1)) == RETURN_LEG_BUDGET_BYTES - 1, "just under"
        assert self.newest_whole(brief(edge - 1))
        assert brief(edge + 1).data["session"]["newest"] == {"state": "oversize", "text": None}, (
            "over"
        )
        assert self.rendered(brief(edge + 1)) <= RETURN_LEG_BUDGET_BYTES
        # The part a brief may never give up survives the edge in every direction.
        for size in (edge - 1, edge, edge + 1):
            assert brief(size).data["session"]["decision"]["prompt"] == "Which base?"

    def test_a_chinese_brief_sits_on_the_same_boundary_measured_in_bytes(self) -> None:
        """A character is three bytes here too, so the edge lands within three."""

        def brief(size: int) -> Reply:
            return self.briefed(newest="中" * size).ask(
                Action.BRIEF, target=CODEX_ADDRESS, reader=Reader.VOICE
            )

        edge = self.largest_whole(brief, self.newest_whole)

        assert self.rendered(brief(edge)) > RETURN_LEG_BUDGET_BYTES - 3
        assert self.rendered(brief(edge)) <= RETURN_LEG_BUDGET_BYTES
        assert brief(edge).data["session"]["newest"]["text"] == "中" * edge
        assert brief(edge + 1).data["session"]["newest"]["state"] == "oversize"
        assert len(("中" * edge).encode("utf-8")) == edge * 3

    # ----------------------------------------------------------------- the mark

    def test_an_unrecognised_reader_is_refused_naming_the_values_there_are(self) -> None:
        reply = asyncio.run(surface_asking_raw_reader("history", "the-voice"))

        assert reply.error is not None
        assert reply.error.code is ErrorCode.MALFORMED_REQUEST
        assert "voice" in reply.error.message

    def test_the_mark_is_carried_and_ignored_on_an_action_that_is_not_a_read(self) -> None:
        """It rides on `history` and `brief`; every other action is already bounded."""
        surface = Surface()
        surface.register()

        marked = surface.ask(Action.STATUS, reader=Reader.VOICE)
        unmarked = surface.ask(Action.STATUS)

        assert marked.data == unmarked.data


async def surface_asking_raw_reader(action: str, reader: str) -> Reply:
    """One line naming a reader, read the way the socket reads it."""
    try:
        request = Request.of({"action": action, "reader": reader})
    except MalformedRequest as refused:
        return Reply.refused(None, refused.code, str(refused))
    return await Surface().plane.handle(request)


class TestTheFocusSession:
    """One pointer, moved by the user replying and by nothing else (#165 Q2)."""

    def test_relaying_into_a_session_makes_it_the_focus(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        assert surface.state.sessions.focus == CODEX

    def test_answering_a_permission_makes_it_the_focus(self) -> None:
        surface = Surface()
        surface.register()
        surface.dialog_on_screen()

        surface.ask(Action.APPROVE, approval_id="a1", verdict="allow")

        assert surface.state.sessions.focus == CODEX

    def test_a_verdict_that_found_nothing_moves_nothing(self) -> None:
        surface = Surface()
        surface.register()

        surface.ask(Action.APPROVE, approval_id="a1", verdict="allow")

        assert surface.state.sessions.focus is None

    def test_briefing_a_session_never_makes_it_the_focus(self) -> None:
        """Asking about a Session is not replying to one."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(SessionInspection(target=CODEX, workspace=WORKSPACE, state=SessionState.IDLE),)
        )

        surface.ask(Action.BRIEF)
        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert surface.state.sessions.focus is None

    def test_the_focus_clears_when_that_session_ends(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()
        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(), now=1.0)

        assert surface.state.sessions.focus is None

    def test_focus_metadata_does_not_remove_a_session_from_the_counts(self) -> None:
        surface = Surface()
        surface.register()
        surface.register(SECOND_CODEX)
        surface.open_window()
        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        roster = surface.ask(Action.BRIEF).data["roster"]

        assert roster["focus"] == CODEX_ADDRESS
        assert roster["rows"][0]["focus"] is True
        assert sum(roster["counts"].values()) == 2


class TestTheMenuScreens:
    """`sessions` and `config` answer with a screen: text, and labels in order (ADR 0021 §6).

    On this surface the labels travel beside the text and are never drawn; the
    text is the screen, with the numbered lines a typed numeral is read against.
    `assistant` is in the set so one parser accepts it, and refused by the hub
    until #265 builds the conversation.
    """

    def test_sessions_is_the_roster_text_with_one_label_per_live_session(self) -> None:
        surface = Surface()
        surface.register()

        data = surface.ask(Action.SESSIONS).data

        assert data["text"] == surface.ask(Action.BRIEF).data["text"]
        assert data["options"] == [str(NAME)]

    def test_sessions_with_nothing_live_has_no_labels(self) -> None:
        data = Surface().ask(Action.SESSIONS).data

        assert data["options"] == []
        assert data["text"].startswith("sessions: none")

    def test_config_offers_switch_verify_and_live(self) -> None:
        data = Surface().ask(Action.CONFIG).data

        assert data == {
            "text": "config\n1. switch\n2. verify\n3. live",
            "options": ["switch", "verify", "live"],
        }

    def test_assistant_answers_with_the_opening_line_and_no_options(self) -> None:
        """ADR 0021 §7: the screen `bridgectl` prints is the one the menu opens."""
        surface = Surface(assistant=True)

        reply = surface.ask(Action.ASSISTANT)

        assert reply.ok
        assert dict(reply.data) == {"text": ASSISTANT_OPENING_LINE, "options": []}
        # A thread is started; no turn is run to produce a fixed line.
        assert surface.call.conversations == ["the rules"]
        assert surface.call.delegated == []

    def test_an_engine_with_no_assistant_refuses_in_the_hubs_own_words(self) -> None:
        reply = Surface().ask(Action.ASSISTANT)

        assert not reply.ok
        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert reply.error.message == ASSISTANT_UNAVAILABLE_HINT


class TestTelegramBinding:
    @pytest.mark.parametrize("outcome", ["success", "cancel", "credentials", "network"])
    def test_binding_releases_the_existing_poll_and_resumes_afterwards(self, outcome: str) -> None:
        import threading

        from gpt_voicecoding.adapters.companion_channel.telegram.api import (
            FailureLayer,
            TelegramBinding,
            TelegramError,
        )
        from test_companion_channel import FakeTelegram, channel, until

        async def scenario() -> None:
            wire = FakeTelegram()
            polling_slot = threading.Lock()
            conflicts: list[str] = []

            def transport(method, payload, *, timeout_seconds):
                if method != "getUpdates":
                    return wire(method, payload, timeout_seconds=timeout_seconds)
                if not polling_slot.acquire(blocking=False):
                    conflicts.append(method)
                    raise TelegramError(FailureLayer.API, "another consumer is polling")
                try:
                    return wire(method, payload, timeout_seconds=timeout_seconds)
                finally:
                    polling_slot.release()

            configured_channel = channel(transport)
            await configured_channel.connect()
            try:
                await until(lambda: polling_slot.locked(), what="existing long poll")
                binding = TelegramBinding(
                    transport_for=lambda token: transport,
                    confirmation="Binding confirmed.",
                    pause=configured_channel.pause_polling,
                    resume=configured_channel.connect,
                )
                plane = ControlPlane(Surface().core, telegram_binding=binding)
                if outcome in {"credentials", "network"}:
                    wire.refuse("getMe", TelegramError(FailureLayer(outcome), "test refusal"))
                begun = await plane.handle(Request(Action.BIND_TELEGRAM, {"token": "same-token"}))
                if outcome in {"credentials", "network"}:
                    assert not begun.ok
                    assert begun.error.code == f"telegram_{outcome}"
                else:
                    assert begun.ok
                    polls = len(wire.method_calls("getUpdates"))
                    # An empty binding poll is the only reader while Start is pending.
                    assert (await plane.handle(Request(Action.BIND_TELEGRAM))).ok
                    assert len(wire.method_calls("getUpdates")) == polls + 1
                    if outcome == "cancel":
                        assert (
                            await plane.handle(Request(Action.BIND_TELEGRAM, {"cancel": True}))
                        ).ok
                        assert not wire.sent()
                    else:
                        wire.deliver(
                            {
                                "update_id": 1,
                                "message": {
                                    "text": "/start",
                                    "chat": {"id": 42, "type": "private"},
                                },
                            }
                        )
                        assert (await plane.handle(Request(Action.BIND_TELEGRAM))).data[
                            "chat_id"
                        ] == "42"
                        assert wire.sent() == ["Binding confirmed."]
                polls = len(wire.method_calls("getUpdates"))
                await until(
                    lambda: len(wire.method_calls("getUpdates")) > polls, what="resumed polling"
                )
                assert not conflicts
            finally:
                await configured_channel.aclose()

        asyncio.run(scenario())

    @pytest.mark.parametrize("chat_type", ["private", "group"])
    def test_binding_while_unbound_confirms_one_start(self, chat_type: str) -> None:
        from gpt_voicecoding.adapters.companion_channel.null import NullCompanionChannel
        from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramBinding
        from test_companion_channel import FakeTelegram

        async def scenario() -> None:
            wire = FakeTelegram()
            surface = Surface()
            core = BridgeCore(
                state=surface.state, call=surface.call, channel=NullCompanionChannel(), agents={}
            )
            binding = TelegramBinding(
                transport_for=lambda token: wire, confirmation="Binding confirmed."
            )
            plane = ControlPlane(core, telegram_binding=binding)
            begun = await plane.handle(Request(Action.BIND_TELEGRAM, {"token": "test-token"}))
            assert begun.ok
            assert begun.data["bot_name"] == "fake_bot"
            assert begun.data["chat_id"] is None
            wire.deliver(
                {
                    "update_id": 1,
                    "message": {"text": "/start", "chat": {"id": 42, "type": chat_type}},
                }
            )
            bound = await plane.handle(Request(Action.BIND_TELEGRAM))
            assert bound.ok
            assert bound.data["chat_id"] == "42"
            assert bound.data["chat_type"] == chat_type
            assert wire.sent() == ["Binding confirmed."]
            assert not (await plane.handle(Request(Action.BIND_TELEGRAM))).ok
            assert wire.sent() == ["Binding confirmed."]

        asyncio.run(scenario())


def test_live_cancel_is_scoped_and_does_not_wait_for_the_dial_response() -> None:
    from gpt_voicecoding.seams.call import CallSnapshot, CallStarted, CallState

    async def run() -> None:
        class RacingCall(FakeCall):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.released = asyncio.Event()
                self.first = True

            async def ensure_call(self, dial):
                if not self.first:
                    return await super().ensure_call(dial)
                self.first = False
                self.entered.set()
                await self.released.wait()
                return CallSnapshot(state=CallState.UP, call_id="cancelled-call")

            async def end_call(self):
                result = await super().end_call()
                self.released.set()
                return result

        call = RacingCall()
        surface = Surface(duty=False, call=call)
        dial = asyncio.create_task(
            surface.plane.handle(Request(action=Action.LIVE, payload={"attempt_id": "first"}))
        )
        await call.entered.wait()
        status = await surface.plane.handle(Request(action=Action.STATUS))
        assert status.data["dial_attempt"] == "first"
        cancelled = await surface.plane.handle(
            Request(action=Action.LIVE, payload={"cancel_dial": "first"})
        )
        assert cancelled.data["cancelled"] is True
        assert (await dial).data["state"] == "down"
        await surface.core.keeper.heard(CallStarted(call_id="cancelled-call"))
        assert surface.core.status().call_id is None
        retry = await surface.plane.handle(
            Request(action=Action.LIVE, payload={"attempt_id": "second"})
        )
        assert retry.data["state"] == "up"
        stale = await surface.plane.handle(
            Request(action=Action.LIVE, payload={"cancel_dial": "first"})
        )
        assert stale.data["cancelled"] is False
        assert surface.core.status().call_id == retry.data["call_id"]
        assert call.calls_ended == 1

    asyncio.run(run())
