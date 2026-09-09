"""The wire vocabulary: closed, and the same shape in both directions.

These are the assertions the Swift shell will implement against, so they are
about the *contract* — the action set, the error set, and the fact that a
request or reply survives the round trip through a plain JSON document — never
about sockets, which are `control_plane/`'s business.
"""

from __future__ import annotations

import json

import pytest

from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    MENU,
    PROTOCOL_VERSION,
    USAGE,
    Action,
    ErrorCode,
    MalformedRequest,
    Reader,
    Reply,
    Request,
)


class TestTheActionSet:
    def test_the_action_set_is_exactly_these(self) -> None:
        assert {str(action) for action in Action} == {
            "status",
            "switch",
            "brief",
            "history",
            "live",
            "relay",
            "approve",
            "verify",
            "sessions",
            "config",
            "assistant",
        }

    def test_the_reader_mark_moved_the_protocol_to_ten(self) -> None:
        """A surface can tell an engine that knows the mark from one that does not.

        Protocol 9 added the three menu verbs (#264). Ten adds the optional
        `reader` field: an engine at 9 answers a marked `history` with a page
        fitted to the Control Plane's 64 KB and nothing else, which codex then
        cuts (#302). The Swift shell compares this number and nothing else, so
        the field cannot arrive under an unchanged version.
        """
        assert PROTOCOL_VERSION >= 10

    def test_the_new_state_word_moves_the_protocol_to_eleven(self) -> None:
        """A closed set that changed under an unchanged number is a gate that lies.

        `state` is what a surface lights a symbol on. #320 removed `unreadable`
        and added `waiting_on`, so a version-10 surface would meet a word it has
        no light for — and draw nothing where the user reads the one fact they
        act on. Same rule as the two above, applied to a value set rather than
        to an action set or a field.
        """
        assert PROTOCOL_VERSION == 11

    def test_the_three_menu_verbs_moved_the_protocol_to_nine(self) -> None:
        """A v8 surface would send `sessions` and be answered `unknown_action`.

        The Swift shell compares this number and nothing else, so an action set
        that grew under an unchanged number is a gate that lies (#264, ADR 0021
        §6). Protocol 8 retired `pending_approvals` from `status` (#191).
        """
        assert PROTOCOL_VERSION >= 9

    def test_the_retired_exact_progress_verb_is_not_an_action_this_engine_has(self) -> None:
        """Retired with the History page, and its absence asserted, not assumed."""
        assert "progress" not in {str(action) for action in Action}

    def test_the_sessions_verb_is_the_menu_screen_and_not_the_retired_roster(self) -> None:
        """`sessions` came back in protocol 9 as a screen, not as the rendered-row roster.

        Protocol 6 retired a `sessions` that was a second vocabulary for what
        Briefing says once. The one here answers with Briefing's own roster text
        and the option labels a menu draws (ADR 0021 §6); its usage line takes
        no argument, because a screen is opened and never addressed.
        """
        assert USAGE[Action.SESSIONS] == "sessions"
        assert USAGE[Action.CONFIG] == "config"
        assert USAGE[Action.ASSISTANT] == "assistant"

    def test_every_action_has_one_usage_line(self) -> None:
        assert set(USAGE) == set(Action)

    def test_the_command_menu_is_exactly_these_four_in_this_order(self) -> None:
        """What a surface with a command menu advertises: four, and nothing it cannot parse.

        The menu is vocabulary the surface shares with the engine, not a
        Telegram-private list (ADR 0021 §6): every entry is an action the shared
        parser accepts, and its description is a sentence Core chose.
        """
        assert tuple(MENU) == (Action.ASSISTANT, Action.SESSIONS, Action.STATUS, Action.CONFIG)
        assert set(MENU) <= set(Action)
        assert all(description.strip() for description in MENU.values())

    def test_launching_and_closing_are_not_actions_this_engine_has(self) -> None:
        """Parked with the launcher (#72), and their absence is asserted, not assumed.

        An action that quietly came back would be a wire this engine answers on
        with nothing behind it. A surface still sending either gets
        `unknown_action`, which is the honest answer and the reason the protocol
        version moved with them.
        """
        assert {"launch", "close"} & {str(action) for action in Action} == set()
        assert {"launch_failed", "close_failed"} & {str(code) for code in ErrorCode} == set()

    def test_no_legacy_alias_survives(self) -> None:
        """The old CLI carried both a legacy and a current Stop command."""
        assert "duty_toggle" not in {str(action) for action in Action}
        assert "overview" not in {str(action) for action in Action}


class TestARequestOnTheWire:
    def test_survives_the_round_trip(self) -> None:
        request = Request(action=Action.SWITCH, payload={"name": "duty", "on": True})
        assert Request.of(json.loads(json.dumps(request.as_document()))) == request

    def test_an_unknown_action_is_refused_rather_than_carried(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "duty_toggle"})
        assert refusal.value.code is ErrorCode.UNKNOWN_ACTION

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of(["status"])
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST

    def test_a_payload_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "status", "payload": "duty"})
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST

    def test_a_missing_payload_is_an_empty_one(self) -> None:
        assert Request.of({"action": "status"}).payload == {}


class TestTheReaderMark:
    """#302: who the answer is *for*, when that changes what may be carried."""

    def test_the_reader_set_is_exactly_the_voice(self) -> None:
        """One value today. The set is closed, so a second reader is a decision."""
        assert {str(reader) for reader in Reader} == {"voice"}

    def test_a_request_without_the_mark_carries_no_reader(self) -> None:
        """The Companion Channel's shape, and every `bridgectl` that never marks."""
        assert Request.of({"action": "history"}).reader is None

    def test_an_absent_mark_survives_the_round_trip_as_an_absent_field(self) -> None:
        """A request that names no reader does not grow a null one to be read back."""
        assert "reader" not in Request(action=Action.HISTORY).as_document()

    def test_the_mark_survives_the_round_trip(self) -> None:
        document = Request(action=Action.HISTORY, reader=Reader.VOICE).as_document()
        assert document["reader"] == "voice"
        assert Request.of(document).reader is Reader.VOICE

    def test_an_explicit_null_reader_is_read_as_no_reader(self) -> None:
        """How a surface that builds its request from a nullable field writes it."""
        assert Request.of({"action": "history", "reader": None}).reader is None

    def test_an_unrecognised_reader_is_refused_and_the_refusal_names_the_values(self) -> None:
        """A mistyped reader is never silently the default (#302).

        Malformed rather than an unusable payload: the field is the request's,
        not one action's, so it is read where the request is read.
        """
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "history", "reader": "the-voice"})
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST
        assert "voice" in str(refusal.value)

    def test_a_reader_that_is_not_text_is_refused(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "history", "reader": 1})
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST

    def test_two_requests_differing_only_in_reader_are_not_equal(self) -> None:
        """The mark changes the answer, so it is part of what a request *is*."""
        assert Request(action=Action.HISTORY) != Request(action=Action.HISTORY, reader=Reader.VOICE)


class TestAReplyOnTheWire:
    def test_the_current_protocol_includes_the_version_four_action_removal(self) -> None:
        """Version 4 removed launch/close; every version since retains that."""
        assert PROTOCOL_VERSION >= 4
        assert {"launch", "close"} & {str(action) for action in Action} == set()

    def test_an_answer_carries_the_action_it_answers_and_the_protocol_version(self) -> None:
        document = Reply.answered(Action.STATUS, {"call_id": None}).as_document()
        assert document["ok"] is True
        assert document["action"] == "status"
        assert document["protocol"] == PROTOCOL_VERSION
        assert document["data"] == {"call_id": None}

    def test_a_refusal_carries_the_code_and_the_refusals_own_words(self) -> None:
        document = Reply.refused(
            Action.SWITCH, ErrorCode.UNKNOWN_SWITCH, "unknown switch: 'sound'"
        ).as_document()
        assert document["ok"] is False
        assert document["error"] == {
            "code": "unknown_switch",
            "message": "unknown switch: 'sound'",
        }
        assert "data" not in document

    def test_a_refusal_may_answer_no_action_at_all(self) -> None:
        """A line that was never valid JSON names no action, and says so."""
        document = Reply.refused(None, ErrorCode.MALFORMED_REQUEST, "not JSON").as_document()
        assert document["action"] is None

    def test_a_refusal_must_carry_words_to_render(self) -> None:
        with pytest.raises(ValueError):
            Reply.refused(Action.STATUS, ErrorCode.REFUSED, "   ")

    def test_a_reply_survives_the_round_trip(self) -> None:
        reply = Reply.refused(Action.RELAY, ErrorCode.STALE_SESSION, "that Session is ended")
        assert Reply.of(json.loads(json.dumps(reply.as_document()))) == reply


class TestTheBound:
    def test_the_request_bound_is_stated_on_the_contract(self) -> None:
        """Both sides must agree on it, so neither side may invent it."""
        assert MAX_REQUEST_BYTES > 0
