"""Where the two sources of "what it stopped on" meet, and who wins (#75).

The parser is pure and tested against fragments in `test_claude_stop_analysis.py`.
This is the other half: the adapter reads a Session's own transcript, ranks what
it found against any dialog parked on this engine's approval socket, and puts the
answer on both paths Bridge Core actually reads — the roster row it renders in
`sessions`, and the `SessionStopped` it renders the Stop Notice from.

The ranking is a table because the two sources can disagree, and the four rows
are the only judgment #75 has outside the parser.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from claude_adapter_fake import ParkedApproval, claude_waiting_roster
from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import adapter as claude_adapter
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter, SessionReport
from gpt_voicecoding.adapters.agent.claude.registry import (
    PEER_PROTOCOL,
    STATUS_IDLE_WITH_BACKGROUND,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.transcript import TranscriptReader
from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher, StopReading
from gpt_voicecoding.seams.agent import (
    SANDBOX_TOOL_NAME,
    AgentEvent,
    ApprovalRequest,
    LaneDiscovery,
    LaneUnavailable,
    Option,
    ProgressCapture,
    ProgressObservation,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionInspection,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from test_claude_reply_window import Child, say
from test_claude_stop_analysis import asked, called, said, turn

SESSION = "d3a776ae-3b60-437d-bc70-ba57a2b280c6"
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=3538)

#: What the roster alone says about a Session it calls `waiting`: something is
#: being waited on and the command does not carry what (`discovery.py`).
ROSTER_WAITING = WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)

#: The transcript tail of a Session that started a background command and has
#: not been told it finished: the `tool_result` naming the id, and nothing since.
#: One shape, read by both paths that answer for such a Session — the Stop that
#: reports the turn ended, and the roster row the cadence projects — because the
#: whole of #325 is those two paths having answered differently about it.
BACKGROUND_COMMAND_RUNNING: list[dict[str, Any]] = [
    said("Kicked off the review."),
    {
        "type": "user",
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t9",
                    "content": "Command running in background with ID: brv1.",
                }
            ],
        },
        "toolUseResult": {"backgroundTaskId": "brv1"},
    },
]


def transcript(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def adapter_holding(
    transcript_path: Path | None = None,
    *,
    parked: tuple[ApprovalRequest, ...] = (),
    asking: WaitingFor | None = None,
    progress_capture: ProgressCapture = PROGRESS_CAPTURE,
) -> ClaudeAgentAdapter:
    """An adapter that has heard one Session's registration and holds `parked`.

    The registration is seeded rather than driven through the hook, because what
    is under test is the ranking and not the `SessionStart` wire — which
    `test_claude_registration.py` owns. `pending()` is stubbed for the same
    reason: parking a real dialog needs a real hook process on a real socket, and
    `test_claude_approval.py` already proves that half.
    """
    adapter = ClaudeAgentAdapter(progress_capture=progress_capture)
    adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
        session_id=SESSION, pid=TARGET.pid, transcript_path=transcript_path
    )
    for index, request in enumerate(parked):
        # Parked the way a hook parks one, minus the socket: `newest_for` reads
        # this dict, and `test_claude_approval.py` owns proving a real hook gets
        # a request into it.
        adapter._approvals._waiting[request.approval_id] = ParkedApproval(  # noqa: SLF001
            request, asking
        )
        del index
    return adapter


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch):
    """Make `claude agents --json` answer with exactly this lane, for one test.

    The command itself is `discovery.py`'s and is tested there; what is under
    test here is what the adapter adds to the rows it comes back with.
    """

    def answering(lane: LaneDiscovery) -> None:
        async def stub(**_asked: object) -> LaneDiscovery:
            return lane

        monkeypatch.setattr(claude_adapter.claude_discovery, "discover", stub)

    return answering


def dialog(tool_name: str = "Bash", detail: str = "push the branch") -> ApprovalRequest:
    """One `PermissionRequest` hook's dialog, as `approval.request_from` builds it."""
    return ApprovalRequest(
        approval_id="a-1",
        target=TARGET,
        tool_name=tool_name,
        detail=detail,
    )


def parallel(*calls: tuple[str, str, Any]) -> dict[str, Any]:
    """One assistant record holding several `tool_use` blocks, as parallel calls arrive.

    `called` writes one block, which is the shape the ranking cases used until a
    dialog had to be matched against the right one of several.
    """
    return {
        "type": "assistant",
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": identifier, "name": tool, "input": tool_input}
                for tool, identifier, tool_input in calls
            ],
        },
    }


class TestAParkedQuestionWinsOutright:
    """The fifth row, and the top of the table (#77 B).

    `AskUserQuestion` raises the same `PermissionRequest` hook a `Write` does
    (measured on 2.1.246), so a question is parked on this engine's approval
    socket with the whole prompt and its options — while the transcript says
    nothing about it until the tool call has flushed, by which time the person at
    the keyboard has usually answered it. Claude 2.1.248 supplies no usable
    `prompt_id` for this request, so the listener may use a private correlator.
    The hook's question is the thing itself; the transcript's is a reconstruction
    of it. So the hook wins whether the two name the same prompt or different ones.
    """

    @staticmethod
    def asking(prompt: str = "Tabs or spaces?", *labels: str) -> WaitingFor:
        return WaitingFor(
            kind=WaitingKind.QUESTION,
            prompt=prompt,
            options=tuple(Option(text=label) for label in labels or ("Spaces", "Tabs")),
            approval_id="7333021c-1ab7-451d-9eee-91617bc4838d",
        )

    def test_it_wins_over_a_record_that_has_not_flushed_the_call(self, tmp_path: Path) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [*turn()]),
            parked=(dialog(tool_name="AskUserQuestion", detail=""),),
            asking=self.asking(),
        )

        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for

        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.prompt == "Tabs or spaces?"
        assert [option.text for option in waiting.options] == ["Spaces", "Tabs"]
        assert waiting.caught_up is True, "the hook payload is the record it read"

    def test_it_wins_over_the_records_reconstruction_of_the_same_prompt(
        self, tmp_path: Path
    ) -> None:
        """Same question, two readings. The parked one is what is on the screen."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Tabs or spaces?", ["Spaces", "Tabs"]))]),
            parked=(dialog(tool_name="AskUserQuestion", detail=""),),
            asking=self.asking(),
        )

        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for

        assert waiting.approval_id == "7333021c-1ab7-451d-9eee-91617bc4838d"

    def test_it_wins_over_a_different_question_the_record_is_still_holding(
        self, tmp_path: Path
    ) -> None:
        """Different prompts: the hook still wins, because it is what is parked."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Which base?", ["main", "feature"]))]),
            parked=(dialog(tool_name="AskUserQuestion", detail=""),),
            asking=self.asking(),
        )

        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for

        assert waiting.prompt == "Tabs or spaces?"

    def test_an_older_question_does_not_anchor_the_current_questions_progress(
        self, tmp_path: Path
    ) -> None:
        adapter = adapter_holding(
            transcript(
                tmp_path,
                [
                    said("Choose the first base.", role="user"),
                    said("The first decision should use main."),
                    asked("q1", ("Which base?", ["main", "feature"])),
                    said("main", role="user"),
                    said("The second decision needs a fresh answer."),
                ],
            ),
            parked=(dialog(tool_name="AskUserQuestion", detail=""),),
            asking=self.asking("Tabs or spaces?", "Spaces", "Tabs"),
        )

        reading = adapter.stop_reading(TARGET, ROSTER_WAITING)

        assert reading.waiting_for.prompt == "Tabs or spaces?"
        assert reading.progress.recent[-1].text == "The second decision needs a fresh answer."

    def test_the_question_never_projects_into_the_permission_relay(self, tmp_path: Path) -> None:
        """Question words ride Answer Relay; Approval Relay remains permissions only."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn()]),
            parked=(dialog(tool_name="AskUserQuestion", detail=""),),
            asking=self.asking(),
        )

        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for

        assert waiting.as_approval_request(TARGET) is None

    def test_a_parked_permission_is_ranked_as_it_always_was(self, tmp_path: Path) -> None:
        """The control: `asking` is what distinguishes the two, not the tool name."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Edit", "e1", {"file_path": "/tmp/notes.md"})]),
            parked=(dialog(tool_name="Edit", detail="notes.md"),),
        )

        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for

        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.approval_id == "a-1"


class TestTheRanking:
    """Four rows. The transcript and the parked dialog are ranked, never merged."""

    def test_a_readable_question_wins_outright(self, tmp_path: Path) -> None:
        """A dialog held open by a hook never overrides a decision the user was asked.

        The reference implementation's precedence
        (`legacy@1d32845:bridge/transcript.py:1691-1692`), carried across the
        second source v2 has and legacy did not.
        """
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Which base?", ["main", "feature"]))]),
            parked=(dialog(),),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.approval_id is None
        assert waiting.tool_name is None

    def test_a_permission_read_from_the_record_keeps_its_fields_and_gains_the_handle(
        self, tmp_path: Path
    ) -> None:
        """The handle is the one thing a transcript can never carry.

        The dialog here names the same call, which is the ordinary case, and
        while it does the record's own reading stands: it read the call's input
        and the hook payload carries a summary built from a subset of it. This
        parked a dialog naming a *different* tool until #98 — the record won
        then, and that was the defect, because the `approval_id` beside it was
        always the dialog's.
        """
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Edit", "e1", {"file_path": "/tmp/notes.md"})]),
            parked=(dialog(tool_name="Edit", detail="notes.md"),),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.PERMISSION
        # The record's own reading is not overwritten by the dialog's.
        assert waiting.tool_name == "Edit"
        assert waiting.detail == "/tmp/notes.md"
        assert waiting.approval_id == "a-1"
        assert waiting.caught_up is True

    def test_the_dialog_names_the_call_the_verdict_will_reach(self, tmp_path: Path) -> None:
        """Parallel calls in one message: the record's pick and the dialog can differ.

        **The hook payload is the authority on what is parked** (advisor,
        2026-08-26, #98). The transcript is held up on the newest outstanding
        call, which for several calls written in one message is the last-listed
        one; the dialog is parked on whichever the far side actually stopped to
        ask about. Announcing the record's pick beside the dialog's
        `approval_id` would name one tool and send the user's verdict to
        another — an Approval carrying their authority to a call they were never
        shown.
        """
        adapter = adapter_holding(
            transcript(
                tmp_path,
                [
                    *turn(),
                    parallel(
                        ("Read", "r1", {"file_path": "/tmp/first"}),
                        ("Bash", "b1", {"description": "push the branch"}),
                    ),
                ],
            ),
            parked=(dialog(tool_name="Read", detail="/tmp/first"),),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "Read"
        # The record's `detail` describes the record's call, so it goes with it.
        assert waiting.detail == "/tmp/first"
        assert waiting.approval_id == "a-1"

    def test_a_record_that_named_no_tool_does_not_lend_the_dialog_its_summary(
        self, tmp_path: Path
    ) -> None:
        """Naming no tool is not agreeing with the dialog about which call it is.

        A `tool_use` whose `name` this reader could not read is still summarised
        from its input, and that summary describes the call the *record* was held
        up on. Beside a dialog naming a tool, the two are not known to be one
        call, so the announcement takes the dialog's own words whole.
        """
        unnamed = called("Bash", "x1", {"description": "delete the release tag"})
        del unnamed["message"]["content"][0]["name"]
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), unnamed]),
            parked=(dialog(tool_name="Bash", detail="push the branch"),),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"
        assert waiting.approval_id == "a-1"

    def test_a_call_the_record_could_not_describe_takes_the_dialog_s_words(
        self, tmp_path: Path
    ) -> None:
        """Filling a gap is not overwriting: the parser said nothing on this field."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"prompt": "unreadable"})]),
            parked=(dialog(tool_name="Bash", detail="push the branch"),),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"

    def test_a_dialog_the_record_has_not_caught_up_with_is_still_a_stop(
        self, tmp_path: Path
    ) -> None:
        """The first-turn case, **adapted** from the Notification sentence.

        `PermissionRequest` fires when the dialog opens, before the `tool_use`
        record is flushed. Legacy scraped the tool name out of English
        (`legacy@1d32845:bridge/daemon.py:143-145,2049-2051`); the hook carries
        the same fact plus a handle, from the process holding the dialog.
        """
        adapter = adapter_holding(transcript(tmp_path, turn()), parked=(dialog(),))
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"
        assert waiting.approval_id == "a-1"
        # `caught_up=True` is what the seam's own invariant forces and what it
        # means: the reader read the record that says so, and that record is the
        # hook payload rather than the transcript (`seams/agent.py:180-186`).
        assert waiting.caught_up is True

    def test_a_dialog_on_a_session_the_roster_calls_finished_is_still_a_stop(
        self, tmp_path: Path
    ) -> None:
        """A dialog on screen is a stop whatever the roster and the record say."""
        adapter = adapter_holding(transcript(tmp_path, turn()), parked=(dialog(),))
        waiting = adapter.stop_reading(TARGET, WaitingFor()).waiting_for
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.approval_id == "a-1"

    def test_with_no_dialog_the_parser_s_answer_stands_untouched(self, tmp_path: Path) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.approval_id is None

    def test_the_newest_of_two_dialogs_is_the_one_it_is_held_up_on(self, tmp_path: Path) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, turn()),
            parked=(
                dialog(tool_name="Read", detail="/tmp/first"),
                replace(dialog(tool_name="Edit", detail="/tmp/second"), approval_id="a-2"),
            ),
        )
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.tool_name == "Edit"
        assert waiting.approval_id == "a-2"

    def test_another_session_s_dialog_is_not_this_session_s_stop(self, tmp_path: Path) -> None:
        """`--resume` forks two processes under one session id, with two dialogs."""
        other = replace(TARGET, pid=9999)
        adapter = adapter_holding(
            transcript(tmp_path, turn()), parked=(replace(dialog(), target=other),)
        )
        assert adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for == ROSTER_WAITING


class TestWhenTheRecordSaysNothing:
    """The roster's own word stands, and `NONE` is not `UNKNOWN`."""

    def test_a_finished_turn_stays_a_finished_turn(self, tmp_path: Path) -> None:
        adapter = adapter_holding(transcript(tmp_path, turn()))
        assert adapter.stop_reading(TARGET, WaitingFor()).waiting_for.kind is WaitingKind.NONE

    def test_a_waiting_session_whose_record_says_nothing_is_asked_again(
        self, tmp_path: Path
    ) -> None:
        """`UNKNOWN` with `caught_up=False` is the seam's word for *ask again*."""
        adapter = adapter_holding(transcript(tmp_path, turn()))
        waiting = adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for
        assert waiting.kind is WaitingKind.UNKNOWN
        assert waiting.caught_up is False

    def test_a_session_that_never_registered_leaves_the_roster_s_word_alone(self) -> None:
        """No `SessionStart` hook ran, so there is no path and nothing was read."""
        adapter = ClaudeAgentAdapter(progress_capture=PROGRESS_CAPTURE)
        assert adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for == ROSTER_WAITING

    def test_a_transcript_that_does_not_exist_yet_is_not_an_empty_one(self, tmp_path: Path) -> None:
        """A Session's first turn creates the file (#73); before that, nobody looked."""
        adapter = adapter_holding(tmp_path / "never-written.jsonl")
        assert adapter.stop_reading(TARGET, ROSTER_WAITING).waiting_for == ROSTER_WAITING


class TestTheRosterRow:
    """`discover` is the verb Bridge Core calls, so it is where this lands."""

    def row(self, state: SessionState, waiting: WaitingFor) -> SessionInspection:
        return SessionInspection(
            target=TARGET, workspace=Path("/tmp/workspace"), state=state, waiting_for=waiting
        )

    def test_a_stopped_row_says_what_it_stopped_on(self, tmp_path: Path, roster) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Which base?", ["main", "feature"]))])
        )
        roster(LaneDiscovery(rows=(self.row(SessionState.WAITING, ROSTER_WAITING),)))
        lane = asyncio.run(adapter.discover())
        assert lane.rows[0].waiting_for.kind is WaitingKind.QUESTION
        assert [option.text for option in lane.rows[0].waiting_for.options] == [
            "main",
            "feature",
        ]

    def test_a_session_mid_turn_is_not_read_at_all(self, tmp_path: Path, roster) -> None:
        """A Session that is working is not stopped on anything, so no file is opened.

        This is what keeps the five-second cadence off the hot path: on a machine
        of busy Sessions it costs one roster command and no reads.
        """
        path = transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        adapter = adapter_holding(path)
        opened: list[Path | None] = []
        original = adapter._transcripts.records  # noqa: SLF001

        def watched(argument: Path | None) -> Any:
            opened.append(argument)
            return original(argument)

        adapter._transcripts.records = watched  # type: ignore[method-assign]  # noqa: SLF001
        roster(LaneDiscovery(rows=(self.row(SessionState.RUNNING, WaitingFor()),)))
        lane = asyncio.run(adapter.discover())
        assert opened == []
        assert lane.rows[0].waiting_for.kind is WaitingKind.NONE

    def test_a_lane_that_could_not_look_is_passed_through_whole(self, roster) -> None:
        """A failed enumeration has no rows to read, and must not gain any."""
        adapter = adapter_holding()
        roster(LaneDiscovery(error="`claude` is not on the PATH"))
        lane = asyncio.run(adapter.discover())
        assert lane.error == "`claude` is not on the PATH"
        assert lane.rows == ()

    def test_inspect_answers_from_the_same_rows(self, tmp_path: Path, roster) -> None:
        """One reader, one shape — `inspect` reads what `discover` produced."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        )
        roster(LaneDiscovery(rows=(self.row(SessionState.WAITING, ROSTER_WAITING),)))
        found = asyncio.run(adapter.inspect(TARGET))
        assert found.waiting_for.kind is WaitingKind.PERMISSION
        assert found.waiting_for.detail == "push"

    def test_a_lane_that_could_not_look_still_raises_from_inspect(self, roster) -> None:
        """#74's rule survives the overlay: `LaneUnavailable` is not `UNKNOWN`."""
        adapter = adapter_holding()
        roster(LaneDiscovery(error="`claude` is not on the PATH"))
        with pytest.raises(LaneUnavailable):
            asyncio.run(adapter.inspect(TARGET))


class TestTheRosterAndReplyWindowAgree:
    """The two readers act on one `waitingFor` classification (#155)."""

    def test_a_sandbox_request_reaches_both_paths_as_the_same_wait(
        self, tmp_path: Path, roster
    ) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        target = replace(TARGET, pid=os.getpid())
        say(tmp_path, "busy", pid=target.pid, session_id=SESSION)
        raised: list[AgentEvent] = []
        settings = ClaudeSettings(registry_directory=sessions)
        adapter = ClaudeAgentAdapter(progress_capture=PROGRESS_CAPTURE, settings=settings)
        adapter._reported[target] = SessionReport(  # noqa: SLF001 - registration fact
            session_id=SESSION,
            pid=target.pid,
            transcript_path=transcript(tmp_path, turn()),
        )
        watcher = ReplyWindowWatcher(
            settings=settings,
            registry_directory=sessions,
            emit=raised.append,
            stopped_on=adapter.stop_reading,
        )
        watcher.watch(target)

        say(
            tmp_path,
            "waiting",
            pid=target.pid,
            session_id=SESSION,
            waiting_for="sandbox request",
        )
        watcher.poll_once()
        swept = next(event for event in raised if isinstance(event, SessionStopped))
        roster(claude_waiting_roster(target, "sandbox request"))
        projected = asyncio.run(adapter.discover()).rows[0]

        assert projected.waiting_for == swept.waiting_for
        assert projected.waiting_for.tool_name == SANDBOX_TOOL_NAME

    def test_a_named_roster_base_does_not_displace_a_parked_dialog_handle(
        self, tmp_path: Path, roster
    ) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, turn()),
            parked=(dialog(tool_name="Bash", detail="push the branch"),),
        )
        roster(claude_waiting_roster(TARGET, "sandbox request"))

        waiting = asyncio.run(adapter.discover()).rows[0].waiting_for

        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "Bash"
        assert waiting.approval_id == "a-1"


class TestTheStopNotice:
    """The other path: the event Bridge Core renders the Stop Notice from."""

    def test_a_question_stop_carries_the_leading_text_and_option_descriptions(
        self, tmp_path: Path
    ) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        say(tmp_path, "busy", pid=os.getpid(), session_id=SESSION)
        raised: list[AgentEvent] = []

        class Sink:
            def emit(self, event: AgentEvent) -> None:
                raised.append(event)

        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            sink=Sink(),
            settings=ClaudeSettings(registry_directory=sessions),
        )
        question = called(
            "AskUserQuestion",
            "q1",
            {
                "questions": [
                    {
                        "question": "Which base?",
                        "options": [
                            {
                                "label": "main",
                                "description": "Merge into the default branch",
                            },
                            {
                                "label": "feature",
                                "description": "Keep the work isolated",
                            },
                        ],
                    }
                ]
            },
        )
        path = transcript(
            tmp_path,
            [
                said("Please inspect the merge.", role="user"),
                said("The default branch is the safest choice."),
                question,
            ],
        )
        target = replace(TARGET, pid=os.getpid())
        adapter._reported[target] = SessionReport(  # noqa: SLF001 - registration fact
            session_id=SESSION,
            pid=os.getpid(),
            transcript_path=path,
        )
        adapter.register_session(target, tmp_path / "channel.sock")

        say(
            tmp_path,
            "waiting",
            pid=os.getpid(),
            session_id=SESSION,
            waiting_for="input needed",
        )
        adapter._windows.poll_once()  # noqa: SLF001 - one deterministic sweep

        stopped = next(event for event in raised if isinstance(event, SessionStopped))
        assert stopped.progress.recent[-1].text == "The default branch is the safest choice."
        assert [option.description for option in stopped.waiting_for.options] == [
            "Merge into the default branch",
            "Keep the work isolated",
        ]

    def test_a_stop_carries_what_it_stopped_on(self, tmp_path: Path) -> None:
        """`SessionStopped.waiting_for` replaces the free-text `detail` (#74, #75)."""
        from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
        from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher

        raised: list[Any] = []
        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(),
            registry_directory=tmp_path / "sessions",
            emit=raised.append,
            stopped_on=lambda target, roster=None: StopReading(
                waiting_for=WaitingFor(
                    kind=WaitingKind.PERMISSION,
                    tool_name="Bash",
                    detail="push the branch",
                ),
                progress=ProgressObservation(),
            ),
        )
        assert watcher._read_stop(TARGET).waiting_for.tool_name == "Bash"  # noqa: SLF001

    def test_a_reader_that_raises_costs_the_words_and_never_the_notice(
        self, tmp_path: Path
    ) -> None:
        """A Stop is already proven at that point; silence would be the worse loss."""
        from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
        from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher

        def raising(target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
            raise RuntimeError("the transcript reader is broken")

        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(),
            registry_directory=tmp_path / "sessions",
            emit=lambda event: None,
            stopped_on=raising,
        )
        assert watcher._read_stop(TARGET).waiting_for == WaitingFor()  # noqa: SLF001


class TestReadingTheFileOnce:
    """The lane's one opener of a transcript, shared with #76."""

    def test_it_parses_once_until_the_file_changes(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        first = reader.records(path)
        assert first is not None
        assert reader.records(path) is first

    def test_a_grown_transcript_is_read_again(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        before = reader.records(path)
        assert before is not None
        with path.open("a", encoding="utf-8") as growing:
            growing.write(json.dumps(called("Bash", "b1", {"description": "push"})) + "\n")
        after = reader.records(path)
        assert after is not None
        assert len(after) == len(before) + 1

    def test_a_half_written_last_line_costs_itself_and_nothing_else(self, tmp_path: Path) -> None:
        """The record being appended right now, which is the ordinary case."""
        path = transcript(tmp_path, turn())
        with path.open("a", encoding="utf-8") as growing:
            growing.write('{"type": "assistant", "mess')
        records = TranscriptReader().records(path)
        assert records is not None
        assert len(records) == 2

    @pytest.mark.parametrize("line", ["not json at all", "[1, 2, 3]", '"a string"', "null", "   "])
    def test_a_line_that_is_not_a_record_is_skipped(self, tmp_path: Path, line: str) -> None:
        path = transcript(tmp_path, turn())
        with path.open("a", encoding="utf-8") as growing:
            growing.write(line + "\n")
        records = TranscriptReader().records(path)
        assert records is not None
        assert len(records) == 2

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
    def test_a_unicode_line_separator_inside_a_record_does_not_split_it(
        self, tmp_path: Path, separator: str
    ) -> None:
        """JSONL is `\n`-delimited, and `str.splitlines` is not.

        `JSON.stringify` leaves U+2028, U+2029 and U+0085 raw inside a string, so
        they reach the file as themselves — which is why this case writes its
        records with `ensure_ascii=False` rather than through `transcript`.
        Splitting on them cuts a record in two, neither half parses, and the
        record is dropped whole. Measured on 2026-08-26 over the 200 most recent
        transcripts on this machine: 4 files losing 31 records between them
        (1, 2, 13 and 15). A dropped `tool_result` leaves the call it closes
        outstanding, which reads as a permanent false `PERMISSION`.
        """
        path = tmp_path / "session.jsonl"
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in [*turn(), said(f"before{separator}after")]
            ),
            encoding="utf-8",
        )
        records = TranscriptReader().records(path)
        assert records is not None
        assert len(records) == 3
        assert records[2]["message"]["content"][0]["text"] == f"before{separator}after"

    def test_no_path_and_no_file_are_both_none_rather_than_empty(self, tmp_path: Path) -> None:
        reader = TranscriptReader()
        assert reader.records(None) is None
        assert reader.records(tmp_path / "absent.jsonl") is None

    def test_a_file_that_vanishes_drops_its_cache(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        assert reader.records(path) is not None
        path.unlink()
        assert reader.records(path) is None

    def test_forgetting_one_session_leaves_the_others(self, tmp_path: Path) -> None:
        one = transcript(tmp_path, turn())
        other = tmp_path / "other.jsonl"
        other.write_text(json.dumps(turn()[0]) + "\n", encoding="utf-8")
        reader = TranscriptReader()
        kept = reader.records(other)
        reader.records(one)
        reader.forget(one)
        reader.forget(None)
        assert reader.records(other) is kept


class TestWhenTheSessionEnds:
    """What the adapter lets go of when it reports a Session ended (#98).

    `forget_session` existed and nothing called it: it is not on the
    `AgentAdapter` seam, and Bridge Core's `_session_ended` only marks state. On
    an engine that starts at login, that made every Session that ever registered
    keep its parsed transcript for the life of the process — records of files
    measured at 186 MB on this machine.

    The fix is neither a timer nor a seam method: the adapter that emits
    `SessionEnded` is the one that knows, and the cache is its own. So these
    drive the real sweep and assert on what is left behind.
    """

    def watching(
        self,
        tmp_path: Path,
        pid: int,
        transcript_records: list[dict[str, Any]] | None = None,
    ) -> tuple[ClaudeAgentAdapter, SessionTarget, Path, list[Any]]:
        """An adapter registered on that pid, with the transcript already parsed.

        The registry stands in for `~/.claude/sessions` the way
        `test_claude_reply_window` writes it, so the sweep below is the real one
        rather than a stub of it.
        """
        (tmp_path / "sessions").mkdir()
        say(tmp_path, "busy", pid=pid, session_id=SESSION)
        target = replace(TARGET, pid=pid)
        raised: list[Any] = []

        class Sink:
            def emit(self, event: AgentEvent) -> None:
                raised.append(event)

        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            sink=Sink(),
            settings=ClaudeSettings(
                registry_directory=tmp_path / "sessions", reply_window_poll_seconds=0.02
            ),
        )
        records = (
            [*turn(), called("Bash", "b1", {"description": "push"})]
            if transcript_records is None
            else transcript_records
        )
        path = transcript(tmp_path, records)
        adapter._reported[target] = SessionReport(  # noqa: SLF001 - seeding one registration
            session_id=SESSION, pid=pid, transcript_path=path
        )
        adapter.register_session(target, tmp_path / "channel.sock")
        adapter.stop_reading(target)  # warms the cache before the transition
        return adapter, target, path, raised

    def test_a_death_reported_is_a_session_forgotten(self, tmp_path: Path) -> None:
        corpse = Child()
        corpse.kill()
        adapter, target, path, raised = self.watching(tmp_path, corpse.pid)

        adapter._windows.poll_once()  # noqa: SLF001 - one sweep, without the timer

        assert [type(event) for event in raised] == [SessionEnded]
        assert adapter.reported(target) is None
        assert adapter.reachable() == ()
        # The parsed records go with it, which is the megabytes this is about.
        assert path not in adapter._transcripts._cache  # noqa: SLF001 - the leak itself

    def test_a_session_that_is_still_alive_keeps_everything(self, tmp_path: Path) -> None:
        """The sweep runs on every watched Session; only a death may forget one.

        The pid is this test's own process, which is alive by definition. Its
        record finishes its turn between registration and the sweep, so the
        sweep has something to report — which is what proves it looked at this
        Session at all and still buried nothing.
        """
        adapter, target, path, raised = self.watching(tmp_path, os.getpid())
        say(tmp_path, "idle", pid=os.getpid(), session_id=SESSION)

        adapter._windows.poll_once()  # noqa: SLF001

        assert [type(event) for event in raised] == [ReplyWindowChanged, SessionStopped]
        stopped = next(event for event in raised if isinstance(event, SessionStopped))
        assert [entry.text for entry in stopped.progress.recent if entry.role == "assistant"] == [
            "done"
        ]
        assert adapter.reported(target) is not None
        assert adapter.reachable() == (target,)
        assert path in adapter._transcripts._cache  # noqa: SLF001

    def test_shell_and_idle_stops_carry_equal_observations(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`shell` changes background activity, not what the Session last said (#151)."""
        read_at = claude_adapter.datetime(2026, 8, 31, 5, 8, 36, tzinfo=claude_adapter.UTC)

        class FixedDateTime:
            @staticmethod
            def now(_timezone: object) -> object:
                return read_at

        monkeypatch.setattr(claude_adapter, "datetime", FixedDateTime)

        def stop_at(status: str) -> object:
            root = tmp_path / status
            root.mkdir()
            adapter, target, _path, raised = self.watching(root, os.getpid())
            say(root, status, pid=os.getpid(), session_id=SESSION)
            adapter._windows.poll_once()  # noqa: SLF001
            return next(event.progress for event in raised if isinstance(event, SessionStopped))

        assert stop_at("shell") == stop_at("idle")

    def test_an_empty_transcript_is_readable_history_with_nothing_said(
        self, tmp_path: Path
    ) -> None:
        adapter, _target, _path, raised = self.watching(
            tmp_path,
            os.getpid(),
            transcript_records=[],
        )
        say(tmp_path, "idle", pid=os.getpid(), session_id=SESSION)

        adapter._windows.poll_once()  # noqa: SLF001

        stopped = next(event for event in raised if isinstance(event, SessionStopped))
        assert stopped.progress.has_history is False
        assert stopped.progress.recent == ()


class TestWaitingOnAnotherSession:
    """#320: the lane carries a raw recipient; this side resolves it or drops it.

    `stop_analysis` reads no registry, so what it hands over is the string the
    Session typed into `SendMessage`. Turning that into an address of a Session
    this engine actually knows is the adapter's, and a recipient that names none
    is not this state at all — the announcement says `finished` instead, which
    tells the user less and never tells them something false.
    """

    #: This process, because the record has to name one that is actually alive:
    #: a registry file outlives the Session that wrote it, and a recipient with
    #: nobody behind it names nobody.
    PEER_PID = os.getpid()
    PEER_SESSION = "4d3e79c8-b919-4116-a10c-f7a42a76360a"
    PEER_NAME = "gpt-voicecoding-32"

    def registry_naming(self, tmp_path: Path, *, pid: int | None = None) -> Path:
        """A registry holding one other Session, as Claude Code writes one."""
        pid = pid if pid is not None else self.PEER_PID
        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": self.PEER_SESSION,
                    "cwd": "/a/workspace",
                    "version": "2.1.266",
                    "peerProtocol": PEER_PROTOCOL,
                    "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
                    "status": "busy",
                    "name": self.PEER_NAME,
                    "nameSource": "derived",
                }
            ),
            encoding="utf-8",
        )
        return sessions

    def reading(self, tmp_path: Path, recipient: str, *, pid: int | None = None) -> WaitingFor:
        sessions = self.registry_naming(tmp_path, pid=pid)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            settings=ClaudeSettings(registry_directory=sessions),
        )
        adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
            session_id=SESSION,
            pid=TARGET.pid,
            transcript_path=transcript(
                tmp_path,
                [
                    said("Escalating."),
                    called("SendMessage", "t1", {"to": recipient, "message": "CREW ASK 320"}),
                    {
                        "type": "user",
                        "isSidechain": False,
                        "userType": "external",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                            ],
                        },
                        "toolUseResult": {"success": True, "msg_id": "m-1"},
                    },
                ],
            ),
        )
        return adapter.stop_reading(TARGET).waiting_for

    def test_a_recipient_the_registry_names_becomes_that_sessions_address(
        self, tmp_path: Path
    ) -> None:
        found = self.reading(tmp_path, self.PEER_NAME)

        assert found.kind is WaitingKind.PEER
        assert found.awaiting == str(
            SessionTarget(agent=AgentKind.CLAUDE, session_id=self.PEER_SESSION, pid=self.PEER_PID)
        )

    def test_a_recipient_addressed_by_its_socket_resolves_to_the_same_session(
        self, tmp_path: Path
    ) -> None:
        """A `to` carries either spelling, and one record holds both."""
        found = self.reading(tmp_path, f"uds:/tmp/cc-socks/{self.PEER_PID}.sock")

        assert found.kind is WaitingKind.PEER
        assert found.awaiting == str(
            SessionTarget(agent=AgentKind.CLAUDE, session_id=self.PEER_SESSION, pid=self.PEER_PID)
        )

    def test_a_recipient_this_engine_knows_nothing_about_is_not_this_state(
        self, tmp_path: Path
    ) -> None:
        """The ticket's own edge case: not 🟣, 🟢."""
        assert self.reading(tmp_path, "somebody-else-entirely").kind is WaitingKind.NONE

    def test_a_record_whose_process_is_gone_names_nobody(self, tmp_path: Path) -> None:
        """A registry file outlives the Session that wrote it, and liveness is asked apart."""
        dead = 2**22 - 1  # above `kern.maxproc`, so no process can hold it

        assert self.reading(tmp_path, self.PEER_NAME, pid=dead).kind is WaitingKind.NONE

    def test_a_stopped_session_with_a_background_command_is_waiting_on_a_child(
        self, tmp_path: Path
    ) -> None:
        """The child wait enters through the existing child tracking (ADR 0021)."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            settings=ClaudeSettings(registry_directory=sessions),
        )
        adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
            session_id=SESSION,
            pid=TARGET.pid,
            transcript_path=transcript(tmp_path, BACKGROUND_COMMAND_RUNNING),
        )

        found = adapter.stop_reading(TARGET).waiting_for

        assert found.kind is WaitingKind.CHILD
        assert found.awaiting is None


class TestTheRegistryOverlayOnARosterRow:
    """What only the Session's own registry record can tell the roster (#325).

    `claude agents --json` answers `busy` for a Session whose record reads
    `status: "shell"` — measured at #154, and the reason the roster projection
    maps three status words and not four. So the roster called a Session with a
    background command `running` while the Reply Window sweep, reading the same
    record, called its turn over and sent a 🟣 Stop Notice. These are the rows of
    that overlay: what it changes, and everything it leaves exactly as found.
    """

    def registered(
        self, tmp_path: Path, *, status: str, session_id: str = SESSION
    ) -> ClaudeAgentAdapter:
        """An adapter whose registry holds one record for this Session's pid."""
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"{TARGET.pid}.json").write_text(
            json.dumps(
                {
                    "pid": TARGET.pid,
                    "sessionId": session_id,
                    "cwd": str(tmp_path),
                    "peerProtocol": PEER_PROTOCOL,
                    "messagingSocketPath": str(tmp_path / "claude.sock"),
                    "status": status,
                }
            ),
            encoding="utf-8",
        )
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            settings=ClaudeSettings(registry_directory=sessions),
        )
        adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
            session_id=SESSION,
            pid=TARGET.pid,
            transcript_path=transcript(tmp_path, BACKGROUND_COMMAND_RUNNING),
        )
        return adapter

    def listed(self, state: SessionState, waiting: WaitingFor | None = None) -> LaneDiscovery:
        """What the roster command answered, before anything overlays it."""
        return LaneDiscovery(
            rows=(
                SessionInspection(
                    target=TARGET,
                    workspace=Path("/tmp/workspace"),
                    state=state,
                    waiting_for=waiting if waiting is not None else WaitingFor(),
                ),
            )
        )

    def test_a_shell_record_makes_a_running_row_idle_and_waiting_on_its_child(
        self, tmp_path: Path, roster
    ) -> None:
        """The ticket, in one pass: the overlay runs ahead of the stop gate.

        `_row_with_stop` opens a transcript only for a row that is no longer
        `RUNNING`, so the flip has to happen before it or the child wait is never
        asked for — and the roster would keep disagreeing with the Stop Notice
        the same record already produced.
        """
        adapter = self.registered(tmp_path, status="shell")
        roster(self.listed(SessionState.RUNNING))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.state is SessionState.IDLE
        assert found.waiting_for.kind is WaitingKind.CHILD
        assert found.waiting_for.awaiting is None

    def test_the_flipped_row_reads_the_same_way_the_stop_notice_does(
        self, tmp_path: Path, roster
    ) -> None:
        """One Session, two surfaces, one word — which is the whole ticket."""
        adapter = self.registered(tmp_path, status="shell")
        roster(self.listed(SessionState.RUNNING))

        row = asyncio.run(adapter.discover()).rows[0]
        notice = adapter.stop_reading(TARGET).waiting_for

        assert row.waiting_for.kind is notice.kind is WaitingKind.CHILD
        assert row.state is notice.stopped_state is SessionState.IDLE

    def test_the_flipped_rows_reply_window_is_open(self, tmp_path: Path, roster) -> None:
        """`shell` reads OPEN in the registry sweep, and now on the row too (#154)."""
        adapter = self.registered(tmp_path, status="shell")
        roster(self.listed(SessionState.RUNNING))

        found = asyncio.run(adapter.discover()).rows[0]

        assert derive_reply_window(found.state, found.waiting_for, found.child) is ReplyWindow.OPEN

    def test_a_busy_record_leaves_the_running_row_alone_and_opens_no_transcript(
        self, tmp_path: Path, roster, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate that keeps the cadence off the hot path is untouched.

        The overlay is narrow on purpose: `busy` is the roster and the registry
        agreeing, so the row stays `RUNNING` and its transcript is never parsed —
        which is what makes a five-second sweep over a machine of working
        Sessions cost one command and no file reads.
        """
        adapter = self.registered(tmp_path, status="busy")
        roster(self.listed(SessionState.RUNNING))
        opened: list[Path] = []
        original = TranscriptReader.records

        def watched(self_: TranscriptReader, path: Path):  # type: ignore[no-untyped-def]
            opened.append(path)
            return original(self_, path)

        monkeypatch.setattr(TranscriptReader, "records", watched)

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.state is SessionState.RUNNING
        assert found.waiting_for.kind is WaitingKind.NONE
        assert opened == []

    @pytest.mark.parametrize("status", ["shell", "waiting", None])
    def test_a_waiting_row_is_left_exactly_as_the_roster_stated_it(
        self, tmp_path: Path, roster, status: str | None
    ) -> None:
        """Only `RUNNING` moves, and a dialog on screen is not this reader's to erase.

        Both words are asked, and the answer is measured against the control —
        the same pass with no record to read at all. That is what pins the
        overlay contributing *nothing* to a `WAITING` row, state and label
        alike: asserting the state word by itself would still pass if the
        registry's `waitingFor` had quietly displaced the roster's.
        """
        adapter = self.registered(tmp_path / (status or "no-record"), status=status or "waiting")
        if status is None:
            (tmp_path / "no-record" / "sessions" / f"{TARGET.pid}.json").unlink()
        roster(self.listed(SessionState.WAITING, ROSTER_WAITING))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.state is SessionState.WAITING
        assert found.waiting_for == self.waiting_row_without_a_record(tmp_path, roster)

    def waiting_row_without_a_record(self, tmp_path: Path, roster) -> WaitingFor:
        """What a `WAITING` row's wait reads as when no record overlays it at all."""
        control = self.registered(tmp_path / "control", status="waiting")
        (tmp_path / "control" / "sessions" / f"{TARGET.pid}.json").unlink()
        roster(self.listed(SessionState.WAITING, ROSTER_WAITING))
        return asyncio.run(control.discover()).rows[0].waiting_for

    def test_a_record_for_another_session_leaves_the_roster_word_alone(
        self, tmp_path: Path, roster
    ) -> None:
        """A recycled pid may not restate the Session the roster is talking about."""
        adapter = self.registered(tmp_path, status="shell", session_id="another-session")
        roster(self.listed(SessionState.RUNNING))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.state is SessionState.RUNNING

    def test_a_missing_record_leaves_the_roster_word_alone(self, tmp_path: Path, roster) -> None:
        """Fail toward what was observed: no record is not evidence of a stop."""
        adapter = self.registered(tmp_path, status="shell")
        (tmp_path / "sessions" / f"{TARGET.pid}.json").unlink()
        roster(self.listed(SessionState.RUNNING))

        assert asyncio.run(adapter.discover()).rows[0].state is SessionState.RUNNING

    def test_an_unreadable_record_leaves_the_roster_word_alone(
        self, tmp_path: Path, roster
    ) -> None:
        """A half-written record is an ordinary momentary state, not a stop."""
        adapter = self.registered(tmp_path, status="shell")
        (tmp_path / "sessions" / f"{TARGET.pid}.json").write_text("{not json", encoding="utf-8")
        roster(self.listed(SessionState.RUNNING))

        assert asyncio.run(adapter.discover()).rows[0].state is SessionState.RUNNING

    def test_the_word_shell_is_spelled_in_exactly_one_module(self) -> None:
        """The two readers that act on it cite one name (#325, ruling 3).

        A third literal in a third module is how one reader comes to disagree
        with another about the same Session, which is the defect this closes.
        """
        lane = Path(claude_adapter.__file__).parent
        naming = {
            source.name
            for source in lane.glob("*.py")
            if STATUS_IDLE_WITH_BACKGROUND in _string_literals(source)
        }

        assert naming == {"registry.py"}


def _string_literals(source: Path) -> set[str]:
    """Every string this module *evaluates*, docstrings and comments excluded.

    Prose may quote a registry document — `discovery.py` transcribes the
    measurement that a `shell` record reads `busy` on the roster, and should —
    and the rule being pinned is about the word a module *acts* on, not the word
    it explains. So the check reads the parse tree rather than the bytes.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    documented = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
    }
