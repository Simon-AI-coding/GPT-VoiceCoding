"""The Session registry — Bridge Core state, deliberately not a module.

The behaviours under test are the ones the reference implementation got wrong or
left to prose: a target is exact or it is refused, a Session Name disambiguates or asks,
and a stale identity fails closed rather than resolving to something plausible.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gpt_voicecoding.core.errors import (
    ChildSessionError,
    DuplicateSessionError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    ReplyWindow,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path(__file__).resolve().parents[1]


def claude(session_id: str, pid: int, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid),
        name=SessionName("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        first_seen=1_000.0,
    )


def codex(session_id: str, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id),
        name=SessionName("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        first_seen=1_000.0,
    )


class TestRegistering:
    def test_a_registered_session_resolves_by_its_exact_target(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.resolve(session.target) == session

    def test_registering_the_same_target_twice_is_refused(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(DuplicateSessionError):
            registry.register(codex("abc"))

    def test_a_resumed_claude_session_forks_a_second_pid_under_one_session_id(self) -> None:
        """Both are live, and they are two different Sessions."""
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        registry.register(claude("abc", pid=101))
        assert len(registry.live()) == 2

    def test_a_non_pid_agent_may_not_hold_one_session_id_twice(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(DuplicateSessionError):
            registry.register(codex("abc", task="another task"))


class TestResolving:
    def test_an_unknown_session_id_fails_closed(self) -> None:
        registry = SessionRegistry()
        with pytest.raises(UnknownSessionError):
            registry.resolve(SessionTarget(agent=AgentKind.CODEX, session_id="nope"))

    def test_the_wrong_pid_under_a_known_session_id_is_stale_not_unknown(self) -> None:
        """The distinction is load-bearing: one is a typo, the other is a fork."""
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        with pytest.raises(StaleSessionError) as raised:
            registry.resolve(SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=999))
        assert 100 in raised.value.live_pids

    def test_a_fork_resolves_to_the_pid_that_was_asked_for(self) -> None:
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        registry.register(claude("abc", pid=101))
        resolved = registry.resolve(
            SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=101)
        )
        assert resolved.target.pid == 101

    def test_an_ended_session_fails_closed_rather_than_resolving(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.mark_ended(session.target)
        with pytest.raises(StaleSessionError):
            registry.resolve(session.target)

    def test_a_forgotten_session_is_unknown_again(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.forget(session.target)
        with pytest.raises(UnknownSessionError):
            registry.resolve(session.target)

    def test_a_pid_carried_on_a_codex_target_does_not_change_the_answer(self) -> None:
        """Only agents addressed by pid are matched on it."""
        registry = SessionRegistry()
        registry.register(codex("abc"))
        resolved = registry.resolve(SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=777))
        assert resolved.target.pid is None


class TestRefusingAChildProcess:
    """Seen, not spoken to — refused here rather than remembered by a caller (#79).

    Structural on purpose: a crew's reviewer answering a question meant for the
    Session that spawned it is the user's own words landing under somebody
    else's authority, and a rule each caller had to remember is a rule one
    caller will forget.
    """

    def spawned(self, agent_id: str = "a891a18f447827175") -> Session:
        return Session(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id=agent_id, pid=9231),
            workspace=WORKSPACE,
            first_seen=1_000.0,
            child=ChildClassification(
                kind=ChildKind.CHILD,
                parent=SessionTarget(agent=AgentKind.CLAUDE, session_id="parent", pid=9231),
            ),
        )

    def test_a_child_is_refused_as_a_target(self) -> None:
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError):
            registry.resolve(child.target)

    def test_the_refusal_names_the_child_in_the_words_a_surface_typed(self) -> None:
        """The acceptance reads this sentence and looks for the address in it.

        A non-zero exit is not by itself a refusal — the surface exits non-zero
        for an engine that never answered too — so the address is what proves
        the rule was applied rather than the call merely failing.
        """
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError) as raised:
            registry.resolve(child.target)
        assert "claude:a891a18f447827175:9231" in str(raised.value)

    def test_it_names_the_session_that_spawned_it_too(self) -> None:
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError) as raised:
            registry.resolve(child.target)
        assert "claude:parent:9231" in str(raised.value)
        assert raised.value.parent == child.child.parent

    def test_it_is_still_listed(self) -> None:
        """The whole difference from the reference implementation, in one line."""
        registry = SessionRegistry()
        registry.register(self.spawned())
        assert len(registry.live()) == 1

    def test_it_can_still_be_recorded_as_ended(self) -> None:
        """`resolve` guards addressing; `mark_ended` records what happened.

        Routing this through the refusal would leave the roster claiming a dead
        process is running, which is the one thing worse than listing it.
        """
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        assert registry.mark_ended(child.target).lifecycle is SessionLifecycle.ENDED


class TestReplyWindow:
    def test_a_held_question_is_open_only_while_the_lane_can_answer_it(self) -> None:
        question = WaitingFor(kind=WaitingKind.QUESTION, prompt="Which layout?")

        assert (
            derive_reply_window(
                SessionState.WAITING,
                question,
                ChildClassification(),
                question_answerable=True,
            )
            is ReplyWindow.OPEN
        )
        assert (
            derive_reply_window(
                SessionState.WAITING,
                question,
                ChildClassification(),
                question_answerable=False,
            )
            is ReplyWindow.CLOSED
        )

    def test_a_session_starts_with_its_reply_window_closed(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_the_window_follows_what_the_session_is_doing(self) -> None:
        """Derived, never set: one field, so nothing can disagree with it."""
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)

        registry.set_state(session.target, SessionState.IDLE)
        assert registry.resolve(session.target).reply_window is ReplyWindow.OPEN

        registry.set_state(session.target, SessionState.RUNNING)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_a_session_waiting_on_a_dialog_is_closed(self) -> None:
        """A dialog on screen blocks every other Relay until it is answered."""
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)

        registry.set_state(session.target, SessionState.WAITING)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_a_child_process_is_never_open_however_idle_it_is(self) -> None:
        """Seen, not spoken to (#68) — and the window says so, not just `resolve`."""
        registry = SessionRegistry()
        session = replace(
            codex("abc"),
            state=SessionState.IDLE,
            child=ChildClassification(kind=ChildKind.CHILD),
        )
        registry.register(session)

        assert registry.all()[0].reply_window is ReplyWindow.CLOSED

    def test_setting_the_state_of_an_unknown_session_fails_closed(self) -> None:
        registry = SessionRegistry()
        with pytest.raises(UnknownSessionError):
            registry.set_state(
                SessionTarget(agent=AgentKind.CODEX, session_id="nope"), SessionState.IDLE
            )

    def test_ending_a_session_closes_its_reply_window(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.set_state(session.target, SessionState.IDLE)

        ended = registry.mark_ended(session.target)
        assert ended.lifecycle is SessionLifecycle.ENDED
        assert ended.reply_window is ReplyWindow.CLOSED


class TestRoster:
    def test_the_roster_lists_live_sessions_in_registration_order(self) -> None:
        registry = SessionRegistry()
        first = codex("abc")
        second = claude("def", pid=100)
        registry.register(first)
        registry.register(second)
        assert registry.live() == (first, second)

    def test_the_roster_excludes_ended_sessions_but_all_still_holds_them(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.mark_ended(session.target)
        assert registry.live() == ()
        assert len(registry.all()) == 1


class TestTheSessionSpokenFirst:
    """`focus` is the record of a reply; `spoken_first` is what the voice does with it."""

    def test_a_sole_live_session_is_spoken_first_though_nothing_set_the_focus(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.focus is None, "no reply, so no Focus Session"
        assert registry.spoken_first == session.target

    def test_two_live_sessions_with_no_focus_leave_nobody_spoken_first(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        registry.register(claude("def", pid=100))
        assert registry.spoken_first is None

    def test_a_held_focus_wins_over_the_roster(self) -> None:
        registry = SessionRegistry()
        first = codex("abc")
        second = claude("def", pid=100)
        registry.register(first)
        registry.register(second)
        registry.set_focus(second.target)
        assert registry.spoken_first == second.target

    def test_the_survivor_of_an_ended_focus_is_spoken_first(self) -> None:
        registry = SessionRegistry()
        first = codex("abc")
        second = claude("def", pid=100)
        registry.register(first)
        registry.register(second)
        registry.set_focus(second.target)
        registry.mark_ended(second.target)
        assert registry.focus is None, "ended is ended"
        assert registry.spoken_first == first.target

    def test_an_empty_roster_has_nobody_spoken_first(self) -> None:
        assert SessionRegistry().spoken_first is None

    def test_a_child_process_does_not_make_a_roster_of_one_look_like_two(self) -> None:
        """A subagent is listed, never addressed — and never a Session to choose between."""
        registry = SessionRegistry()
        parent = codex("abc")
        registry.register(parent)
        child = replace(
            claude("def", pid=100),
            child=ChildClassification(kind=ChildKind.CHILD, parent=parent.target),
        )
        registry.register(child)
        assert len(registry.live()) == 2
        assert registry.sole_live() == parent
        assert registry.spoken_first == parent.target

    def test_an_ended_session_leaves_the_survivor_sole(self) -> None:
        registry = SessionRegistry()
        first = codex("abc")
        second = claude("def", pid=100)
        registry.register(first)
        registry.register(second)
        registry.mark_ended(second.target)
        assert registry.spoken_first == first.target


class TestTheFocusSession:
    """One pointer, cleared by the Session ending and by nothing else (#165 Q2)."""

    def test_the_focus_is_held_as_the_identity_the_roster_addresses_it_by(self) -> None:
        """A surface may write a weaker address than the roster holds."""
        registry = SessionRegistry()
        held = registry.register(claude("abc", pid=100))

        registry.set_focus(SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=100))

        assert registry.focus == held.target

    def test_a_session_the_roster_does_not_hold_leaves_the_focus_where_it_was(self) -> None:
        """A verdict carried for a row that ended is still a verdict carried.

        Refusing here would turn the answer landing into the user being told it
        did not — the opposite of what happened.
        """
        registry = SessionRegistry()
        registry.register(codex("abc"))
        registry.set_focus(SessionTarget(agent=AgentKind.CODEX, session_id="abc"))

        registry.set_focus(SessionTarget(agent=AgentKind.CODEX, session_id="gone"))

        assert registry.focus == SessionTarget(agent=AgentKind.CODEX, session_id="abc")

    def test_the_focus_clears_when_that_session_is_marked_ended(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.mark_ended(session.target)

        assert registry.focus is None

    def test_the_focus_clears_when_a_discovery_stops_seeing_that_session(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.observe(AgentKind.CODEX, LaneDiscovery(), now=2_000.0)

        assert registry.focus is None

    def test_the_focus_clears_when_that_session_is_forgotten(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.forget(session.target)

        assert registry.focus is None

    def test_another_session_ending_leaves_the_focus_alone(self) -> None:
        registry = SessionRegistry()
        focused = registry.register(codex("abc"))
        other = registry.register(codex("def"))
        registry.set_focus(focused.target)

        registry.mark_ended(other.target)

        assert registry.focus == focused.target

    def test_the_focus_does_not_follow_a_new_thread_on_the_same_process(self) -> None:
        """`/new` in a Codex TUI is a different Session under one pid (#77).

        Following it would make the Focus Session one the user has never replied
        to, which is the one way #165 Q2 says it must not be set.
        """
        registry = SessionRegistry()
        held = SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=6548)
        registry.register(Session(target=held, workspace=WORKSPACE, first_seen=1_000.0))
        registry.set_focus(held)

        registry.observed_one(
            SessionInspection(
                target=SessionTarget(agent=AgentKind.CODEX, session_id="def", pid=6548),
                workspace=WORKSPACE,
            ),
            now=2_000.0,
        )

        assert registry.focus is None

    def test_the_focus_follows_a_codex_row_that_gains_its_thread_id(self) -> None:
        """A better-known identity is the same Session, and nothing ended (#73)."""
        registry = SessionRegistry()
        anonymous = SessionTarget(agent=AgentKind.CODEX, pid=6548)
        registry.register(Session(target=anonymous, workspace=WORKSPACE, first_seen=1_000.0))
        registry.set_focus(anonymous)

        named = SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=6548)
        registry.observed_one(SessionInspection(target=named, workspace=WORKSPACE), now=2_000.0)

        assert registry.focus == named


def test_observation_names_climb_follow_and_survive_missing_sources(caplog):
    from gpt_voicecoding.core.naming import NameRung
    from gpt_voicecoding.core.policy import CorePolicy

    registry = SessionRegistry(policy=CorePolicy(first_prompt_characters=5))
    target = SessionTarget(agent=AgentKind.CLAUDE, session_id="naming", pid=100)

    def see(**fields):
        return registry.observed_one(
            SessionInspection(target=target, workspace=WORKSPACE, project_name="Project", **fields),
            now=1.0,
        )

    assert see(derived_name="floor").name.task == "floor"
    assert see(first_prompt="你好世界🙂 extra").name.task == "你好世界🙂"
    assert see(ai_title="AI title").name_rung is NameRung.AI_TITLE
    assert see(user_name="My title").name.task == "My title"
    assert see(user_name="New title").name.task == "New title"
    assert see(first_prompt="lesser").name.task == "New title"
    with caplog.at_level("DEBUG"):
        assert see(user_name="bad · name").name.task == "New title"
    assert any("carrying" in record.message for record in caplog.records)
    assert registry.resolve(target).name.task == "New title"
