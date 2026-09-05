"""The menu screens: every one an Anchor with option labels (ADR 0021 §6, #264).

Core's words, numbered; the row each screen registers; labels resolved by
position and never by text. What a press *does* is `core/bridge.py`'s and is
proved in `test_bridge.py`.
"""

from __future__ import annotations

from pathlib import Path

from gpt_voicecoding.core import briefing, menu
from gpt_voicecoding.core.anchors import AnchorKind, Screen
from gpt_voicecoding.core.briefing import MenuWord
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.agent import SessionState
from gpt_voicecoding.seams.companion_channel import MenuNotice, RosterNotice
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)
OTHER_CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="ghi")


def session(target: SessionTarget, task: str = "port the log") -> Session:
    return Session(
        target=target,
        name=SessionName("GPT-VoiceCoding", task),
        workspace=Path("/tmp/workspace"),
        first_seen=0.0,
        state=SessionState.RUNNING,
    )


class TestTheRosterScreen:
    def test_one_label_per_live_session_in_the_rosters_order(self) -> None:
        brief = briefing.roster([session(CODEX), session(CLAUDE, "build the shell")], CLAUDE)

        screen = menu.roster_screen(brief)

        assert screen.text == briefing.text(brief)
        assert screen.options == (
            "GPT-VoiceCoding · build the shell",
            "GPT-VoiceCoding · port the log",
        )
        assert screen.anchor is not None
        assert screen.anchor.kind is AnchorKind.MENU
        assert screen.anchor.target is Screen.ROSTER
        assert screen.anchor.picks == (CLAUDE, CODEX)

    def test_the_brief_beside_the_text_carries_the_same_labels(self) -> None:
        brief = briefing.roster([session(CODEX)], None)

        notice = menu.roster_screen(brief).notice

        assert isinstance(notice, RosterNotice)
        assert notice.options == ("GPT-VoiceCoding · port the log",)
        assert [row.name for row in notice.rows] == ["GPT-VoiceCoding · port the log"]

    def test_two_sessions_sharing_a_name_are_told_apart_by_address(self) -> None:
        """Resolved by position either way; the suffix is for the user's eyes."""
        brief = briefing.roster([session(CODEX), session(OTHER_CODEX), session(CLAUDE, "x")], None)

        screen = menu.roster_screen(brief)

        assert screen.options == (
            "GPT-VoiceCoding · port the log (codex:abc)",
            "GPT-VoiceCoding · port the log (codex:ghi)",
            "GPT-VoiceCoding · x",
        )
        assert screen.anchor is not None
        assert screen.anchor.picks == (CODEX, OTHER_CODEX, CLAUDE)
        # The row carries it too, not only the label (#264 review): the label
        # is what a button cuts, and it cuts the end — where the address is.
        assert isinstance(screen.notice, RosterNotice)
        assert tuple(row.name for row in screen.notice.rows) == screen.options

    def test_with_nothing_live_it_is_text_and_the_hint_and_no_anchor(self) -> None:
        brief = briefing.roster([], None)

        screen = menu.roster_screen(brief)

        assert screen.text == "sessions: none\n" + briefing.NOTHING_RUNNING_HINT
        assert screen.notice is None
        assert screen.anchor is None
        assert screen.options == ()


class TestTheGreetingScreen:
    def test_it_offers_brief_history_and_send_message_about_that_session(self) -> None:
        screen = menu.greeting_screen(session(CLAUDE))

        assert screen.text == (
            "GPT-VoiceCoding · port the log — claude:def:100 — running\n"
            "1. brief\n"
            "2. history\n"
            "3. send message"
        )
        assert screen.notice == MenuNotice(
            heading="GPT-VoiceCoding · port the log — claude:def:100 — running",
            options=("brief", "history", "send message"),
        )
        assert screen.anchor is not None
        assert screen.anchor.kind is AnchorKind.MENU
        assert screen.anchor.target == CLAUDE
        assert screen.anchor.picks == (MenuWord.BRIEF, MenuWord.HISTORY, MenuWord.SEND_MESSAGE)


class TestThePromptScreen:
    def test_it_is_the_say_to_line_asking_for_words_and_offering_nothing_to_pick(self) -> None:
        screen = menu.prompt_screen(session(CODEX))

        assert screen.text == "Say to GPT-VoiceCoding · port the log:"
        assert screen.notice == MenuNotice(
            heading="Say to GPT-VoiceCoding · port the log:", expects_words=True
        )
        assert screen.anchor is not None
        assert screen.anchor.kind is AnchorKind.PROMPT
        assert screen.anchor.target == CODEX
        assert screen.anchor.options == ()


class TestTheConfigScreen:
    def test_it_offers_switch_verify_and_live(self) -> None:
        screen = menu.config_screen()

        assert screen.text == "config\n1. switch\n2. verify\n3. live"
        assert screen.anchor is not None
        assert screen.anchor.target is Screen.CONFIG
        assert screen.anchor.picks == (MenuWord.SWITCH, MenuWord.VERIFY, MenuWord.LIVE)


class TestTheSwitchScreen:
    def test_one_label_per_switch_carrying_its_state_and_the_row_carries_the_names(self) -> None:
        board = Switchboard()
        board.flip(SwitchName.DUTY, True)

        screen = menu.switches_screen(board)

        assert screen.options == ("duty: on", "voice: off", "message: off", "auto_hangup: on")
        assert screen.text == (
            "switches — press one to flip it\n"
            "1. duty: on\n"
            "2. voice: off\n"
            "3. message: off\n"
            "4. auto_hangup: on"
        )
        assert screen.anchor is not None
        assert screen.anchor.target is Screen.SWITCHES
        assert screen.anchor.picks == ("duty", "voice", "message", "auto_hangup")
