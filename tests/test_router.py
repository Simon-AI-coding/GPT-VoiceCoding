"""The inbound-text router — classification is Bridge Core's, never the channel's.

The Companion Channel hands up text and no opinion about it. Deciding whether
that text is a control-plane command, an Answer Relay or a delegation happens
here, and anything unknown or ambiguous **fails closed with an honest reply**
rather than being guessed into a command.

The grammar has four forms and all of its vocabulary is injected, because the
command set belongs to the control-plane surface and not to this module:

    /<command> …      a control-plane command
    ><prompt>         a Delegated Turn
    @<name>: words    the user's own words, for the Session that name names
    words             the user's own words, when exactly one Session is live

The collision that has to be got right in both directions: a bare `stop` must
not silently become the control command, and `/stop` must not silently become
text injected into a coding session.
"""

from __future__ import annotations

from pathlib import Path

from gpt_voicecoding.core.anchors import Anchor, AnchorKind, AnchorTable, Screen
from gpt_voicecoding.core.briefing import (
    NON_TEXT_HINT,
    NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT,
    NUMERAL_PICKS_NOTHING_HINT,
    QUESTION_ALREADY_ANSWERED_HINT,
)
from gpt_voicecoding.core.router import InboundClass, InboundRouter, TextGrammar
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ApprovalVerdict,
    Option,
    ProgressObservation,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

COMMANDS = frozenset({"status", "stop", "switches"})


def registry_of(*sessions: tuple[SessionTarget, str]) -> SessionRegistry:
    registry = SessionRegistry()
    for target, task in sessions:
        registry.register(
            Session(
                target=target,
                name=SessionName("GPT-VoiceCoding", task),
                workspace=Path("/tmp/workspace"),
                first_seen=0.0,
            )
        )
    return registry


def router_over(registry: SessionRegistry) -> InboundRouter:
    return InboundRouter(sessions=registry, grammar=TextGrammar(control_commands=COMMANDS))


def router(*sessions: tuple[SessionTarget, str]) -> InboundRouter:
    return router_over(registry_of(*sessions))


class TestControlCommands:
    def test_a_marked_command_is_a_control_command(self) -> None:
        found = router((CODEX, "port the log")).classify("/status")

        assert found.kind is InboundClass.CONTROL
        assert found.command == "status"

    def test_a_command_carries_its_arguments_through_unparsed(self) -> None:
        """The control-plane surface owns the payload schema; this only routes."""
        found = router((CODEX, "port the log")).classify("/stop the log one")

        assert found.command == "stop"
        assert found.text == "the log one"

    def test_asking_for_progress_never_touches_a_session(self) -> None:
        """A progress question is a read, not a Relay."""
        found = router((CODEX, "port the log")).classify("/status")

        assert found.target is None

    def test_a_command_works_with_no_session_running_at_all(self) -> None:
        found = router().classify("/switches")

        assert found.kind is InboundClass.CONTROL

    def test_an_unregistered_command_fails_closed_rather_than_being_guessed(self) -> None:
        found = router((CODEX, "port the log")).classify("/deploy production")

        assert found.kind is InboundClass.UNKNOWN
        assert "deploy" in found.reply

    def test_the_command_set_is_injected_not_known_here(self) -> None:
        empty = InboundRouter(sessions=SessionRegistry(), grammar=TextGrammar())

        assert empty.classify("/status").kind is InboundClass.UNKNOWN


class TestDelegation:
    def test_a_marked_prompt_is_a_delegated_turn(self) -> None:
        found = router((CODEX, "port the log")).classify(">what does ADR 0002 say")

        assert found.kind is InboundClass.DELEGATION
        assert found.text == "what does ADR 0002 say"

    def test_an_empty_delegation_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify(">   ")

        assert found.kind is InboundClass.UNKNOWN

    def test_a_delegation_is_never_relayed_into_a_session(self) -> None:
        found = router((CODEX, "port the log")).classify(">summarise the diff")

        assert found.target is None


class TestAddressingASessionByName:
    def test_a_named_relay_resolves_to_that_session(self) -> None:
        found = router((CODEX, "port the log"), (CLAUDE, "build the shell")).classify(
            "@shell: ship it"
        )

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE
        assert found.text == "ship it"

    def test_a_name_matching_nothing_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify("@nothing: ship it")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply

    def test_a_name_matching_two_sessions_refuses_and_names_both(self) -> None:
        """A Session Name disambiguates or asks — never picks."""
        found = router((CODEX, "port the log"), (CLAUDE, "port the shell")).classify(
            "@port: ship it"
        )

        assert found.kind is InboundClass.UNKNOWN
        assert "port the log" in found.reply
        assert "port the shell" in found.reply

    def test_a_named_relay_with_no_words_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify("@log:   ")

        assert found.kind is InboundClass.UNKNOWN


class TestBareText:
    def test_bare_text_goes_to_the_one_live_session(self) -> None:
        found = router((CODEX, "port the log")).classify("yes, go ahead")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX
        assert found.text == "yes, go ahead"

    def test_bare_text_with_two_live_sessions_asks_which(self) -> None:
        found = router((CODEX, "port the log"), (CLAUDE, "build the shell")).classify(
            "yes, go ahead"
        )

        assert found.kind is InboundClass.UNKNOWN
        assert "port the log" in found.reply
        assert "build the shell" in found.reply

    def test_bare_text_with_nothing_running_says_so(self) -> None:
        found = router().classify("yes, go ahead")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply

    def test_an_ended_session_is_not_a_candidate(self) -> None:
        registry = registry_of((CODEX, "port the log"), (CLAUDE, "build the shell"))
        registry.mark_ended(CLAUDE)

        assert router_over(registry).classify("yes, go ahead").target == CODEX

    def test_empty_text_is_never_classified_as_anything(self) -> None:
        found = router((CODEX, "port the log")).classify("   ")

        assert found.kind is InboundClass.UNKNOWN

    def test_empty_text_is_answered_with_the_one_non_text_hint(self) -> None:
        """A voice note or a photo arrives as empty text (ADR 0021 §4). The hint is
        Core's wording, held once in the wording tables and read from there."""
        found = router((CODEX, "port the log")).classify("")

        assert found.reply == NON_TEXT_HINT
        assert "text" in NON_TEXT_HINT


class TestTheCommandWordCollision:
    def test_a_bare_command_word_is_never_guessed_into_a_command(self) -> None:
        """The locked rule: never guessed into a command."""
        found = router((CODEX, "port the log")).classify("stop")

        assert found.kind is InboundClass.UNKNOWN

    def test_the_refusal_offers_both_readings_rather_than_picking_one(self) -> None:
        found = router((CODEX, "port the log")).classify("stop")

        assert "/stop" in found.reply
        assert "port the log" in found.reply

    def test_the_marked_command_is_never_injected_into_a_session(self) -> None:
        """The other direction of the same collision."""
        found = router((CODEX, "port the log")).classify("/stop")

        assert found.kind is InboundClass.CONTROL
        assert found.target is None

    def test_a_command_word_inside_a_sentence_is_ordinary_words(self) -> None:
        """The collision is exact-match only; the guard must not swallow speech."""
        found = router((CODEX, "port the log")).classify("stop after the tests pass")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "stop after the tests pass"

    def test_a_named_command_word_is_unambiguous_and_goes_through(self) -> None:
        found = router((CODEX, "port the log")).classify("@log: stop")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "stop"

    def test_the_collision_only_exists_where_the_word_is_registered(self) -> None:
        found = router((CODEX, "port the log")).classify("continue")

        assert found.kind is InboundClass.ANSWER_RELAY


class TestTheGrammarIsConfiguration:
    def test_the_markers_are_injected_rather_than_baked_in(self) -> None:
        custom = InboundRouter(
            sessions=registry_of((CODEX, "port the log")),
            grammar=TextGrammar(
                control_prefix="!",
                delegate_prefix="~",
                relay_marker="#",
                control_commands=COMMANDS,
            ),
        )

        assert custom.classify("!status").kind is InboundClass.CONTROL
        assert custom.classify("~summarise").kind is InboundClass.DELEGATION
        assert custom.classify("#log: ship it").kind is InboundClass.ANSWER_RELAY
        assert custom.classify("/status").kind is InboundClass.ANSWER_RELAY


# ----------------------------------------------------------------------
# The reply grammar (ADR 0021 §2, §3; #263): a reply is targeted by the message it
# answers, and a numeral picks that message's option.
# ----------------------------------------------------------------------


def anchored(
    *sessions: tuple[SessionTarget, str],
    rows: tuple[tuple[tuple[str, ...], Anchor], ...] = (),
    answerable: set[SessionTarget] | None = None,
    asking: dict[SessionTarget, tuple[str, ...]] | None = None,
    cap: int = 100,
) -> tuple[InboundRouter, AnchorTable]:
    """A router over a registry, an Anchor Table and the Agent seam's answerable fact.

    `asking` puts a Session's roster row at `WAITING` on a question with those
    option labels — what a Stop carrying a question does through the hub.
    """
    table = AnchorTable(rows_per_target=cap)
    for ids, row in rows:
        table.register(ids, row)
    registry = registry_of(*sessions)
    for target, labels in (asking or {}).items():
        registry.set_stop_reading(
            target,
            waiting_for=WaitingFor(
                kind=WaitingKind.QUESTION,
                prompt="Which?",
                options=tuple(Option(text=label) for label in labels),
            ),
            progress=ProgressObservation(),
            now=1.0,
        )
    can_answer = answerable or set()
    router = InboundRouter(
        sessions=registry,
        grammar=TextGrammar(control_commands=COMMANDS),
        anchors=table,
        answerable=lambda target: target in can_answer,
    )
    return router, table


def question(target: SessionTarget, *options: str, at: float = 1.0) -> Anchor:
    return Anchor(kind=AnchorKind.NOTICE, target=target, options=options, sent_at=at)


def permission(target: SessionTarget, approval_id: str, at: float = 1.0) -> Anchor:
    return Anchor(
        kind=AnchorKind.NOTICE,
        target=target,
        options=(str(ApprovalVerdict.ALLOW), str(ApprovalVerdict.DENY)),
        approval_id=approval_id,
        sent_at=at,
    )


def receipt(target: SessionTarget, at: float = 1.0) -> Anchor:
    return Anchor(kind=AnchorKind.RECEIPT, target=target, sent_at=at)


TWO = ((CODEX, "port the log"), (CLAUDE, "build the shell"))


class TestAReplyIsTargetedByTheMessageItAnswers:
    def test_words_replying_to_a_known_anchor_go_to_that_anchors_session(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main", "feature")),))

        found = router.classify("go with the second one", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE
        assert found.text == "go with the second one"

    def test_a_reply_to_any_part_of_a_split_send_reaches_the_same_session(self) -> None:
        router, _ = anchored(*TWO, rows=((("40", "41"), question(CLAUDE, "main")),))

        assert router.classify("yes", in_reply_to="41").target == CLAUDE

    def test_words_replying_to_an_unknown_anchor_take_the_newest_anchor_path(self) -> None:
        """Pre-restart id, a fallen-out row, the user's own message: all the same."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify("yes", in_reply_to="7")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE

    def test_words_replying_to_nothing_go_to_the_newest_anchors_session(self) -> None:
        router, _ = anchored(
            *TWO,
            rows=((("1",), question(CODEX, "main", at=1.0)), (("2",), receipt(CLAUDE, at=2.0))),
        )

        found = router.classify("yes")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE

    def test_with_an_empty_table_two_live_sessions_still_ask_which(self) -> None:
        router, _ = anchored(*TWO)

        found = router.classify("yes")

        assert found.kind is InboundClass.UNKNOWN
        assert "port the log" in found.reply and "build the shell" in found.reply

    def test_with_an_empty_table_one_live_session_still_receives_bare_words(self) -> None:
        router, _ = anchored((CODEX, "port the log"))

        assert router.classify("yes").target == CODEX

    def test_the_named_form_survives_as_a_fallback_at_top_level(self) -> None:
        router, _ = anchored(*TWO, rows=((("1",), question(CLAUDE, "main")),))

        found = router.classify("@port the log: yes")

        assert found.target == CODEX

    def test_a_row_that_fell_out_of_the_table_is_an_unknown_anchor(self) -> None:
        router, table = anchored(*TWO, rows=((("1",), question(CODEX, "main")),), cap=1)
        table.register(("2",), question(CODEX, "a", "b"))

        found = router.classify("1", in_reply_to="1")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT


class TestANumeralPicksThatAnchorsOption:
    def test_a_numeral_on_an_answerable_question_relays_the_nth_label(self) -> None:
        router, _ = anchored(
            *TWO,
            rows=((("40",), question(CLAUDE, "main", "feature")),),
            asking={CLAUDE: ("main", "feature")},
            answerable={CLAUDE},
        )

        found = router.classify("2", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE
        assert found.text == "feature"

    def test_a_numeral_after_the_question_was_answered_on_screen_relays_nothing(self) -> None:
        """The roster still shows the question; the lane no longer holds its hook."""
        router, _ = anchored(
            *TWO,
            rows=((("40",), question(CLAUDE, "main", "feature")),),
            asking={CLAUDE: ("main", "feature")},
        )

        found = router.classify("2", in_reply_to="40")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == QUESTION_ALREADY_ANSWERED_HINT

    def test_a_numeral_on_an_older_question_never_answers_the_newer_one(self) -> None:
        """Q1 answered at the terminal, Q2 now held: "2" on Q1's notice is refused,
        not relayed as Q1's label into Q2 with the user's authority (review finding)."""
        router, _ = anchored(
            *TWO,
            rows=(
                (("40",), question(CLAUDE, "keep", "replace", "skip")),
                (("41",), question(CLAUDE, "yes", "no")),
            ),
            asking={CLAUDE: ("yes", "no")},
            answerable={CLAUDE},
        )

        found = router.classify("2", in_reply_to="40")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == QUESTION_ALREADY_ANSWERED_HINT
        assert router.classify("2", in_reply_to="41").text == "no"

    def test_a_numeral_on_a_question_the_session_has_moved_past_is_refused(self) -> None:
        """The row is a question's; the roster row is running again."""
        router, _ = anchored(
            *TWO, rows=((("40",), question(CLAUDE, "main", "feature")),), answerable={CLAUDE}
        )

        assert router.classify("1", in_reply_to="40").reply == QUESTION_ALREADY_ANSWERED_HINT

    def test_free_text_on_that_same_anchor_is_never_checked(self) -> None:
        """Words are relayed as words; whether they land is the Relay pipeline's."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main", "feature")),))

        found = router.classify("feature, please", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "feature, please"

    def test_a_numeral_past_the_last_option_is_refused(self) -> None:
        router, _ = anchored(
            *TWO,
            rows=((("40",), question(CLAUDE, "main", "feature")),),
            asking={CLAUDE: ("main", "feature")},
            answerable={CLAUDE},
        )

        assert router.classify("3", in_reply_to="40").reply == NUMERAL_PICKS_NOTHING_HINT
        assert router.classify("0", in_reply_to="40").reply == NUMERAL_PICKS_NOTHING_HINT

    def test_a_numeral_on_a_receipt_anchor_is_refused(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), receipt(CLAUDE)),), answerable={CLAUDE})

        found = router.classify("1", in_reply_to="40")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_PICKS_NOTHING_HINT

    def test_a_numeral_on_an_unknown_anchor_is_refused_never_retargeted(self) -> None:
        """Core no longer knows that row's options; "2" against the newest anchor
        would pick a different message's second option (ADR 0021 §2)."""
        router, _ = anchored(
            *TWO, rows=((("40",), question(CLAUDE, "main", "feature")),), answerable={CLAUDE}
        )

        found = router.classify("2", in_reply_to="pre-restart")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT

    def test_a_numeral_replying_to_nothing_is_refused_the_same_way(self) -> None:
        """Ruling (advisor, 2026-09-06): a numeral must reply to the message it
        answers. The newest-anchor rule is for words only."""
        router, _ = anchored(
            *TWO, rows=((("40",), question(CLAUDE, "main", "feature")),), answerable={CLAUDE}
        )

        found = router.classify("2")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT

    def test_a_numeral_with_an_empty_table_is_refused_even_with_one_live_session(self) -> None:
        router, _ = anchored((CODEX, "port the log"))

        found = router.classify("1")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT

    def test_a_digit_string_int_refuses_is_words_not_a_traceback(self) -> None:
        """CPython caps int() at 4300 digits; the event must still be answered (#211)."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify("9" * 5000, in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY

    def test_a_numeral_is_whole_digits_only(self) -> None:
        """ "2nd" and "2 please" are words, not a pick."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main", "feature")),))

        assert router.classify("2nd", in_reply_to="40").kind is InboundClass.ANSWER_RELAY
        assert router.classify("2 please", in_reply_to="40").kind is InboundClass.ANSWER_RELAY


class TestANumeralOnAPermissionIsAVerdict:
    def test_one_is_allow_on_the_rows_approval_id(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), permission(CLAUDE, "p-1")),))

        found = router.classify("1", in_reply_to="40")

        assert found.kind is InboundClass.APPROVAL_RELAY
        assert found.target == CLAUDE
        assert found.approval_id == "p-1"
        assert found.verdict is ApprovalVerdict.ALLOW

    def test_two_is_deny(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), permission(CLAUDE, "p-1")),))

        assert router.classify("2", in_reply_to="40").verdict is ApprovalVerdict.DENY

    def test_three_on_a_two_option_permission_is_refused(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), permission(CLAUDE, "p-1")),))

        found = router.classify("3", in_reply_to="40")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply == NUMERAL_PICKS_NOTHING_HINT

    def test_a_verdict_is_never_subject_to_the_question_check(self) -> None:
        """A permission has no held question; whether the dialog is still open is
        the Approval Relay's own answer (`answer_approval`)."""
        router, _ = anchored(*TWO, rows=((("40",), permission(CLAUDE, "p-1")),), answerable=set())

        assert router.classify("1", in_reply_to="40").kind is InboundClass.APPROVAL_RELAY

    def test_words_on_a_permission_notice_are_words_for_that_session(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), permission(CLAUDE, "p-1")),))

        found = router.classify("why do you need that?", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE


class TestTheMarkersInsideAReply:
    def test_a_slash_line_inside_a_reply_is_relayed_inside_the_wrapper(self) -> None:
        """`/` at top level is the control plane; `/x` inside a reply belongs to the
        Session, wrapped in the words that name the act (#248). No pre-check of the
        name (#256): an unknown skill reaches the Session verbatim."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify("/no-such-skill util.py --fast", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE
        assert found.text == "run the skill /no-such-skill with arguments: util.py --fast"

    def test_a_slash_line_with_no_arguments_names_only_the_skill(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        assert router.classify("/review", in_reply_to="40").text == "run the skill /review"

    def test_a_registered_command_inside_a_reply_is_still_the_sessions(self) -> None:
        """`/status` in a reply is not the control plane's; the reply decided whose it is."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify("/status", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "run the skill /status"

    def test_a_slash_at_top_level_is_still_the_control_plane(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        assert router.classify("/status").kind is InboundClass.CONTROL

    def test_a_delegate_marker_inside_a_reply_is_the_users_words(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify(">what did you change", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == ">what did you change"

    def test_a_delegate_marker_at_top_level_is_still_a_delegation(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        assert router.classify(">what changed").kind is InboundClass.DELEGATION

    def test_a_bare_command_word_inside_a_reply_is_words_not_a_collision(self) -> None:
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        found = router.classify("stop", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "stop"

    def test_a_bare_command_word_after_an_anchor_is_still_the_collision(self) -> None:
        """Top level keeps both honest readings: nothing was replied to."""
        router, _ = anchored(*TWO, rows=((("40",), question(CLAUDE, "main")),))

        assert router.classify("stop").kind is InboundClass.UNKNOWN


class TestARowWhoseSessionTheRosterNoLongerHolds:
    """Between two discovery passes a Codex row can be re-keyed (#73, #77) or end;
    its rows are an unknown Anchor until the pass trims them."""

    GONE = SessionTarget(agent=AgentKind.CODEX, session_id="re-keyed-away")

    def test_words_replying_to_it_take_the_unknown_anchor_path(self) -> None:
        router, _ = anchored((CODEX, "port the log"), rows=((("40",), question(self.GONE, "a")),))

        found = router.classify("yes", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX

    def test_a_numeral_on_it_is_refused(self) -> None:
        router, _ = anchored(
            (CODEX, "port the log"),
            rows=((("40",), question(self.GONE, "a")),),
            answerable={self.GONE},
        )

        assert router.classify("1", in_reply_to="40").reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT

    def test_as_the_newest_anchor_it_is_skipped_and_todays_rule_stands(self) -> None:
        router, _ = anchored((CODEX, "port the log"), rows=((("40",), question(self.GONE, "a")),))

        assert router.classify("yes").target == CODEX

    def test_an_ended_sessions_row_is_skipped_too(self) -> None:
        registry = registry_of(*TWO)
        registry.mark_ended(CLAUDE)
        table = AnchorTable(rows_per_target=100)
        table.register(("40",), question(CLAUDE, "a"))
        router = InboundRouter(
            sessions=registry, grammar=TextGrammar(control_commands=COMMANDS), anchors=table
        )

        assert router.classify("yes", in_reply_to="40").target == CODEX
        assert router.classify("yes").target == CODEX


class TestARouterWithoutATable:
    def test_a_router_built_with_no_table_keeps_todays_rules(self) -> None:
        found = router((CODEX, "port the log")).classify("yes", in_reply_to="40")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX

    def test_a_numeral_with_no_table_is_refused(self) -> None:
        found = router((CODEX, "port the log")).classify("1", in_reply_to="40")

        assert found.reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT


def roster(*targets: SessionTarget, at: float = 1.0) -> Anchor:
    return Anchor(
        kind=AnchorKind.MENU,
        target=Screen.ROSTER,
        options=tuple(f"name {index}" for index in range(len(targets))),
        picks=targets,
        sent_at=at,
    )


def greeting(target: SessionTarget, at: float = 1.0) -> Anchor:
    return Anchor(
        kind=AnchorKind.MENU,
        target=target,
        options=("brief", "history", "send message"),
        picks=("brief", "history", "send_message"),
        sent_at=at,
    )


class TestANumeralOnAMenuScreenIsAPick:
    """ADR 0021 §6 (#264): every menu screen is an Anchor whose labels resolve by
    position. The router names the pick — the row and the position — and
    Bridge Core decides what the press does."""

    def test_a_numeral_on_the_roster_picks_that_position(self) -> None:
        router, _ = anchored(*TWO, rows=((("50",), roster(CODEX, CLAUDE)),))

        found = router.classify("2", in_reply_to="50")

        assert found.kind is InboundClass.MENU_PICK
        assert found.position == 2
        assert found.anchor is not None and found.anchor.target is Screen.ROSTER
        assert found.text == "name 1"

    def test_the_pick_is_by_position_never_by_the_labels_text(self) -> None:
        """Two rows sharing a label are two picks; the label decides nothing."""
        row = Anchor(
            kind=AnchorKind.MENU,
            target=Screen.ROSTER,
            options=("same", "same"),
            picks=(CODEX, CLAUDE),
            sent_at=1.0,
        )
        router, _ = anchored(*TWO, rows=((("50",), row),))

        assert router.classify("2", in_reply_to="50").anchor.picks[1] == CLAUDE  # type: ignore[union-attr]

    def test_a_numeral_past_the_last_label_is_refused(self) -> None:
        router, _ = anchored(*TWO, rows=((("50",), roster(CODEX, CLAUDE)),))

        assert router.classify("3", in_reply_to="50").reply == NUMERAL_PICKS_NOTHING_HINT
        assert router.classify("0", in_reply_to="50").reply == NUMERAL_PICKS_NOTHING_HINT

    def test_words_replying_to_a_screen_are_words_that_replied_to_nothing(self) -> None:
        """A screen is nobody's: there is no Session for the words to be for."""
        router, _ = anchored((CODEX, "port the log"), rows=((("50",), roster(CODEX)),))

        found = router.classify("carry on", in_reply_to="50")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX

    def test_a_command_replying_to_a_screen_is_still_a_command(self) -> None:
        router, _ = anchored((CODEX, "port the log"), rows=((("50",), roster(CODEX)),))

        assert router.classify("/status", in_reply_to="50").kind is InboundClass.CONTROL

    def test_a_numeral_on_a_greeting_picks_that_sessions_choice(self) -> None:
        router, _ = anchored(*TWO, rows=((("51",), greeting(CLAUDE)),))

        found = router.classify("3", in_reply_to="51")

        assert found.kind is InboundClass.MENU_PICK
        assert found.target == CLAUDE
        assert found.position == 3
        assert found.text == "send message"

    def test_words_replying_to_a_greeting_are_words_for_that_session(self) -> None:
        router, _ = anchored(*TWO, rows=((("51",), greeting(CLAUDE)),))

        found = router.classify("ship it", in_reply_to="51")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE

    def test_a_numeral_on_a_greeting_whose_session_ended_is_refused(self) -> None:
        registry = registry_of(*TWO)
        registry.mark_ended(CLAUDE)
        table = AnchorTable(rows_per_target=100)
        table.register(("51",), greeting(CLAUDE))
        router = InboundRouter(
            sessions=registry, grammar=TextGrammar(control_commands=COMMANDS), anchors=table
        )

        assert router.classify("1", in_reply_to="51").reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT

    def test_a_numeral_on_an_expired_screen_gets_the_one_fixed_hint(self) -> None:
        """The roster fell out of the table: the press is refused, never re-targeted."""
        older, newer = (("49",), roster(CODEX, CLAUDE)), (("50",), roster(CODEX, CLAUDE, at=2.0))
        router, table = anchored(*TWO, rows=(older, newer), cap=1)
        assert table.lookup("49") is None  # evicted by the newer roster

        assert router.classify("1", in_reply_to="49").reply == NUMERAL_NEEDS_A_KNOWN_ANCHOR_HINT
        assert router.classify("1", in_reply_to="50").kind is InboundClass.MENU_PICK

    def test_a_numeral_on_the_prompt_picks_nothing(self) -> None:
        prompt = Anchor(kind=AnchorKind.PROMPT, target=CLAUDE, sent_at=1.0)
        router, _ = anchored(*TWO, rows=((("52",), prompt),))

        assert router.classify("1", in_reply_to="52").reply == NUMERAL_PICKS_NOTHING_HINT

    def test_words_replying_to_the_prompt_are_words_for_that_session(self) -> None:
        prompt = Anchor(kind=AnchorKind.PROMPT, target=CLAUDE, sent_at=1.0)
        router, _ = anchored(*TWO, rows=((("52",), prompt),))

        found = router.classify("ship it", in_reply_to="52")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE

    def test_a_numeral_on_a_history_page_picks_nothing(self) -> None:
        page = Anchor(kind=AnchorKind.HISTORY, target=CLAUDE, sent_at=1.0)
        router, _ = anchored(*TWO, rows=((("53",), page),))

        assert router.classify("1", in_reply_to="53").reply == NUMERAL_PICKS_NOTHING_HINT
        assert router.classify("more", in_reply_to="53").target == CLAUDE
