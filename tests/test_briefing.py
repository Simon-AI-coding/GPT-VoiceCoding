"""Briefing — the one source of words about Session state.

The behaviours under test are the five states, the Focus Session's place in a
Roster Brief, and the rule that `text` carries every field: the engine hands
facts whole and never condenses, so a brief that dropped a field would be the
engine deciding what the user is told (#166).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.briefing import BriefState, MenuWord, Newest, NewestState, NoticeWord
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    SANDBOX_TOOL_NAME,
    ChildClassification,
    ChildKind,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressPhase,
    ProgressRole,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.call import (
    HANDOVER_BUDGET_BYTES,
    MAX_HANDOVER_ITEMS,
    Dial,
    SpokenBrief,
    SpokenRosterBrief,
)
from gpt_voicecoding.seams.control_plane import Action
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path(__file__).resolve().parents[1]
READ_AT = datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)

CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=1234)
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="def")


def said(text: str) -> ProgressObservation:
    return ProgressObservation.readable(
        has_history=True,
        read_at=READ_AT,
        recent=(ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=text),),
    )


def row(
    target: SessionTarget = CLAUDE,
    *,
    state: SessionState = SessionState.IDLE,
    waiting_for: WaitingFor | None = None,
    progress: ProgressObservation | None = None,
    name: SessionName | None = None,
    child: ChildClassification | None = None,
    last_activity: datetime | None = READ_AT,
) -> Session:
    return Session(
        target=target,
        workspace=WORKSPACE,
        first_seen=0.0,
        name=name if name is not None else SessionName(project="gpt-voicecoding", task="a task"),
        state=state,
        waiting_for=waiting_for or WaitingFor(),
        progress=progress if progress is not None else said("done"),
        last_activity=last_activity,
        child=child or ChildClassification(),
    )


QUESTION = WaitingFor(
    kind=WaitingKind.QUESTION,
    prompt="Which base?",
    options=(
        Option(text="main", description="the default branch", recommended=True),
        Option(text="develop"),
    ),
    recommendation="main",
)
PERMISSION = WaitingFor(
    kind=WaitingKind.PERMISSION,
    tool_name="Bash",
    detail="rm -rf build",
    approval_id="ap-1",
)


class TestTheFiveStates:
    def test_prose_questions_have_the_same_state_and_empty_decision_on_both_surfaces(self):
        for target in (CLAUDE, CODEX):
            asked = (
                said("Pick 1 or 2?")
                if target is CLAUDE
                # Codex marks its final answer, and only a marked one is read
                # (#188); nothing else about this case differs between the lanes.
                else codex_said(("Pick 1 or 2?", ProgressPhase.FINAL_ANSWER))
            )
            brief = briefing.session(row(target, progress=asked))
            assert brief.state is BriefState.DECISION
            assert briefing.spoken(brief).state == "waiting for your decision"
            assert briefing.spoken(brief).decision == ()
            notice = briefing.notice(brief)
            assert notice.question == ""
            assert notice.newest == "Pick 1 or 2?"
            from gpt_voicecoding.adapters.companion_channel.telegram.layout import lay_out

            laid_out = lay_out(notice)
            assert laid_out.text == (
                "🟡 waiting for your decision\n"
                f"{target.agent} · gpt-voicecoding · a task\n\nPick 1 or 2?"
            )
            assert [entity["type"] for entity in laid_out.entities] == ["expandable_blockquote"]

    def test_a_question_wait_is_a_decision(self) -> None:
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=QUESTION))
        assert brief.state is BriefState.DECISION

    def test_a_permission_wait_is_a_permission(self) -> None:
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=PERMISSION))
        assert brief.state is BriefState.PERMISSION

    def test_a_stopped_claude_session_with_no_wait_is_finished(self) -> None:
        """Legacy's "finished its turn and is waiting for the user", ported."""
        assert briefing.session(row()).state is BriefState.FINISHED

    def test_a_running_session_is_running(self) -> None:
        assert briefing.session(row(state=SessionState.RUNNING)).state is BriefState.RUNNING

    def test_a_running_session_whose_progress_is_unreadable_stays_running(self) -> None:
        """The state is the lifecycle's; the read only fills the fields."""
        brief = briefing.session(
            row(
                state=SessionState.RUNNING,
                progress=ProgressObservation.unreadable("no daemon holds it"),
            )
        )
        assert brief.state is BriefState.RUNNING
        assert brief.newest.state is NewestState.UNREADABLE

    def test_a_stopped_session_whose_wait_is_unclassified_is_finished(self) -> None:
        """#320 retired `unreadable`: a wait nobody could classify asks nothing."""
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
            )
        )
        assert brief.state is BriefState.FINISHED

    def test_a_stopped_session_whose_progress_is_unreadable_is_finished_and_says_why(self) -> None:
        """The failed read is a fact about the *message*, and it is still told (#320).

        This is the whole of what replaced the sixth state word: the state says
        nothing is being asked, and the omission sentence beside it says the
        newest message could not be read. "The engine does not know" is never
        dressed as "it is waiting for you".
        """
        brief = briefing.session(
            row(progress=ProgressObservation.unreadable("the transcript could not be read"))
        )
        assert brief.state is BriefState.FINISHED
        assert brief.newest.state is NewestState.UNREADABLE
        assert brief.newest.words.startswith("could not be read")
        assert briefing.notice(brief).state_word == "finished"

    def test_a_codex_turn_end_whose_answer_carries_no_phase_is_finished(self) -> None:
        """#320 reversed #166 B2's default: no answer read, so no question found."""
        assert briefing.session(row(CODEX)).state is BriefState.FINISHED

    def test_there_is_no_sixth_state_word(self) -> None:
        """The five words are the whole table, and `ended` is not one of them."""
        assert set(briefing.STATE_WORDING) == set(BriefState)
        assert [state.value for state in BriefState] == [
            "decision",
            "permission",
            "waiting_on",
            "finished",
            "running",
        ]
        assert not hasattr(BriefState, "UNREADABLE")
        # `ended` lives beside the state words, not among them (ADR 0021 §9).
        assert briefing.NOTICE_WORDING[NoticeWord.ENDED] == "ended"
        assert briefing.ended_line(row()).startswith("⚫ ended · ")

    def test_an_exited_session_never_appears_in_a_roster_brief(self) -> None:
        ended = replace(row(), lifecycle=SessionLifecycle.ENDED)
        brief = briefing.roster((ended,), focus=None)
        assert brief.rows == ()
        assert sum(brief.counts.values()) == 0


FINAL_ANSWER = ProgressPhase.FINAL_ANSWER
COMMENTARY = ProgressPhase.COMMENTARY


def codex_said(*said: tuple[str, ProgressPhase | None]) -> ProgressObservation:
    """A Codex reading: what the agent said, each with the `phase` it carried."""
    return ProgressObservation.readable(
        has_history=True,
        read_at=READ_AT,
        recent=tuple(
            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=text, phase=phase)
            for text, phase in said
        ),
    )


def told(text: str, *, turn_id: str | None = None) -> ProgressEntry:
    """What the Session was told — and, in a tail, where a turn begins."""
    return ProgressEntry(ordinal=0, role=ProgressRole.USER, text=text, turn_id=turn_id)


def codex_tail(*entries: ProgressEntry) -> ProgressObservation:
    """A reading of both sides, in the order they happened."""
    return ProgressObservation.readable(has_history=True, read_at=READ_AT, recent=entries)


def answer(
    text: str, phase: ProgressPhase | None = FINAL_ANSWER, *, turn_id: str | None = None
) -> ProgressEntry:
    return ProgressEntry(
        ordinal=0, role=ProgressRole.ASSISTANT, text=text, phase=phase, turn_id=turn_id
    )


def codex_state(*said: tuple[str, ProgressPhase | None]) -> BriefState:
    return briefing.session(row(CODEX, progress=codex_said(*said))).state


def answered(text: str) -> BriefState:
    return codex_state((text, FINAL_ANSWER))


class TestACodexTurnThatAskedNothing:
    """#320: a question mark makes a decision, and nothing else does.

    #188 read this the other way round — DECISION was the default and a final
    answer with no sign of an ask was *promoted* out of it. The 2026-09-09
    corpus measured what that cost: a turn nobody could read and a hand-over
    that asked nothing were both announced as *waiting for your decision*, so
    the light the user was told to act on was the light that meant the engine
    could not tell. The evidence now runs the other way, and the two clauses
    that read a menu as an ask are gone with the default they served. The rule's
    measurements are still #176
    (`docs/research/2026-09-01-codex-turn-end-classification.md` §5).
    """

    def test_a_final_answer_that_reports_a_result_is_finished(self) -> None:
        assert answered("已完成并提交 1a15cb0，工作树干净。") is BriefState.FINISHED

    def test_a_final_answer_that_asks_is_a_decision(self) -> None:
        assert answered("我建议按方案处理。同意吗?") is BriefState.DECISION

    def test_a_full_width_question_mark_asks_too(self) -> None:
        """The corpus is 98% Chinese, so `？` is the common spelling of an ask."""
        assert answered("你是否拍板按上述方案处理 #140 和 #141？") is BriefState.DECISION

    def test_a_question_mark_the_user_never_saw_as_one_does_not_ask(self) -> None:
        """A fenced block is machinery being shown, not a question being put."""
        said = "已完成。\n\n```sh\ngit status --short   # 是否还有残留?\n```\n"
        assert answered(said) is BriefState.FINISHED

    def test_a_question_mark_inside_an_inline_code_span_does_not_ask(self) -> None:
        """The measured false positive `A → B` removes: a literal `??`."""
        assert answered("`git status --short` 仅显示 `?? uv.lock`。") is BriefState.FINISHED

    def test_a_question_mark_in_a_link_target_does_not_ask(self) -> None:
        """The target is an address; only the label is words the user read."""
        said = "已完成，见 [运行记录](https://ci.example/runs?id=7)。"
        assert answered(said) is BriefState.FINISHED

    def test_a_labelled_option_block_with_no_question_mark_is_finished(self) -> None:
        """The regression for the deleted clause (#320).

        A turn that lays out lettered options and asks nothing is a report of
        what it considered, and the same shape must never get two words. It was
        `_OPTION_BLOCK`, which was safe only while it merely *promoted* out of a
        DECISION default; it now creates a 🟡 the user is told to act on.
        """
        said = "两条路:\n\nA) 保留当前实现\nB) 换成统一观察器\nC) 全部回退\n"
        assert codex_state((said, FINAL_ANSWER)) is BriefState.FINISHED

    def test_a_named_option_with_no_question_mark_is_finished(self) -> None:
        """The regression for the other deleted clause, `_NAMED_OPTION`."""
        assert answered("➡️ 我建议采用方案 B。") is BriefState.FINISHED
        assert answered("选项 A 最省事。") is BriefState.FINISHED

    def test_a_named_option_that_does_ask_is_still_a_decision(self) -> None:
        """Nothing was lost that carried its own question mark."""
        assert answered("选项 A 最省事，可以吗？") is BriefState.DECISION

    def test_a_hand_over_that_states_the_next_move_is_finished(self) -> None:
        """The 2026-09-09 fixture: a statement is not a question (#320, story 4)."""
        assert answered("我这边到此为止，由你决定何时启动。") is BriefState.FINISHED

    def test_a_numbered_list_is_not_a_menu(self) -> None:
        """No numeric clause, deliberately: numbered lists are how findings are
        enumerated, which is the most common *done* shape in the corpus
        (`codex-rs/core/gpt_5_codex_prompt.md:47`, #176 §3)."""
        said = "未发现 Spec findings。\n\n1. 读了票\n2. 读了 diff\n3. 跑了测试\n"
        assert codex_state((said, FINAL_ANSWER)) is BriefState.FINISHED

    def test_the_answer_is_classified_even_when_commentary_came_after_it(self) -> None:
        """3 of 669 turns end on commentary; the answer before it is the answer."""
        assert (
            codex_state(("同意吗？", FINAL_ANSWER), ("正在收尾…", COMMENTARY))
            is BriefState.DECISION
        )
        assert (
            codex_state(("已完成。", FINAL_ANSWER), ("正在收尾…", COMMENTARY))
            is BriefState.FINISHED
        )

    def test_commentary_alone_asks_nothing(self) -> None:
        """A non-blocking mid-turn question is not the turn's answer, and a turn
        with no answer read is a turn nobody found a question in (#320)."""
        assert codex_state(("先看一下目录。", COMMENTARY)) is BriefState.FINISHED

    def test_the_answer_to_an_earlier_turn_is_not_this_turn_s_answer(self) -> None:
        """The tail carries several turns, and only this one ended (or did not).

        Without the boundary a turn still working — or one that produced only
        commentary — would be briefed on the *previous* turn's answer: since
        #320 that reads the question the user already settled and announces it
        as one still waiting for them, which is exactly the wrong direction.

        These three name no turn, which is the **fallback** rule (#210): a
        Claude reading, or a Codex build whose turns carried no `id`, still
        stops at the newest thing the user said. The three below them are the
        same three boundaries when the source did name its turns.
        """
        working = codex_tail(
            told("do the first thing"),
            answer("同意吗？"),
            told("now do the second"),
            answer("先看一下目录。", COMMENTARY),
        )
        assert briefing.session(row(CODEX, progress=working)).state is BriefState.FINISHED

    def test_a_turn_that_has_said_nothing_yet_is_not_the_turn_before_it(self) -> None:
        silent = codex_tail(told("do the thing"), answer("同意吗？"), told("now do the next"))
        assert briefing.session(row(CODEX, progress=silent)).state is BriefState.FINISHED

    def test_this_turn_s_answer_is_read_across_the_boundary_behind_it(self) -> None:
        """The boundary stops the search; it does not stop this turn being read."""
        done = codex_tail(
            told("do the first thing"),
            answer("同意吗？"),
            told("now do the second"),
            answer("已完成第二件事。"),
        )
        assert briefing.session(row(CODEX, progress=done)).state is BriefState.FINISHED

    def test_a_turn_opened_by_an_image_alone_is_not_the_turn_before_it(self) -> None:
        """#210: a `userMessage` carrying only an image leaves no entry, so the
        boundary the search stops at is the turn each entry names, not the
        newest thing the user said. Without it this reads the previous turn's
        answer and briefs FINISHED where the turn has produced only commentary.
        """
        wordless = codex_tail(
            told("do the first thing", turn_id="turn_one"),
            answer("同意吗？", turn_id="turn_one"),
            answer("先看一下目录。", COMMENTARY, turn_id="turn_two"),
        )
        assert briefing.session(row(CODEX, progress=wordless)).state is BriefState.FINISHED

    def test_a_turn_named_by_the_source_bounds_the_search_at_both_ends(self) -> None:
        """The named boundary stops the search; it does not stop this turn being
        read, and an entry the user put mid-turn does not end it either."""
        done = codex_tail(
            told("do the first thing", turn_id="turn_one"),
            answer("同意吗？", turn_id="turn_one"),
            told("now do the second", turn_id="turn_two"),
            answer("已完成第二件事。", turn_id="turn_two"),
        )
        assert briefing.session(row(CODEX, progress=done)).state is BriefState.FINISHED

    def test_a_link_target_that_holds_brackets_is_still_a_target(self) -> None:
        """A URL may carry balanced parentheses, and cutting at the first one
        leaves the query string in the prose (`?run=7` reads as an ask)."""
        said = "已完成，见 [运行记录](https://ci.example/path_(part)?run=7)。"
        assert answered(said) is BriefState.FINISHED

    def test_a_phase_this_build_cannot_read_is_not_an_answer(self) -> None:
        """The adapter maps an unrecognised codex word to `UNKNOWN`, and this is
        what that member means here: the turn did not end on its answer, so the
        question in it is not read either."""
        assert codex_state(("同意吗？", ProgressPhase.UNKNOWN)) is BriefState.FINISHED

    def test_a_session_nobody_read_is_finished_and_the_body_says_it_was_not_read(self) -> None:
        """#320 story 5: "the engine does not know" is never dressed as "your turn"."""
        brief = briefing.session(row(CODEX, progress=ProgressObservation()))
        assert brief.state is BriefState.FINISHED
        assert brief.newest.state is NewestState.NOT_READ
        assert brief.newest.words == "not read"

    def test_the_claude_lane_also_reads_a_prose_question(self) -> None:
        finished = briefing.session(row(CLAUDE, progress=codex_said(("同意吗？", FINAL_ANSWER))))
        assert finished.state is BriefState.DECISION

    def test_a_promotion_never_outranks_a_question_the_lane_reported(self) -> None:
        """The heuristic reads a turn that stopped on nothing; a typed wait wins."""
        brief = briefing.session(
            row(
                CODEX,
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=codex_said(("已完成。", FINAL_ANSWER)),
            )
        )
        assert brief.state is BriefState.DECISION


class TestWhatADecisionCarries:
    def test_a_decision_carries_its_options_and_the_recommendation(self) -> None:
        """Legacy's single recommendation, ported into `decision.recommendation`."""
        decision = briefing.session(row(state=SessionState.WAITING, waiting_for=QUESTION)).decision
        assert decision is not None
        assert decision.prompt == "Which base?"
        assert [(one.text, one.description, one.recommended) for one in decision.options] == [
            ("main", "the default branch", True),
            ("develop", None, False),
        ]
        assert decision.recommendation == "main"

    def test_a_permission_carries_the_tool_and_a_one_line_summary(self) -> None:
        decision = briefing.session(
            row(state=SessionState.WAITING, waiting_for=PERMISSION)
        ).decision
        assert decision is not None
        assert (decision.tool, decision.summary) == ("Bash", "rm -rf build")

    def test_a_sandbox_request_is_named_by_the_seam_s_own_wording(self) -> None:
        """The wait whose tool has no name still names something the user can act on."""
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name=SANDBOX_TOOL_NAME),
            )
        )
        assert brief.decision is not None
        assert brief.decision.tool == "sandbox network access"
        assert "sandbox network access" in briefing.text(brief)

    def test_a_wait_nobody_could_classify_keeps_whatever_was_read(self) -> None:
        """Never counted as a decision (B7), and never emptied either (#320)."""
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
                progress=said("halfway through"),
            )
        )
        assert brief.state is BriefState.FINISHED
        assert brief.newest == Newest(state=NewestState.SAID, text="halfway through")


PEER = SessionTarget(agent=AgentKind.CLAUDE, session_id="ghi", pid=4321)


class TestWaitingOnSomebody:
    """#320: the symmetric 🟣 state, and the one state word that names somebody."""

    def peer_row(self) -> Session:
        return row(PEER, name=SessionName(project="gpt-voicecoding", task="the driver"))

    def waiting_row(self) -> Session:
        return row(waiting_for=WaitingFor(kind=WaitingKind.PEER, awaiting=str(PEER)))

    def test_a_peer_wait_is_named_by_that_sessions_own_session_name(self) -> None:
        """Story 1: not my turn, and whose it is."""
        peer = self.peer_row()
        brief = briefing.session(self.waiting_row(), peers=(self.waiting_row(), peer))

        assert brief.state is BriefState.WAITING_ON
        assert brief.awaited == "gpt-voicecoding · the driver"

    def test_the_three_readers_of_the_table_say_the_same_word(self) -> None:
        """The Stop Notice, the Roster Brief row and the spoken brief, on one fixture.

        The state word is a template only in the table; every reader is handed
        the same filled sentence, because a template rendered at a call site is
        one a call site will forget to render.
        """
        waiting, peer = self.waiting_row(), self.peer_row()
        sessions = (waiting, peer)
        word = "waiting on gpt-voicecoding · the driver"

        brief = briefing.session(waiting, peers=sessions)
        summary = briefing.roster(sessions, focus=None)
        row_notice = next(
            item
            for item in briefing.roster_notice(summary).rows
            if item.state is BriefState.WAITING_ON
        )

        assert briefing.notice(brief).state_word == word
        assert briefing.spoken(brief).state == word
        assert row_notice.state_word == word
        assert word in briefing.text(brief)
        assert word in briefing.text(summary)

    def test_the_telegram_layout_lights_it_purple(self) -> None:
        from gpt_voicecoding.adapters.companion_channel.telegram.layout import lay_out

        brief = briefing.session(self.waiting_row(), peers=(self.waiting_row(), self.peer_row()))

        assert lay_out(briefing.notice(brief)).text.startswith("🟣 waiting on ")

    def test_the_ruling_and_the_escalation_read_the_same_way(self) -> None:
        """Story 2: the transcript cannot tell an ask from a ruling, so neither does this."""
        driver = row(PEER, name=SessionName(project="crew", task="the driver"))
        worker = row(waiting_for=WaitingFor(kind=WaitingKind.PEER, awaiting=str(PEER)))
        ruled = replace(driver, waiting_for=WaitingFor(kind=WaitingKind.PEER, awaiting=str(CLAUDE)))

        both = (worker, ruled)
        assert briefing.session(worker, peers=both).state is BriefState.WAITING_ON
        assert briefing.session(ruled, peers=both).state is BriefState.WAITING_ON
        assert briefing.session(ruled, peers=both).awaited == "gpt-voicecoding · a task"

    def test_a_peer_with_no_known_project_does_not_use_an_address_as_its_name(self) -> None:
        brief = briefing.session(self.waiting_row(), peers=())

        assert brief.state is BriefState.WAITING_ON
        assert briefing.spoken(brief).state == "waiting on Session"

    def test_a_child_wait_is_named_by_the_childs_own_name(self) -> None:
        """Story 3: a subagent or teammate the tracking has a name for."""
        waiting = row(waiting_for=WaitingFor(kind=WaitingKind.CHILD, awaiting="review-bridge"))

        assert briefing.spoken(briefing.session(waiting)).state == "waiting on review-bridge"

    def test_a_background_command_is_named_by_the_wording_table(self) -> None:
        """Story 8, and the rule that a command line is never a name."""
        waiting = row(waiting_for=WaitingFor(kind=WaitingKind.CHILD))
        brief = briefing.session(waiting)

        assert brief.state is BriefState.WAITING_ON
        assert brief.awaited is None
        assert briefing.spoken(brief).state == f"waiting on {briefing.BACKGROUND_COMMAND}"

    def test_the_counts_line_names_nobody_because_it_groups_by_state(self) -> None:
        """One roster line, several Sessions, and no one party to name."""
        summary = briefing.roster(
            (
                self.waiting_row(),
                replace(self.peer_row(), waiting_for=self.waiting_row().waiting_for),
            ),
            focus=None,
        )

        assert briefing.roster_notice(summary).counts == (
            f"sessions: 2 waiting on {briefing.SOMEBODY_ELSE}"
        )

    def test_the_reply_window_stays_open_so_the_notice_is_an_anchor(self) -> None:
        """Story 6: a 🟣 notice is an ordinary Anchor the user replies into."""
        for kind in (WaitingKind.PEER, WaitingKind.CHILD):
            waiting = WaitingFor(kind=kind, awaiting="somebody")
            assert waiting.stopped_state is SessionState.IDLE
            assert waiting.needs_the_user is False


class TestAnswerableHere:
    def test_a_question_is_answerable_only_when_the_lane_still_holds_the_route(self) -> None:
        waiting = row(state=SessionState.WAITING, waiting_for=QUESTION)
        assert briefing.session(waiting, question_answerable=True).answerable_here is True
        assert briefing.session(waiting, question_answerable=False).answerable_here is False

    def test_a_permission_is_answerable_only_while_a_handle_holds_it_open(self) -> None:
        held = row(state=SessionState.WAITING, waiting_for=PERMISSION)
        released = row(
            state=SessionState.WAITING,
            waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name="Bash"),
        )
        assert briefing.session(held).answerable_here is True
        assert briefing.session(released).answerable_here is False

    def test_a_running_session_takes_no_reply(self) -> None:
        assert briefing.session(row(state=SessionState.RUNNING)).answerable_here is False


class TestNewest:
    def test_the_newest_assistant_message_travels_whole(self) -> None:
        """The engine never condenses: the conclusion and the detail are one field."""
        whole = "I rebuilt the index and every test passes. " * 20
        brief = briefing.session(row(progress=said(whole)))
        assert brief.newest == Newest(state=NewestState.SAID, text=whole)

    def test_a_session_that_has_said_nothing_says_so(self) -> None:
        brief = briefing.session(
            row(progress=ProgressObservation.readable(has_history=False, read_at=READ_AT))
        )
        assert brief.newest.state is NewestState.NOTHING_SAID
        assert "nothing said yet" in briefing.text(brief)

    def test_an_oversize_newest_entry_is_named_rather_than_dropped(self) -> None:
        brief = briefing.session(
            row(
                progress=ProgressObservation.readable(
                    has_history=True,
                    read_at=READ_AT,
                    omission=ProgressOmission.NEWEST_OVERSIZE,
                )
            )
        )
        assert brief.newest.state is NewestState.OVERSIZE
        assert "too large to carry" in briefing.text(brief)

    def test_nobody_having_looked_is_not_the_same_as_having_failed_to_read(self) -> None:
        assert (
            briefing.session(row(progress=ProgressObservation())).newest.state
            is NewestState.NOT_READ
        )
        assert (
            briefing.session(row(progress=ProgressObservation.unreadable("gone"))).newest.state
            is NewestState.UNREADABLE
        )


class TestTheRosterBrief:
    def test_focus_neither_reorders_equal_activity_nor_changes_the_counts(self) -> None:
        focus = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)
        others = (
            row(CLAUDE, state=SessionState.RUNNING),
            row(
                SessionTarget(agent=AgentKind.CLAUDE, session_id="ghi", pid=99),
                state=SessionState.WAITING,
                waiting_for=PERMISSION,
            ),
        )
        brief = briefing.roster((*others, focus), focus=CODEX)
        assert [one.target for one in brief.rows] == [CLAUDE, others[1].target, CODEX]
        assert brief.rows[-1].focus is True
        assert brief.counts == {
            BriefState.RUNNING: 1,
            BriefState.PERMISSION: 1,
            BriefState.DECISION: 1,
        }

    def test_with_no_focus_the_counts_are_every_live_session(self) -> None:
        brief = briefing.roster((row(), row(CODEX, state=SessionState.RUNNING)), focus=None)
        assert brief.counts == {BriefState.FINISHED: 1, BriefState.RUNNING: 1}
        assert brief.focus is None

    def test_a_header_row_carries_the_name_the_agent_and_the_state(self) -> None:
        header = briefing.roster((row(),), focus=None).rows[0]
        assert header.name == SessionName(project="gpt-voicecoding", task="a task")
        assert header.agent is AgentKind.CLAUDE
        assert header.state is BriefState.FINISHED

    def test_a_child_process_is_not_a_row_the_voice_can_ask_about(self) -> None:
        """Seen, never spoken to: every row must be one `brief <address>` answers."""
        child = row(
            CODEX,
            child=ChildClassification(kind=ChildKind.CHILD, parent=CLAUDE),
            name=None,
        )
        assert briefing.roster((row(), child), focus=None).rows == (
            briefing.roster((row(),), focus=None).rows[0],
        )

    def test_an_empty_roster_says_so(self) -> None:
        assert "none" in briefing.text(briefing.roster((), focus=None))


class TestText:
    def test_the_only_renderer_carries_every_field_a_session_brief_holds(self) -> None:
        brief = briefing.session(
            row(state=SessionState.WAITING, waiting_for=QUESTION, progress=said("I got this far")),
            question_answerable=True,
        )
        rendered = briefing.text(brief)
        assert "gpt-voicecoding · a task" in rendered
        assert "claude:abc:1234" in rendered
        assert "waiting for your decision" in rendered
        assert "I got this far" in rendered
        assert "Which base?" in rendered
        assert "the default branch" in rendered
        assert "develop" in rendered
        assert "main" in rendered
        assert READ_AT.isoformat() in rendered

    def test_an_unknown_project_keeps_the_generic_name_apart_from_its_address(self) -> None:
        rendered = briefing.text(briefing.session(replace(row(), name=None)))
        assert rendered.startswith("Session — claude:abc:1234 — ")

    def test_the_roster_brief_renders_the_counts_and_every_header_row(self) -> None:
        rendered = briefing.text(
            briefing.roster(
                (row(), row(CODEX, state=SessionState.WAITING, waiting_for=PERMISSION)),
                focus=CODEX,
            )
        )
        assert "gpt-voicecoding · a task" in rendered
        assert "codex:def" in rendered
        assert "requesting permission" in rendered
        assert "finished" in rendered


class TestTheSpokenBrief:
    def test_opening_shape_is_selected_from_the_current_roster(self):
        from gpt_voicecoding.core.call_keeper import Occasion

        one = row()
        other = row(CODEX, progress=codex_said(("Done.", FINAL_ANSWER)))
        assert briefing.for_call((), None, occasion=Occasion.OPENING) == ()
        assert briefing.for_call((one,), None, occasion=Occasion.OPENING) == (
            briefing.spoken(briefing.session(one)),
        )
        opening = briefing.for_call((one, other), CLAUDE, occasion=Occasion.OPENING)
        assert [type(item) for item in opening] == [SpokenBrief, SpokenRosterBrief]
        assert opening[0] == briefing.spoken(briefing.session(one))
        assert [
            type(item) for item in briefing.for_call((one, other), None, occasion=Occasion.OPENING)
        ] == [SpokenRosterBrief]
        assert briefing.for_call((one, other), None, occasion=Occasion.MID_CALL) == ()
        assert briefing.for_call((one, other), CLAUDE, occasion=Occasion.MID_CALL) == (
            briefing.spoken(briefing.session(one)),
        )

    """One Session Brief as the Call seam carries it — this module's words, as data.

    A Core type may not cross a seam (ADR 0001), so what crosses is the seam's
    own carrier. What it must *not* become is a second vocabulary: the adapter on
    the far side assembles these strings and chooses none of them, so every one
    of them is filled from the same tables `text` prints from.
    """

    def test_every_field_is_a_word_this_module_chose(self) -> None:
        brief = briefing.session(
            row(state=SessionState.WAITING, waiting_for=QUESTION, progress=said("I got this far")),
            question_answerable=True,
        )

        spoken = briefing.spoken(brief)

        assert spoken.name == SessionName(project="gpt-voicecoding", task="a task")
        assert spoken.agent == "claude"
        assert spoken.state == "waiting for your decision"
        assert spoken.newest == "I got this far"
        assert spoken.decision == (
            "asked: Which base?",
            "option: main — the default branch (recommended)",
            "option: develop",
            "recommends: main",
        )
        assert spoken.answerable_here == "from here"
        assert spoken.last_activity_at == READ_AT.isoformat()

    def test_a_session_with_no_known_project_has_no_address_as_its_spoken_name(self) -> None:
        spoken = briefing.spoken(briefing.session(replace(row(), name=None)))

        assert spoken.name == "Session"

    def test_an_omitted_newest_carries_the_reason_and_not_a_blank(self) -> None:
        brief = briefing.omitting_newest(briefing.session(row(progress=said("a long answer"))))

        assert briefing.spoken(brief).newest == "the newest entry is too large to carry"

    def test_a_running_session_carries_no_decision_lines(self) -> None:
        assert briefing.spoken(briefing.session(row(state=SessionState.RUNNING))).decision == ()


class TestTheHandover:
    """What a system-dialled call comes up already holding (#194, ADR 0018).

    Three kinds of item in one order: why the call was dialled, the roster, then
    the Sessions that need the user. The wire refuses an over-budget or
    over-count request outright rather than truncating it, so what is asserted
    here is that this function gives things back in the right order and never
    hands `Dial` something it would refuse.
    """

    def test_the_handover_starts_with_background_roster_and_has_no_dial_reason(self) -> None:
        items = briefing.for_call((row(),), focus=None)

        assert isinstance(items[0], SpokenRosterBrief)

    def test_a_running_session_gets_a_header_row_and_no_brief(self) -> None:
        items = briefing.for_call((row(CODEX, state=SessionState.RUNNING), row()), focus=None)

        summary = items[0]
        assert isinstance(summary, SpokenRosterBrief)
        assert "gpt-voicecoding · a task — codex:def — running" in summary.rows
        assert [item.agent for item in items if isinstance(item, SpokenBrief)] == ["claude"]

    def test_the_focus_session_is_briefed_first(self) -> None:
        focus = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)
        other = row(CLAUDE, state=SessionState.WAITING, waiting_for=PERMISSION)

        items = briefing.for_call((other, focus), focus=CODEX)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert [item.state for item in briefs] == [
            "waiting for your decision",
            "requesting permission",
        ]

    def test_which_sessions_are_briefed_is_the_rosters_answer_alone(self) -> None:
        """No caller may name one *into the list*, whatever it thinks it knows.

        An earlier draft took the Session the call was dialled about and briefed
        it ahead of the Focus Session whatever the row said. That put a `running`
        brief inside the list of Sessions needing the user, because
        `sessions.set_stop_reading` then left a row `RUNNING` unless the wait
        needed the user — a third module's staleness papered over here, at the
        cost of two of this function's own rules.

        There is no way back in. The row a caller used to compensate for — the
        Session a Stop dialled the call about — now says it stopped
        (`sessions.set_stop_reading`, #213), so the roster briefs it itself and
        the tests below are what say so.
        """
        running = row(CODEX, state=SessionState.RUNNING, progress=said("it stopped here"))

        items = briefing.for_call((running,), focus=None)

        assert [item for item in items if isinstance(item, SpokenBrief)] == []

    def test_a_question_is_answerable_here_only_when_the_lane_says_so(self) -> None:
        waiting = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)

        without = briefing.for_call((waiting,), focus=None)
        with_route = briefing.for_call((waiting,), focus=None, answerable=(CODEX,))

        assert _only_brief(without).answerable_here == "at the terminal"
        assert _only_brief(with_route).answerable_here == "from here"

    def test_a_decision_is_never_given_up_to_keep_a_header_row(self) -> None:
        """The ladder's own order, on the case that exposed the wrong one.

        Two hundred waiting Sessions: the roster's rows alone are over budget, so
        something has to go. Giving up briefs first produced one hundred and
        fifty-four names and not one decision — a hand-over that told the user
        which Sessions exist and nothing about what any of them is asking.
        """
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.for_call(sessions, focus=None)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert briefs, "every decision was given up to keep a list of names"
        assert all(item.decision for item in briefs)
        assert _fits(items)

    def test_over_budget_the_newest_bodies_go_from_the_back_and_are_named(self) -> None:
        """Named as omitted and never sliced (ADR 0016), and from the back (#166).

        Twelve bodies of a tenth of the budget apiece overrun it by enough that
        some go and not all: the sizes are taken from the budget rather than
        written out, so this keeps testing the rung and not a number (#215).
        """
        body = "x" * (HANDOVER_BUDGET_BYTES // 10)
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said(body),
            )
            for index in range(12)
        )

        items = briefing.for_call(sessions, focus=None)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert len(briefs) == 12
        carried = [item.newest for item in briefs if item.newest.startswith("x")]
        omitted = [
            item.newest
            for item in briefs
            if item.newest == "the newest entry is too large to carry"
        ]
        assert carried and omitted
        # The ones that kept their body are the ones the roster ordered first.
        assert [item.newest for item in briefs] == carried + omitted
        # Everything else stays: the header and the whole decision are what the
        # user acts on, and they are small.
        assert all(item.decision for item in briefs)
        assert _fits(items)

    def test_a_hand_over_never_exceeds_either_ceiling(self) -> None:
        """Both are hard refusals on the wire, so `Dial` accepts what this returns."""
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.for_call(sessions, focus=None)

        assert len(items) <= MAX_HANDOVER_ITEMS
        assert _fits(items)
        Dial(voice="prose", agent="rules", hand_over=items)

    def test_chinese_bodies_the_old_allowance_gave_up_now_go_whole(self) -> None:
        """#215: the loosening, measured on the language it was raised about.

        Six Sessions with six hundred Chinese characters apiece is about eleven
        thousand UTF-8 bytes — over the 8,192-byte allowance that used to stand
        here, which would have named every one of these newest messages as
        omitted, and well inside the wire's real ceiling of 8,192 estimated
        tokens at four bytes each. Nothing about the ladder changed; it simply
        starts higher up.
        """
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("它" * 600),
            )
            for index in range(6)
        )

        items = briefing.for_call(sessions, focus=None)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert len(briefs) == 6
        assert all(item.newest == "它" * 600 for item in briefs)
        assert sum(item.size_in_bytes for item in items) > 8192
        assert _fits(items)
        Dial(voice="prose", agent="rules", hand_over=items)

    def test_the_counts_survive_any_scale_and_the_focus_is_still_briefed_first(self) -> None:
        """The one thing that never goes, and the one order that never changes.

        Two hundred waiting Sessions fit under neither ceiling, so most of this
        roster is given back — and what is left still says how many there were.
        The counts are the summary ADR 0016 asks for: they are what tells the
        Voice that the call could not carry them all, and they say it without a
        fourth kind of item on the seam #195 and #196 build on. The Focus Session
        is still the first brief, because trimming takes from the back and the
        order the roster chose is the order that survives.
        """
        focus = row(
            CODEX,
            state=SessionState.WAITING,
            waiting_for=PERMISSION,
            progress=said("z" * 400),
        )
        others = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.for_call((*others, focus), focus=CODEX)

        summary = items[0]
        assert isinstance(summary, SpokenRosterBrief)
        assert summary.counts == "sessions: 200 waiting for your decision, 1 requesting permission"
        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert briefs[0].state == "requesting permission"
        assert len(items) <= MAX_HANDOVER_ITEMS
        assert _fits(items)
        Dial(voice="prose", agent="rules", hand_over=items)

    def test_a_session_whose_row_says_it_stopped_is_briefed_from_the_roster(self) -> None:
        """The row a caller used to have to compensate for (#213).

        A Stop that merely ended a turn used to leave its row `RUNNING`, and a
        running Session is briefed by nothing here — so a call dialled by that
        Stop said a Session needed the user and never said which, until the
        caller passed the Stop's own brief in beside the roster. The registry now
        writes the state the Stop implies, so the row arrives here as `IDLE` and
        is briefed like any other Session that stopped, in its place in the
        roster's own order.
        """
        stopped = row(CODEX, state=SessionState.IDLE, progress=said("it stopped here"))
        waiting = row(CLAUDE, state=SessionState.WAITING, waiting_for=PERMISSION)

        items = briefing.for_call((stopped, waiting), focus=None)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert [item.state for item in briefs] == ["finished", "requesting permission"]
        assert briefs[0].newest == "it stopped here"

    def test_a_session_that_stopped_is_briefed_exactly_once(self) -> None:
        """One Session, one brief. The roster's reading is the only one there is."""
        stopped = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)

        items = briefing.for_call((stopped,), focus=None)

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert len(briefs) == 1
        assert briefs[0].state == "waiting for your decision"


def _only_brief(items: tuple[object, ...]) -> SpokenBrief:
    briefs = [item for item in items if isinstance(item, SpokenBrief)]
    assert len(briefs) == 1
    return briefs[0]


def _fits(items: tuple[object, ...]) -> bool:
    return sum(item.size_in_bytes for item in items) <= HANDOVER_BUDGET_BYTES  # type: ignore[attr-defined]


class TestTheChannelNotice:
    """One Session Brief as the Companion Channel seam carries it (ADR 0021 §5).

    The same rule as the spoken brief, on the other surface: what crosses is
    the seam's own carrier, filled from the wording tables here, and the adapter
    that lays it out chooses no words. What it adds over `SpokenBrief` is a state
    the adapter can key a light on and the option labels as a tuple, in order,
    because a numeral picks one by position (ADR 0021 §3, §6).
    """

    def test_a_question_notice_carries_the_words_the_layout_prints(self) -> None:
        brief = briefing.session(
            row(state=SessionState.WAITING, waiting_for=QUESTION, progress=said("I got this far")),
            question_answerable=True,
        )

        notice = briefing.notice(brief)

        assert notice.state is BriefState.DECISION
        assert notice.state_word == "waiting for your decision"
        assert notice.agent == "claude"
        assert notice.name == SessionName(project="gpt-voicecoding", task="a task")
        assert notice.question == "Which base?"
        assert notice.options == ("main", "develop")
        assert notice.recommendation == "recommends: main"
        assert notice.newest == "I got this far"
        assert notice.cut_marker == "… cut here; the rest is on the terminal"
        assert notice.answerable_here is True
        assert notice.answer_wording == "answer from here"

    def test_a_permission_notice_offers_allow_and_deny_from_the_wording_table(self) -> None:
        """ADR 0021 §6: `allow` / `deny` are Core's labels, so a numeral 1 is `allow`."""
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=PERMISSION))

        notice = briefing.notice(brief)

        assert notice.state is BriefState.PERMISSION
        assert notice.question == "permission: Bash — rm -rf build"
        assert notice.options == ("allow", "deny")
        assert notice.recommendation == ""

    def test_a_finished_notice_has_an_empty_question_slot(self) -> None:
        notice = briefing.notice(briefing.session(row(progress=said("All green."))))

        assert notice.state is BriefState.FINISHED
        assert notice.state_word == "finished"
        assert notice.question == ""
        assert notice.options == ()
        assert notice.newest == "All green."

    def test_a_session_with_no_known_project_has_no_address_as_its_notice_name(self) -> None:
        notice = briefing.notice(briefing.session(replace(row(), name=None)))

        assert notice.name == "Session"

    def test_an_absent_newest_carries_the_omission_words(self) -> None:
        brief = briefing.omitting_newest(briefing.session(row(progress=said("a long answer"))))

        assert briefing.notice(brief).newest == "the newest entry is too large to carry"

    def test_a_terminal_only_question_says_so(self) -> None:
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=QUESTION))

        notice = briefing.notice(brief)

        assert notice.answerable_here is False
        assert notice.answer_wording == "answer at the terminal"

    def test_a_roster_notice_keeps_activity_order_and_counts_all_sessions(self) -> None:
        summary = briefing.roster(
            [
                replace(row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION), name=None),
                row(CLAUDE, state=SessionState.RUNNING),
            ],
            CLAUDE,
        )

        notice = briefing.roster_notice(summary)

        assert [(r.state, r.name, r.agent, r.state_word) for r in notice.rows] == [
            (BriefState.DECISION, "Session", "codex", "waiting for your decision"),
            (
                BriefState.RUNNING,
                SessionName(project="gpt-voicecoding", task="a task"),
                "claude",
                "running",
            ),
        ]
        assert notice.counts == "sessions: 1 waiting for your decision, 1 running"

    def test_a_roster_notice_without_a_focus_counts_every_session(self) -> None:
        summary = briefing.roster([row(CLAUDE, state=SessionState.RUNNING)], None)

        assert briefing.roster_notice(summary).counts == "sessions: 1 running"


class TestTheWordingTable:
    """The words every surface prints, held once and in English (ADR 0021, Words)."""

    def test_the_closed_words_and_labels_are_english_entries_of_one_table(self) -> None:
        assert briefing.NOTICE_WORDING[NoticeWord.HANDLED] == "handled"
        assert briefing.NOTICE_WORDING[NoticeWord.ENDED] == "ended"
        assert briefing.NOTICE_WORDING[NoticeWord.ALLOW] == "allow"
        assert briefing.NOTICE_WORDING[NoticeWord.DENY] == "deny"
        assert briefing.NOTICE_WORDING[NoticeWord.TRUNCATED]
        assert briefing.say_to("gpt-voicecoding · a task") == "Say to gpt-voicecoding · a task:"


class TestTheMenuWording:
    """The words the menu screens print, held once in English (ADR 0021 §6, #264)."""

    def test_the_menu_words_are_english_entries_of_one_table(self) -> None:
        assert briefing.MENU_WORDING[MenuWord.SEND_MESSAGE] == "send message"
        assert briefing.MENU_WORDING[MenuWord.ON] == "on"
        assert briefing.MENU_WORDING[MenuWord.OFF] == "off"
        assert briefing.MENU_WORDING[MenuWord.SWITCHES].startswith("switches")
        assert set(briefing.MENU_WORDING) == set(MenuWord)

    def test_a_label_that_is_also_a_verb_is_read_off_the_shared_set(self) -> None:
        """Rename the action and the label follows it, rather than drifting (ADR 0021 §6).

        Pinning the same literal here as the table holds would prove only that
        two hand-written strings match; what has to hold is that the word on
        the button is the word the command line accepts.
        """
        assert briefing.MENU_WORDING[MenuWord.BRIEF] == str(Action.BRIEF)
        assert briefing.MENU_WORDING[MenuWord.HISTORY] == str(Action.HISTORY)
        assert briefing.MENU_WORDING[MenuWord.SWITCH] == str(Action.SWITCH)
        assert briefing.MENU_WORDING[MenuWord.VERIFY] == str(Action.VERIFY)
        assert briefing.MENU_WORDING[MenuWord.LIVE] == str(Action.LIVE)
        assert briefing.MENU_WORDING[MenuWord.CONFIG] == str(Action.CONFIG)

    def test_a_switch_label_carries_its_state(self) -> None:
        assert briefing.switch_label("duty", True) == "duty: on"
        assert briefing.switch_label("auto_hangup", False) == "auto_hangup: off"

    def test_the_greeting_is_the_headline_text_prints(self) -> None:
        """One Session, one line, on every surface: the greeting is `text`'s header."""
        session = row(CLAUDE, state=SessionState.RUNNING)

        header = briefing.text(briefing.session(session)).splitlines()[0]
        assert briefing.greeting(session) == header

        assert briefing.greeting(session) == "gpt-voicecoding · a task — claude:abc:1234 — running"
