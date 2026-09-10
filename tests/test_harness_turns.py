"""The three turns and the four remaining items, checked at CI speed (#353).

The turns themselves need this machine, these agents and those bots. What is
ordinary code — and therefore pinned here — is everything the turns are *read*
by (`docs/acceptance-design.md` §2, §3):

* `TestTheTurns` — §3's three sentences, their files and their words, distinct
  so no effect can be mistaken for another;
* `TestTheEngineLogLine` — the `message_ids=` line to the id every chat read
  starts from (§2 item 2, §9);
* `TestTheReceipts` — `relay` and `approve` graded on the `grade` **field**,
  literally `delivered`, never on a substring that a `retained` receipt would
  false-pass (§2 item 3, #71);
* `TestTheApprovalId` — the permission read from the `status` payload's
  `waiting_for.approval_id` and never from `state` (§2 item 4, #191);
* `TestTheReplyAnchor` — an inbound send with no reply id is impossible by
  construction (§2 item 5, ADR 0021 §2–§3);
* `TestTheCredentialScan` — §8's rule read at the end of a run rather than only
  pinned by a fast test (the deferred item of #351, placed on #353);
* `TestWalkingTheFourItems` — each item's own binary check, and what a failure
  does to the rows behind it.

Nothing here starts an engine, opens a chat or spends a turn.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import deadlines
import hand_started
import items
import journey
import pytest
import support

from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relays import receipt_line
from gpt_voicecoding.installation import claude_hooks
from gpt_voicecoding.seams.agent import WaitingKind
from gpt_voicecoding.seams.delivery import Delivery

CLAUDE_LANE, CODEX_LANE = items.LANES

#: The agent's own transcript, where the agent writes one: `codex` names its
#: rollout on `GroundTruth.record` and `claude agents --json` names none.
ROLLOUT = Path("/runs/rollouts/rollout-thread-1.jsonl")

#: The address of the Session every fake here stands for — `_payload`'s row, the
#: `relay` target, and the `target=` field on a send line that is this lane's own
#: (#355). Spelled from the roster row's three fields, because that is the join
#: the harness makes: a send is this turn's Stop when its address is the one on
#: the row.
OWN_ADDRESS = f"{CODEX_LANE}:thread-1:7"

#: A Session on the same machine that is **not** this lane's — the other lane's,
#: or the operator's own. Run `20260910T191643Z`'s real one, abbreviated.
ANOTHER_ADDRESS = f"{CODEX_LANE}:01a08cc0-f956-7c63-ae40-95e80334cb9b:68569"


def _journal(tmp_path: Path) -> support.Journal:
    return support.Journal(tmp_path / support.JOURNAL_NAME)


class TestTheTurns:
    """§3 — one sentence each, one file each, and nothing shared between them."""

    def test_the_acknowledge_turn_is_the_spec_sentence_and_asks_for_no_tool(self) -> None:
        assert journey.ACKNOWLEDGE_WORDS == "reply with the single word READY, no tools"

    def test_each_turn_that_writes_names_its_own_file_and_its_own_word(self) -> None:
        assert journey.RELAY_FILE == "relay.txt"
        assert journey.RELAY_WORD == "BRAVO"
        assert journey.INBOUND_FILE == "inbound.txt"
        assert journey.INBOUND_WORD == "CHARLIE"

    def test_no_two_turns_share_a_file_or_a_word(self) -> None:
        """Distinct on purpose: a shared effect makes one item's failure another's."""
        assert journey.RELAY_FILE != journey.INBOUND_FILE
        assert journey.RELAY_WORD != journey.INBOUND_WORD

    def test_a_turn_that_writes_is_one_sentence_naming_the_path_and_the_word(self) -> None:
        words = journey.write_words(Path("/runs/w/relay.txt"), journey.RELAY_WORD)
        assert "/runs/w/relay.txt" in words
        assert journey.RELAY_WORD in words
        assert "\n" not in words

    def test_the_relay_turn_writes_inside_the_workspace_where_nothing_is_sandboxed(self) -> None:
        """§3: the Claude lane's write lands in the workspace."""
        workspace = Path("/runs/workspace-claude")
        assert (
            journey.relay_target_file(
                journey.lane(CLAUDE_LANE), workspace=workspace, run_directory=Path("/runs")
            )
            == workspace / journey.RELAY_FILE
        )

    def test_the_relay_turn_writes_outside_the_workspace_where_the_sandbox_refuses(self) -> None:
        """§3 and #105: on the Codex lane the path is the one variable that raises the
        permission, so it sits outside every writable root — under the run directory,
        in a directory of its own rather than in the list §7 draws."""
        written = journey.relay_target_file(
            journey.lane(CODEX_LANE),
            workspace=Path("/runs/workspace-codex"),
            run_directory=Path("/runs"),
        )
        assert written.name == journey.RELAY_FILE
        assert not written.is_relative_to(Path("/runs/workspace-codex"))
        assert written.is_relative_to(Path("/runs"))
        assert written.parent != Path("/runs")

    def test_the_inbound_turn_writes_inside_the_workspace_on_either_lane(self) -> None:
        """Item 5 is graded by the file alone and raises no permission (§2 item 5)."""
        workspace = Path("/runs/workspace-codex")
        assert journey.inbound_target_file(workspace) == workspace / journey.INBOUND_FILE

    def test_only_the_codex_lane_is_sandboxed(self) -> None:
        assert not journey.lane(CLAUDE_LANE).sandboxed
        assert journey.lane(CODEX_LANE).sandboxed


class TestTheEngineLogLine:
    """§2 item 2 and §9 — every chat read starts from an id the product issued,
    and only from a line addressed to this lane's own Session (#355)."""

    SENT = (
        f"2026-09-10 10:00:00 INFO sent Companion Channel message request=r-1 "
        f"outcome=delivered target={OWN_ADDRESS} message_ids=4110,4111"
    )
    #: The same line, for the Session the *other* lane started. This is the line
    #: that was graded as this lane's Stop on run `20260910T191643Z`, 0.16 s
    #: before this lane's own turn was typed.
    THEIRS = (
        f"2026-09-10 10:00:01 INFO sent Companion Channel message request=r-9 "
        f"outcome=delivered target={ANOTHER_ADDRESS} message_ids=1806"
    )

    def test_the_ids_come_off_the_line_the_engine_wrote(self) -> None:
        assert journey.message_ids(self.SENT) == ("4110", "4111")

    def test_a_line_with_no_ids_yields_none(self) -> None:
        """A send that reached nobody carries an empty field, and that is an answer."""
        nobody = "sent Companion Channel message outcome=failed target= message_ids="
        assert journey.message_ids(nobody) == ()

    def test_a_line_that_is_not_a_send_carries_no_ids(self) -> None:
        assert journey.message_ids("Session stopped: 工位") == ()

    def test_the_address_comes_off_the_same_line(self) -> None:
        assert journey.send_target(self.SENT) == OWN_ADDRESS
        assert journey.send_target(self.THEIRS) == ANOTHER_ADDRESS

    def test_a_send_about_nobody_and_a_line_that_is_no_send_both_name_nobody(self) -> None:
        """An empty field is a send that named no Session — a `/status` answer, a
        refusal — and is never this turn's Stop, because an address is what makes
        a send this lane's own."""
        assert journey.send_target("sent Companion Channel message target= message_ids=1") == ""
        assert journey.send_target("Session stopped: 工位") == ""

    def test_the_stop_notice_id_is_the_first_id_of_the_first_send(self) -> None:
        """A notice can span several messages; the anchor the product registers
        covers every id of the receipt, so the first is the one this harness reads
        and the one it replies to (bridge.py's `anchors.register`)."""
        later = (
            f"sent Companion Channel message request=r-2 outcome=delivered "
            f"target={OWN_ADDRESS} message_ids=4200"
        )
        assert journey.stop_notice_id([self.SENT, later], OWN_ADDRESS) == "4110"

    def test_a_send_about_another_session_is_not_this_turns_stop(self) -> None:
        """#355: the newest send is not this lane's by virtue of being newest."""
        assert journey.stop_notice_id([self.THEIRS], OWN_ADDRESS) is None
        assert journey.stop_notice_id([self.THEIRS, self.SENT], OWN_ADDRESS) == "4110"
        assert journey.sent_for([self.THEIRS, self.SENT], OWN_ADDRESS) == [self.SENT]

    def test_a_send_about_nobody_is_not_this_turns_stop_either(self) -> None:
        nobody = (
            "sent Companion Channel message request=r-3 outcome=delivered target= message_ids=1"
        )
        assert journey.stop_notice_id([nobody], OWN_ADDRESS) is None

    def test_no_send_at_all_is_no_id(self) -> None:
        assert journey.stop_notice_id([], OWN_ADDRESS) is None

    def test_no_address_matches_nothing_rather_than_everything(self) -> None:
        """The empty field is what a send about nobody carries, so an equality test
        against an empty address would take every one of them (#355)."""
        nobody = (
            "sent Companion Channel message request=r-3 outcome=delivered target= message_ids=7"
        )
        assert journey.sent_for([nobody, self.SENT, self.THEIRS], "") == []
        assert journey.stop_notice_id([nobody], "") is None

    def test_a_failure_names_who_the_engine_did_send_about(self) -> None:
        """The one failure whose cause is invisible from everything else the row
        carries: a notice that came, addressed elsewhere."""
        said = journey.addressed_elsewhere([self.THEIRS], OWN_ADDRESS)
        assert ANOTHER_ADDRESS in said and OWN_ADDRESS in said

    def test_nothing_is_said_when_every_send_was_this_lanes_own(self) -> None:
        assert journey.addressed_elsewhere([self.SENT], OWN_ADDRESS) == ""
        assert journey.addressed_elsewhere([], OWN_ADDRESS) == ""


class TestTheReceipts:
    """§2 items 3 and 4 — the receipt is read as fields, and the field is `grade`."""

    def test_a_delivered_relay_receipt_is_delivered(self) -> None:
        line = receipt_line(
            state=str(Lifecycle.DELIVERED), grade=str(Delivery.DELIVERED), reason="delivered"
        )
        assert journey.receipt_fields(line)["grade"] == journey.DELIVERED
        assert journey.undelivered(line) is None

    def test_a_retained_receipt_is_not_delivered(self) -> None:
        """#71: `retained` is the receipt a substring on `delivered` false-passes."""
        line = receipt_line(
            state=str(Lifecycle.RETAINED), grade=str(Delivery.HELD), reason="held_far_side"
        )
        assert journey.undelivered(line) is not None

    def test_the_grade_is_read_as_a_field_and_never_as_a_substring(self) -> None:
        """Every other field may carry the word; only `grade` decides."""
        line = receipt_line(state=str(Lifecycle.DELIVERED), grade="none", reason="delivered")
        assert journey.undelivered(line) is not None

    def test_the_state_is_never_what_is_graded(self) -> None:
        line = receipt_line(state="delivered", grade=str(Delivery.HELD), reason="held_far_side")
        assert journey.undelivered(line) is not None

    def test_the_approve_reply_is_read_by_the_same_fields(self) -> None:
        """An Approval Relay is a Relay: `verdict=…` then the same three codes."""
        reply = "verdict=allow " + receipt_line(
            state=str(Lifecycle.DELIVERED), grade=str(Delivery.DELIVERED), reason="delivered"
        )
        assert journey.receipt_fields(reply)["verdict"] == "allow"
        assert journey.undelivered(reply) is None

    def test_a_reply_with_no_grade_at_all_is_not_delivered(self) -> None:
        assert journey.undelivered("the engine said something else entirely") is not None


class TestTheApprovalId:
    """§2 item 4 and #191 — the id comes off `waiting_for`, never off `state`."""

    def _row(self, **waiting: Any) -> dict[str, Any]:
        return {
            "target": {"agent": CODEX_LANE, "session_id": "thread-1", "pid": 7},
            "state": "running",
            "waiting_for": {"kind": str(WaitingKind.PERMISSION), "approval_id": None, **waiting},
        }

    def test_the_permission_is_read_from_waiting_for(self) -> None:
        assert journey.approval_id(self._row(approval_id="ap-1")) == "ap-1"

    def test_a_thread_that_is_still_running_can_still_hold_a_permission(self) -> None:
        """#191: a Codex thread stays `running` with its dialog on screen."""
        row = self._row(approval_id="ap-1")
        assert row["state"] == "running"
        assert journey.approval_id(row) == "ap-1"

    def test_a_row_with_no_permission_yields_none(self) -> None:
        assert journey.approval_id(self._row()) is None
        assert journey.approval_id({"state": "waiting"}) is None

    def test_what_the_session_stopped_on_is_the_kind_it_carries(self) -> None:
        assert journey.stopped_on(self._row()) == str(WaitingKind.PERMISSION)
        assert journey.stopped_on({"waiting_for": {"kind": str(WaitingKind.NONE)}}) == "none"

    def test_a_kind_nobody_can_say_yet_is_not_what_it_stopped_on(self) -> None:
        """`unknown` is "we cannot yet say", which is not an answer (seams/agent.py)."""
        assert journey.stopped_on({"waiting_for": {"kind": str(WaitingKind.UNKNOWN)}}) is None
        assert journey.stopped_on({"waiting_for": {}}) is None
        assert journey.stopped_on({}) is None


class TestTheReplyAnchor:
    """§2 item 5 — the primary inbound form, and no way to send any other."""

    def test_the_chat_has_two_operations_and_neither_is_a_search(self) -> None:
        public = {
            name
            for name, _ in inspect.getmembers(journey.Chat, inspect.isfunction)
            if not name.startswith("_")
        }
        assert public == {"read", "reply"}

    def test_a_reply_cannot_be_sent_without_the_id_it_is_anchored_to(self) -> None:
        """Impossible by construction: there is no `send`, and `reply` takes an anchor."""
        signature = inspect.signature(journey.Chat.reply)
        assert [name for name in signature.parameters][:3] == ["self", "anchor", "words"]
        assert all(
            parameter.default is inspect.Parameter.empty
            for name, parameter in signature.parameters.items()
            if name != "self"
        )

    def test_reading_is_by_one_id_through_the_lanes_own_peer(self) -> None:
        connection = _Connection()
        chat = journey.Chat(peer="@lane-bot", connection=connection)
        chat.read(4110)
        assert connection.reads == [("@lane-bot", 4110)]

    def test_replying_carries_the_anchor_to_the_client(self) -> None:
        connection = _Connection()
        journey.Chat(peer="@lane-bot", connection=connection).reply("4110", "write it")
        assert connection.replies == [("@lane-bot", 4110, "write it")]


class TestTheCredentialScan:
    """§8, read at the end of a run rather than only pinned by a fast test."""

    def test_the_secrets_are_both_lanes_tokens_and_the_api_hash(self) -> None:
        found = support.secrets_of(
            {"TG": "111:claude-token", "TG_2": "222:codex-token"},
            token_variables=("TG", "TG_2"),
            api_hash="deadbeef",
        )
        assert set(found) == {"111:claude-token", "222:codex-token", "deadbeef"}

    def test_the_api_id_is_not_a_secret_this_scan_reads(self) -> None:
        """A short integer matches any message id in the journal, and a false
        REFUSED costs the run its whole reason (§8's rule is about a *value*)."""
        signature = inspect.signature(support.secrets_of)
        assert "api_id" not in signature.parameters

    def test_an_unset_variable_contributes_nothing(self) -> None:
        assert support.secrets_of({}, token_variables=("TG",), api_hash=None) == ()

    def test_a_clean_run_says_so_and_keeps_its_result(self, tmp_path: Path) -> None:
        verdict = self._verdict(tmp_path)
        verdict.record(items.Item.PREFLIGHT, support.PASS, verdict.journal("preflight.passed"))
        verdict.scanned((), verdict.journal("credentials.scanned", artifacts=[]))
        assert verdict.document()["credentials"]["clean"] is True
        assert verdict.result is support.PASS

    def test_an_artifact_carrying_a_credential_makes_the_run_fail(self, tmp_path: Path) -> None:
        """The ruling of #353: FAIL, not REFUSED — REFUSED is preflight's word."""
        verdict = self._verdict(tmp_path)
        verdict.record(items.Item.PREFLIGHT, support.PASS, verdict.journal("preflight.passed"))
        verdict.scanned(
            ("engine-claude/config.toml carries a credential",),
            verdict.journal("credentials.scanned", artifacts=["engine-claude/config.toml"]),
        )
        document = verdict.document()
        assert document["credentials"]["clean"] is False
        assert document["credentials"]["artifacts"] == [
            "engine-claude/config.toml carries a credential"
        ]
        assert verdict.result is support.FAIL

    def test_the_scan_rests_on_a_journal_line_like_every_other_reading(
        self, tmp_path: Path
    ) -> None:
        verdict = self._verdict(tmp_path)
        verdict.scanned((), verdict.journal("credentials.scanned", artifacts=[]))
        evidence = verdict.document()["credentials"]["evidence"]
        assert support.resolve(tmp_path, evidence)["event"] == "credentials.scanned"

    def test_a_run_that_never_scanned_says_so_and_claims_nothing(self, tmp_path: Path) -> None:
        """Unscanned is neither clean nor dirty: a run whose secrets could not be
        assembled has not been *found* clean."""
        credentials = self._verdict(tmp_path).document()["credentials"]
        assert credentials["scanned"] is False
        assert credentials["clean"] is None

    def test_no_secrets_to_look_for_is_not_a_clean_tree(self, tmp_path: Path) -> None:
        """A run refused at §5's token-variable check has empty variables rather than
        missing ones, and a scan for zero values must not report a clean scan."""
        assert support.secrets_of({"TG": ""}, token_variables=("TG", "TG_2"), api_hash=None) == ()

    def test_the_reading_finds_a_token_in_any_artifact_under_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "engine-claude").mkdir()
        (tmp_path / "engine-claude" / "config.toml").write_text('token = "111:leaked"\n')
        assert support.scan_for_credentials(tmp_path, ("111:leaked",)) == (
            "engine-claude/config.toml carries a credential",
        )

    def _verdict(self, tmp_path: Path) -> support.Verdict:
        return support.Verdict(
            run_id="20260910T000000Z",
            selection=items.select(["preflight"]),
            lanes=(),
            journal=_journal(tmp_path),
        )


# --- walking the four items --------------------------------------------------


class _Connection:
    """The user-account client, as the two operations `Chat` asks it for."""

    def __init__(self, message: Any = None) -> None:
        self.reads: list[tuple[Any, int]] = []
        self.replies: list[tuple[Any, int, str]] = []
        self.message = message

    def read(self, peer: Any, message_id: int) -> Any:
        self.reads.append((peer, message_id))
        return self.message

    def reply(self, peer: Any, reply_to_message_id: int, text: str) -> Any:
        self.replies.append((peer, reply_to_message_id, text))
        return self.message


class _Message:
    """One message in the chat, as the client hands it over."""

    def __init__(self, identifier: int = 4110, text: str = "Session stopped: 工位") -> None:
        self.id = identifier
        self.text = text

    def as_journal_fields(self) -> dict[str, object]:
        return {"message_id": self.id, "direction": "received", "text": self.text}


class _Clock:
    """A clock that jumps a whole deadline every reading, so a wait runs out at once."""

    def __init__(self, step: float = max(deadlines.DEADLINES.values())) -> None:
        self.reading = 0.0
        self.step = step

    def __call__(self) -> float:
        answer = self.reading
        self.reading += self.step
        return answer


class _Surface:
    """A `bridgectl` that answers what a test arranged, and remembers what it was asked."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        replies: dict[str, str] | None = None,
        ok: bool = True,
    ) -> None:
        self.payload = payload or {"sessions": []}
        self.replies = replies or {}
        self.ok = ok
        self.calls: list[tuple[str, ...]] = []
        self.effects: dict[str, Any] = {}

    def __call__(self, *arguments: str, deadline: str | None = None) -> support.Answer:
        self.calls.append(arguments)
        effect = self.effects.get(arguments[0])
        if effect is not None:
            effect()
        spoken = self.replies.get(arguments[0], "done")
        return support.Answer(arguments, 0 if self.ok else 1, spoken, "refused")

    def status_payload(self, *, why: str) -> dict[str, Any]:
        return self.payload


class _Engine:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or []

    def log_lines(self) -> list[str]:
        return list(self.lines)


class _Session:
    """The pty, as the walk uses it: a turn is typed in, and nothing is parsed out."""

    def __init__(
        self,
        engine: _Engine | None = None,
        *,
        sends: str | None = None,
        transcript: Path = Path(f"/runs/pty-{CODEX_LANE}.log"),
        environment: dict[str, str] | None = None,
    ) -> None:
        self.engine = engine
        self.sends = sends
        self.transcript = transcript
        self.environment = environment or {}
        self.submitted: list[str] = []

    def submit(self, words: str) -> None:
        self.submitted.append(words)
        if self.engine is not None and self.sends is not None:
            self.engine.lines.append(self.sends)

    def screen_tail(self) -> str:
        return "<the screen, which nothing parses>"


DELIVERED_RECEIPT = receipt_line(
    state=str(Lifecycle.DELIVERED), grade=str(Delivery.DELIVERED), reason="delivered"
)
RETAINED_RECEIPT = receipt_line(
    state=str(Lifecycle.RETAINED), grade=str(Delivery.HELD), reason="held_far_side"
)
SENT_LINE = (
    f"sent Companion Channel message request=r-1 outcome=delivered "
    f"target={OWN_ADDRESS} message_ids=4110,4111"
)
#: The same send, about a Session this lane did not start (#355).
THEIRS_LINE = (
    f"sent Companion Channel message request=r-9 outcome=delivered "
    f"target={ANOTHER_ADDRESS} message_ids=1806"
)


def _payload(workspace: Path, **waiting: Any) -> dict[str, Any]:
    return {
        "sessions": [
            {
                "target": {"agent": CODEX_LANE, "session_id": "thread-1", "pid": 7},
                "workspace": str(workspace),
                "name": "工位",
                "state": "running",
                "waiting_for": {"kind": str(WaitingKind.NONE), "approval_id": None, **waiting},
            }
        ]
    }


def _walk(tmp_path: Path, lane: str = CODEX_LANE, **overrides: Any) -> journey.Walk:
    """One lane's walk over fakes, with the ground `roster` stands on arranged."""
    journal = _journal(tmp_path)
    workspace = tmp_path / f"workspace-{lane}"
    workspace.mkdir(exist_ok=True)
    verdict = support.Verdict(
        run_id=tmp_path.name,
        selection=overrides.pop("selection", items.select()),
        lanes=items.LANES,
        journal=journal,
    )
    arranged: dict[str, Any] = {
        "lane": journey.lane(lane),
        "now": _Clock(),
        "sleep": lambda _: None,
        "selection": verdict.selection,
        "journal": journal,
        "verdict": verdict,
        "bridgectl": _Surface(_payload(workspace)),
        "engine": _Engine(),
        "session": _Session(),
        "workspace": workspace,
        "run_directory": tmp_path,
        "chat": journey.Chat(peer="@lane-bot", connection=_Connection(_Message())),
        "truth": lambda: hand_started.GroundTruth(
            session_id="thread-1", pid=7, workspace=workspace, record=ROLLOUT
        ),
    }
    arranged.update(overrides)
    return journey.Walk(**arranged)


def _rows(walk: journey.Walk) -> dict[str, dict[str, Any]]:
    return {row["item"]: row for row in walk.verdict.document()["lanes"][walk.lane.name]}


class TestWalkingTheFourItems:
    """§2 items 2–5, each on its own binary check."""

    def test_the_chat_mark_is_taken_before_the_acknowledge_turn_is_typed(
        self, tmp_path: Path
    ) -> None:
        """§3: the mark is `stop notice`'s own, and it precedes the turn — a notice
        already in the log belongs to something else."""
        engine = _Engine(["sent Companion Channel message request=r-0 message_ids=4000"])
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.STOP_NOTICE)]
        assert row["verdict"] == "PASS"
        assert support.resolve(tmp_path, row["evidence"])["message_id"] == "4110"

    def test_the_stop_notice_is_re_read_once_by_the_id_the_product_issued(
        self, tmp_path: Path
    ) -> None:
        engine = _Engine()
        connection = _Connection(_Message())
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            chat=journey.Chat(peer="@lane-bot", connection=connection),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        assert connection.reads == [("@lane-bot", 4110)]
        assert _rows(walk)[str(items.Item.STOP_NOTICE)]["verdict"] == "PASS"

    def test_a_stop_notice_about_another_session_is_not_this_turns(self, tmp_path: Path) -> None:
        """#355: a send after the mark addressed to somebody else is not this turn's
        Stop, however new it is — and the row that never came says what did."""
        engine = _Engine()
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=THEIRS_LINE),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.STOP_NOTICE)]
        assert row["verdict"] == "FAIL"
        line = support.resolve(tmp_path, row["evidence"])
        assert line["event"] == "deadline.expired"
        assert ANOTHER_ADDRESS in line["what"]
        assert OWN_ADDRESS in line["what"]

    def test_a_message_the_chat_does_not_hold_is_a_failure(self, tmp_path: Path) -> None:
        engine = _Engine()
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            chat=journey.Chat(peer="@lane-bot", connection=_Connection(None)),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.STOP_NOTICE)]
        assert row["verdict"] == "FAIL"
        assert "4110" in support.resolve(tmp_path, row["evidence"])["why"]

    def test_a_session_that_says_nothing_about_what_it_stopped_on_is_a_failure(
        self, tmp_path: Path
    ) -> None:
        engine = _Engine()
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            bridgectl=_Surface(_payload(workspace, kind=str(WaitingKind.UNKNOWN))),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        assert _rows(walk)[str(items.Item.STOP_NOTICE)]["verdict"] == "FAIL"

    def test_the_relay_turn_is_sent_to_the_address_on_the_roster_row(self, tmp_path: Path) -> None:
        surface = _Surface(_payload(tmp_path / f"workspace-{CODEX_LANE}"))
        surface.replies["relay"] = DELIVERED_RECEIPT
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["relay"]))
        walk.walk()
        (relayed,) = [call for call in surface.calls if call[0] == "relay"]
        assert relayed[1] == f"{CODEX_LANE}:thread-1:7"
        assert journey.RELAY_WORD in relayed[2]
        assert _rows(walk)[str(items.Item.RELAY)]["verdict"] == "PASS"

    def test_a_retained_relay_receipt_fails_the_item(self, tmp_path: Path) -> None:
        surface = _Surface(_payload(tmp_path / f"workspace-{CODEX_LANE}"))
        surface.replies["relay"] = RETAINED_RECEIPT
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["relay"]))
        walk.walk()
        assert _rows(walk)[str(items.Item.RELAY)]["verdict"] == "FAIL"

    def test_the_permission_is_answered_and_the_file_is_what_settles_it(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        written = journey.relay_target_file(
            journey.lane(CODEX_LANE), workspace=workspace, run_directory=tmp_path
        )
        surface = _Surface(_payload(workspace, approval_id="ap-1"))
        surface.replies["relay"] = DELIVERED_RECEIPT
        surface.replies["approve"] = "verdict=allow " + DELIVERED_RECEIPT

        def wrote() -> None:
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(f"{journey.RELAY_WORD}\n")

        surface.effects["approve"] = wrote
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["approval"]))
        walk.walk()
        (answered,) = [call for call in surface.calls if call[0] == "approve"]
        assert answered == ("approve", "ap-1", "allow")
        assert _rows(walk)[str(items.Item.APPROVAL)]["verdict"] == "PASS"

    def test_an_approve_reply_that_is_not_delivered_fails_the_item(self, tmp_path: Path) -> None:
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        surface = _Surface(_payload(workspace, approval_id="ap-1"))
        surface.replies["relay"] = DELIVERED_RECEIPT
        surface.replies["approve"] = "verdict=allow " + RETAINED_RECEIPT
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["approval"]))
        walk.walk()
        assert _rows(walk)[str(items.Item.APPROVAL)]["verdict"] == "FAIL"

    def test_an_allowed_permission_whose_file_never_appears_fails_the_item(
        self, tmp_path: Path
    ) -> None:
        """A real agent that does not perform the action is a FAIL (§3)."""
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        surface = _Surface(_payload(workspace, approval_id="ap-1"))
        surface.replies["relay"] = DELIVERED_RECEIPT
        surface.replies["approve"] = "verdict=allow " + DELIVERED_RECEIPT
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["approval"]))
        walk.walk()
        row = _rows(walk)[str(items.Item.APPROVAL)]
        assert row["verdict"] == "FAIL"
        assert support.resolve(tmp_path, row["evidence"])["event"] == "deadline.expired"

    def test_the_inbound_turn_is_a_reply_anchored_to_the_stop_notice(self, tmp_path: Path) -> None:
        engine = _Engine()
        connection = _Connection(_Message())
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        session = _Session(engine, sends=SENT_LINE)
        walk = _walk(
            tmp_path,
            engine=engine,
            session=session,
            chat=journey.Chat(peer="@lane-bot", connection=connection),
            selection=items.select(["companion inbound"]),
        )
        (workspace / journey.INBOUND_FILE).write_text(f"{journey.INBOUND_WORD}\n")
        walk.walk()
        (peer, anchor, words) = connection.replies[0]
        assert anchor == 4110
        assert journey.INBOUND_WORD in words
        assert _rows(walk)[str(items.Item.COMPANION_INBOUND)]["verdict"] == "PASS"

    def test_the_inbound_turn_is_graded_by_the_file_alone(self, tmp_path: Path) -> None:
        engine = _Engine()
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.COMPANION_INBOUND)]
        assert row["verdict"] == "FAIL"
        assert support.resolve(tmp_path, row["evidence"])["event"] == "deadline.expired"

    def test_a_permission_in_front_of_the_inbound_file_is_answered_as_arrangement(
        self, tmp_path: Path
    ) -> None:
        """The ruling on #353: the one permission this harness answers and does not
        grade. The grade stays the file alone."""
        engine = _Engine()
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        surface = _Surface(_payload(workspace, approval_id="ap-2"))
        surface.replies["approve"] = "verdict=allow " + DELIVERED_RECEIPT
        surface.effects["approve"] = lambda: (workspace / journey.INBOUND_FILE).write_text(
            f"{journey.INBOUND_WORD}\n"
        )
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            bridgectl=surface,
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        assert _rows(walk)[str(items.Item.COMPANION_INBOUND)]["verdict"] == "PASS"
        (arranged,) = [line for line in walk.journal.read() if line["event"] == "approval.arranged"]
        assert arranged["approval_id"] == "ap-2"
        assert arranged["setup_for"] == str(items.Item.COMPANION_INBOUND)

    def test_a_permission_is_answered_once_however_long_the_dialog_lingers(
        self, tmp_path: Path
    ) -> None:
        engine = _Engine()
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        surface = _Surface(_payload(workspace, approval_id="ap-2"))
        surface.replies["approve"] = "verdict=allow " + DELIVERED_RECEIPT
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            bridgectl=surface,
            # A clock that does not run out at once, so the wait polls more than
            # once with the dialog still on the row.
            now=_Clock(step=deadlines.POLL_SECONDS),
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        assert len([call for call in surface.calls if call[0] == "approve"]) == 1

    def test_no_permission_at_all_is_the_ordinary_answer(self, tmp_path: Path) -> None:
        """The Codex lane's sandbox allows a write inside the workspace and asks
        nothing; nothing is journalled and nothing is red for it."""
        engine = _Engine()
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        (workspace / journey.INBOUND_FILE).write_text(f"{journey.INBOUND_WORD}\n")
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        assert _rows(walk)[str(items.Item.COMPANION_INBOUND)]["verdict"] == "PASS"
        assert not [line for line in walk.journal.read() if line["event"] == "approval.arranged"]
        assert not [call for call in walk.bridgectl.calls if call[0] == "approve"]

    def test_a_failed_item_names_the_workspace_the_pty_log_and_the_agents_record(
        self, tmp_path: Path
    ) -> None:
        """§3: a real agent that does not perform the action is a FAIL, and the row
        says where a human looks to see it for themselves."""
        engine = _Engine()
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.COMPANION_INBOUND)]
        line = support.resolve(tmp_path, row["evidence"])
        assert line["workspace"] == str(walk.workspace)
        assert line["pty_log"].endswith(f"pty-{CODEX_LANE}.log")
        assert line["agent_transcript"] == str(ROLLOUT)
        assert "thread-1" in line["agent_record"]

    def test_the_lane_that_names_no_transcript_names_where_one_is(self, tmp_path: Path) -> None:
        """`claude agents --json` carries a session id and no transcript path, so the
        evidence names the config directory the file lives in (§4.1)."""
        engine = _Engine()
        workspace = tmp_path / f"workspace-{CLAUDE_LANE}"
        workspace.mkdir(exist_ok=True)
        walk = _walk(
            tmp_path,
            lane=CLAUDE_LANE,
            engine=engine,
            workspace=workspace,
            # No `record`: `claude agents --json` names a session id and no
            # transcript path.
            truth=lambda: hand_started.GroundTruth(
                session_id="thread-1", pid=7, workspace=workspace
            ),
            session=_Session(
                engine,
                sends=SENT_LINE,
                transcript=tmp_path / f"pty-{CLAUDE_LANE}.log",
                environment={claude_hooks.CONFIG_DIRECTORY_VARIABLE: "/runs/config"},
            ),
            selection=items.select(["companion inbound"]),
        )
        walk.walk()
        row = _rows(walk)[str(items.Item.COMPANION_INBOUND)]
        line = support.resolve(tmp_path, row["evidence"])
        assert line["agent_transcript"] is None
        assert line["agent_config_directory"] == "/runs/config"
        assert "thread-1" in line["agent_record"]

    def test_a_lane_with_no_chat_is_blocked_rather_than_graded(self, tmp_path: Path) -> None:
        """§6 and §1 rule 1: ground the run could not arrange is SKIPPED saying why.

        A red would say the main flow broke, when what broke was the harness's own
        arrangement.
        """
        walk = _walk(tmp_path, chat=None, selection=items.select(["stop notice"]))
        walk.walk()
        row = _rows(walk)[str(items.Item.STOP_NOTICE)]
        assert row["verdict"] == "SKIPPED"
        why = support.resolve(tmp_path, row["evidence"])["why"]
        assert "blocked by" in why and "chat" in why

    def test_every_turn_is_journalled_with_its_seconds(self, tmp_path: Path) -> None:
        """§7: the journal records every turn's seconds, so five minutes is measured."""
        engine = _Engine()
        walk = _walk(
            tmp_path,
            engine=engine,
            session=_Session(engine, sends=SENT_LINE),
            selection=items.select(["stop notice"]),
        )
        walk.walk()
        turns = [line for line in walk.journal.read() if line["event"] == "turn"]
        assert [line["turn"] for line in turns] == [journey.ACKNOWLEDGE_TURN]
        assert all("seconds" in line for line in turns)

    def test_the_relay_turn_is_journalled_from_both_of_its_ends(self, tmp_path: Path) -> None:
        """§2: `relay` and `approval` are one turn read from two ends, so the turn's
        seconds are the two halves a reader adds up."""
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir(exist_ok=True)
        surface = _Surface(_payload(workspace, approval_id="ap-1"))
        surface.replies["relay"] = DELIVERED_RECEIPT
        surface.replies["approve"] = "verdict=allow " + DELIVERED_RECEIPT
        walk = _walk(tmp_path, bridgectl=surface, selection=items.select(["approval"]))
        walk.walk()
        halves = [
            line["half"]
            for line in walk.journal.read()
            if line["event"] == "turn" and line["turn"] == journey.RELAY_TURN
        ]
        assert halves == ["instruction", "permission"]


class TestTheJourneysShape:
    """What the four items are read *by*, rather than what they read."""

    def test_every_lane_item_is_bound_to_a_reading(self, tmp_path: Path) -> None:
        """§7's `missing` is for a row nobody wrote — no item may be unwritten now."""
        walk = _walk(tmp_path)
        assert set(walk.items_walked()) == set(items.LANE_ITEMS)

    def test_the_verdict_of_a_full_walk_carries_every_promised_row(self, tmp_path: Path) -> None:
        walk = _walk(tmp_path)
        walk.walk()
        assert set(_rows(walk)) == {str(item) for item in items.LANE_ITEMS}

    def test_the_turn_names_are_the_specs_three(self, tmp_path: Path) -> None:
        assert (journey.ACKNOWLEDGE_TURN, journey.RELAY_TURN, journey.INBOUND_TURN) == (
            "acknowledge",
            "relay",
            "inbound",
        )


class TestTheRunsWallClock:
    """§7 — the run's wall clock and each lane's are recorded."""

    def test_the_verdict_records_the_run_and_each_lane(self, tmp_path: Path) -> None:
        verdict = support.Verdict(
            run_id="20260910T000000Z",
            selection=items.select(),
            lanes=items.LANES,
            journal=_journal(tmp_path),
        )
        verdict.seconds["run"] = 12.5
        for lane in items.LANES:
            verdict.seconds[lane] = 6.0
        written = json.loads(verdict.write(tmp_path / support.VERDICT_NAME).read_text())
        assert written["seconds"]["run"] == pytest.approx(12.5)
        assert set(written["seconds"]) == {"run", *items.LANES}
