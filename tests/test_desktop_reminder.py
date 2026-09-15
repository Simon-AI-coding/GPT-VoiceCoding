"""Desktop reminders observed through the existing Control Plane over a real Hub."""

import asyncio

from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.seams.agent import SessionStopped, WaitingFor, WaitingKind
from gpt_voicecoding.seams.control_plane import Action, Request
from hub import CODEX, Hub


def read_reminder(hub: Hub):
    reply = asyncio.run(ControlPlane(hub.core).handle(Request(action=Action.BRIEF)))
    assert reply.ok
    return reply.data["roster"].get("desktop_reminder")


def test_a_stop_publishes_one_stable_reminder_without_touching_other_outlets():
    hub = Hub(voice=False, message=False)
    assert read_reminder(hub) is None
    hub.emit(SessionStopped(target=CODEX, waiting_for=WaitingFor(kind=WaitingKind.PERMISSION)))
    first = read_reminder(hub)
    assert first is not None
    assert first["row"]["state"] == "permission"
    assert first["row"]["target"]["session_id"] == CODEX.session_id
    assert read_reminder(hub) == first
    hub.emit(SessionStopped(target=CODEX))
    second = read_reminder(hub)
    assert second["id"] != first["id"]
    assert second["row"]["state"] == "finished"
    assert hub.call.calls_started == 0
    assert not hub.channel.sent


def test_duty_and_session_lifetime_clear_the_latest_reminder():
    from gpt_voicecoding.seams.agent import SessionEnded

    hub = Hub(voice=False, message=False)
    hub.emit(SessionStopped(target=CODEX))
    first = read_reminder(hub)
    hub.flip("duty", False)
    hub.emit(SessionStopped(target=CODEX))
    assert read_reminder(hub) is None
    hub.flip("duty", True)
    assert read_reminder(hub) is None
    hub.emit(SessionStopped(target=CODEX))
    assert read_reminder(hub)["id"] != first["id"]
    hub.emit(SessionEnded(target=CODEX))
    assert read_reminder(hub) is None


def test_outlet_changes_and_roster_reads_do_not_create_new_reminders():
    hub = Hub(voice=False, message=False)
    hub.emit(SessionStopped(target=CODEX))
    reminder = read_reminder(hub)
    hub.flip("message", True)
    hub.flip("message", False)
    assert read_reminder(hub) == reminder
    other = Hub(voice=False, message=False)
    other.emit(SessionStopped(target=CODEX))
    assert read_reminder(other)["id"] != reminder["id"]


def test_a_stop_without_a_user_facing_session_never_creates_a_reminder():
    hub = Hub(voice=False, message=False, sessions=())
    hub.emit(SessionStopped(target=CODEX, has_controlling_terminal=False))
    assert read_reminder(hub) is None


def test_new_stops_replace_the_snapshot_but_discovery_activity_does_not():
    from datetime import UTC, datetime
    from pathlib import Path

    from gpt_voicecoding.seams.agent import (
        LaneDiscovery,
        ProgressEntry,
        ProgressObservation,
        ProgressRole,
        SessionInspection,
        SessionState,
    )

    def progress(text):
        return ProgressObservation.readable(
            has_history=True,
            read_at=datetime(2026, 9, 15, tzinfo=UTC),
            recent=(ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=text),),
        )

    hub = Hub(voice=False, message=False)
    hub.emit(
        SessionStopped(
            target=CODEX,
            progress=progress("Which direction?"),
            waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which direction?"),
        )
    )
    decision = read_reminder(hub)
    assert decision["row"]["state"] == "decision"
    hub.emit(SessionStopped(target=CODEX, progress=progress("First completion.")))
    hub.emit(SessionStopped(target=CODEX, progress=progress("Latest completion.")))
    latest = read_reminder(hub)
    assert latest["id"] != decision["id"]
    assert latest["row"]["newest"] == "Latest completion."
    # The real discovery path can replace state, progress and activity between reads.
    hub.agent.discovery = LaneDiscovery(
        rows=(
            SessionInspection(
                target=CODEX,
                workspace=Path("/tmp/workspace"),
                state=SessionState.RUNNING,
                progress=progress("Ordinary working progress."),
                last_activity=datetime(2026, 9, 16, tzinfo=UTC),
            ),
        )
    )
    asyncio.run(hub.core.discover())
    assert read_reminder(hub) == latest


def test_a_child_stop_is_not_a_desktop_reminder():
    from pathlib import Path

    from gpt_voicecoding.seams.agent import (
        ChildClassification,
        ChildKind,
        LaneDiscovery,
        SessionInspection,
    )

    hub = Hub(voice=False, message=False, sessions=())
    hub.agent.discovery = LaneDiscovery(
        rows=(
            SessionInspection(
                target=CODEX,
                workspace=Path("/tmp/workspace"),
                child=ChildClassification(kind=ChildKind.CHILD),
            ),
        )
    )
    asyncio.run(hub.core.discover())
    hub.emit(SessionStopped(target=CODEX))
    assert read_reminder(hub) is None


def test_ending_clears_before_a_target_can_reappear_between_desktop_reads():
    from pathlib import Path

    from gpt_voicecoding.seams.agent import LaneDiscovery, SessionEnded, SessionInspection

    for reported_by_event in (True, False):
        hub = Hub(voice=False, message=False)
        hub.emit(SessionStopped(target=CODEX))
        assert read_reminder(hub) is not None
        if reported_by_event:
            hub.emit(SessionEnded(target=CODEX))
        else:
            asyncio.run(hub.core.discover())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                ),
            )
        )
        asyncio.run(hub.core.discover())
        assert read_reminder(hub) is None
