"""Rebuilding a registration this engine never received, from Claude Code's own records.

`SessionStart` fires once per Session, so an engine that restarts has no report
for any Session that started before it — and every one of them stays listed by
`claude agents --json` while being unreadable and unreachable (#278). Recovery
composes the missing report by **reading** what the machine already shows: the
Session's own registry record, the key file published beside it, and the
transcript whose filename is the session id.

Every test here is about the two halves of that: what a recovery is allowed to
compose, and what it must refuse — by name, with the reason said out loud —
rather than guess at. Nothing is derived from the encoded-cwd flattening (#73),
and nothing is persisted (#74).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from tests.fakes import PROGRESS_CAPTURE

from claude_inbox_fake import FakeInbox
from gpt_voicecoding.adapters.agent.claude import adapter as adapter_module
from gpt_voicecoding.adapters.agent.claude import recovery as recovery_module
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import (
    MESSAGING_SOCKET_FIELD,
    PID_FIELD,
    SESSION_ID_FIELD,
    TRANSCRIPT_PATH_FIELD,
)
from gpt_voicecoding.adapters.agent.claude.recovery import (
    PROJECTS_DIRECTORY_NAME,
    SessionReport,
    default_projects_directory,
    recover,
)
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.core.briefing import _newest
from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    ReplyWindow,
    SessionState,
    WaitingFor,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget

LIVE_PID = os.getpid()
SESSION_ID = "430b0def-38ef-4783-8d57-d800710d83bd"
SOCKET = f"/tmp/cc-socks/{LIVE_PID}.sock"


def target(pid: int = LIVE_PID, session_id: str = SESSION_ID) -> SessionTarget:
    return SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid)


def entry(pid: int = LIVE_PID, **overrides: object) -> dict[str, object]:
    """One live registry record, in the shape `registry.py` is proven against."""
    document: dict[str, object] = {
        "pid": pid,
        "sessionId": SESSION_ID,
        "cwd": "/Users/someone/work",
        "version": "2.1.266",
        "peerProtocol": PEER_PROTOCOL,
        "messagingSocketPath": SOCKET,
        "status": "idle",
    }
    document.update(overrides)
    return document


def write_record(directory: Path, document: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{document['pid']}.json").write_text(json.dumps(document), encoding="utf-8")


def write_key(directory: Path, pid: int, socket_path: str, **fields: object) -> Path:
    """The sibling key file, named the way `inbox.py` names this engine's own."""
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(socket_path.encode()).hexdigest()
    path = directory / f"{pid}.{digest}.key"
    document: dict[str, object] = {"peerToken": "3f0c1d", "procStart": "Mon Sep  8 10:00:00 2026"}
    document.update(fields)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_transcript(projects: Path, project: str, session_id: str) -> Path:
    directory = projects / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


class TestTheProjectsDirectory:
    """Where transcripts are looked for follows the installation, as the registry does."""

    def test_it_follows_the_config_directory(self, tmp_path: Path) -> None:
        assert default_projects_directory(tmp_path) == tmp_path / PROJECTS_DIRECTORY_NAME

    def test_the_directory_name_is_projects(self) -> None:
        assert PROJECTS_DIRECTORY_NAME == "projects"


class TestAWholeRecovery:
    """Every input present, and the report that composes."""

    def test_it_composes_a_report_from_the_records_on_disk(self, tmp_path: Path) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET, peerToken="a-real-token")
        transcript = write_transcript(projects, "-Users-someone-work", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report == SessionReport(
            session_id=SESSION_ID,
            pid=LIVE_PID,
            workspace=Path("/Users/someone/work"),
            transcript_path=transcript,
            messaging_socket=Path(SOCKET),
            messaging_token="a-real-token",
            recovered=True,
        )
        assert found.unread_reason is None

    def test_a_recovered_report_says_it_was_recovered(self, tmp_path: Path) -> None:
        """A recovered inbox address is a route no handshake has been had on."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET)
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.recovered is True
        assert SessionReport(session_id=SESSION_ID).recovered is False


class TestWhatARecoveryRefuses:
    """A reading that does not narrow to exactly one answer decides nothing (ADR 0020)."""

    def test_no_registry_record_for_the_pid_is_refused_with_its_reason(
        self, tmp_path: Path
    ) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        registry.mkdir()
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is None
        assert found.unread_reason is not None
        assert "registered before this engine started" in found.unread_reason
        assert str(LIVE_PID) in found.unread_reason

    def test_a_record_whose_session_id_disagrees_is_pid_reuse_and_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The pid is another process now, and its socket is not this Session's."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry(sessionId="99999999-0000-0000-0000-000000000000"))
        write_key(registry, LIVE_PID, SOCKET)
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is None
        assert found.unread_reason is not None
        assert "another process" in found.unread_reason

    def test_a_wrong_peer_protocol_refuses_through_the_existing_gate(self, tmp_path: Path) -> None:
        """Recovery refuses with `registry.py`, never around it."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry(peerProtocol=PEER_PROTOCOL + 1))
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is None
        assert found.unread_reason is not None
        assert "peerProtocol" in found.unread_reason


class TestFindingTheTranscript:
    """Found by its own name, never assembled from the encoded workspace (#73)."""

    def test_a_session_with_no_transcript_yet_keeps_its_route_and_says_why(
        self, tmp_path: Path
    ) -> None:
        """Too new to have written one is not a Session that is dead."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET)
        projects.mkdir()

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.transcript_path is None
        assert found.report.messaging_socket == Path(SOCKET)
        assert found.unread_reason is not None
        assert "could not be found by session id" in found.unread_reason

    def test_two_transcripts_under_one_session_id_pick_neither(self, tmp_path: Path) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET)
        write_transcript(projects, "-one", SESSION_ID)
        write_transcript(projects, "-two", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.transcript_path is None
        assert found.unread_reason is not None
        assert "2" in found.unread_reason

    def test_the_workspace_is_never_used_to_compose_a_path(self, tmp_path: Path) -> None:
        """The transcript sits under a directory no flattening of `cwd` produces."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry(cwd="/Users/some_one/work.d"))
        write_key(registry, LIVE_PID, SOCKET)
        transcript = write_transcript(projects, "an-unrelated-directory-name", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.transcript_path == transcript


class TestTheTokenKeyFile:
    """A missing token costs the token, never the whole route (#71)."""

    def test_an_absent_key_file_recovers_the_socket_with_no_token(self, tmp_path: Path) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.messaging_socket == Path(SOCKET)
        assert found.report.messaging_token is None
        assert found.unread_reason is None

    def test_an_unparseable_key_file_recovers_the_socket_with_no_token(
        self, tmp_path: Path
    ) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        (registry / f"{LIVE_PID}.{hashlib.sha256(SOCKET.encode()).hexdigest()}.key").write_text(
            "{not json", encoding="utf-8"
        )
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.messaging_token is None

    def test_a_key_file_named_for_another_socket_is_not_this_socket_s_token(
        self, tmp_path: Path
    ) -> None:
        """The name is a hash of the exact socket path, so a near miss is a miss."""
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, "/tmp/cc-socks/other.sock", peerToken="not-ours")
        write_transcript(projects, "-a", SESSION_ID)

        found = recover(target(), registry_directory=registry, projects_directory=projects)

        assert found.report is not None
        assert found.report.messaging_token is None


class TestAResumeFork:
    """Two processes under one session id have two sockets, and neither is the other's."""

    def test_each_pid_recovers_its_own_socket(self, tmp_path: Path) -> None:
        registry, projects = tmp_path / "sessions", tmp_path / "projects"
        first, second = LIVE_PID, LIVE_PID + 1
        write_record(registry, entry(pid=first, messagingSocketPath=f"/tmp/cc-socks/{first}.sock"))
        write_record(
            registry, entry(pid=second, messagingSocketPath=f"/tmp/cc-socks/{second}.sock")
        )
        write_transcript(projects, "-a", SESSION_ID)

        one = recover(target(pid=first), registry_directory=registry, projects_directory=projects)
        two = recover(target(pid=second), registry_directory=registry, projects_directory=projects)

        assert one.report is not None and two.report is not None
        assert one.report.messaging_socket == Path(f"/tmp/cc-socks/{first}.sock")
        assert two.report.messaging_socket == Path(f"/tmp/cc-socks/{second}.sock")


class TestTheAdapterAfterARestart:
    """The whole point, at the seam: a Session listed before this engine started.

    Every adapter here is built the way a restarted engine is — nothing
    registered, because `SessionStart` already fired for these Sessions and this
    process was not there to hear it.
    """

    def adapter(self, tmp_path: Path) -> tuple[ClaudeAgentAdapter, Path]:
        """One config directory holding both halves, as an installation does (#303)."""
        config = tmp_path / "config"
        registry, projects = config / "sessions", config / "projects"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET, peerToken="a-real-token")
        transcript = projects / "-Users-someone-work" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    # The shape a visible assistant turn really has, so the tail
                    # reader that answers `history` and the brief sees one.
                    "type": "assistant",
                    "isSidechain": False,
                    "userType": "external",
                    "timestamp": "2026-09-08T19:08:00.000Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return (
            ClaudeAgentAdapter(progress_capture=PROGRESS_CAPTURE, claude_config_directory=config),
            transcript,
        )

    def test_history_returns_a_page_rather_than_the_refusal(self, tmp_path: Path) -> None:
        """`bridgectl history <target>` stops saying nobody read what it said."""
        adapter, _ = self.adapter(tmp_path)

        page = asyncio.run(adapter.history(target(), before=None, count=10))

        assert page.read_at is not None
        assert [entry.text for entry in page.entries] == ["hi"]

    def test_the_brief_reads_a_real_newest_and_last_activity(self, tmp_path: Path) -> None:
        adapter, _ = self.adapter(tmp_path)

        read = adapter._read_session(  # noqa: SLF001 - the reading the brief is composed from
            target(), WaitingFor(), state=SessionState.IDLE
        )

        assert read.progress.availability is ProgressAvailability.READABLE
        assert [entry.text for entry in read.progress.recent] == ["hi"]
        assert read.last_activity is not None

    def test_the_reply_window_is_read_from_the_registry_rather_than_short_circuited(
        self, tmp_path: Path
    ) -> None:
        """The recovered socket is registered, so `reply_window` stops failing closed."""
        adapter, _ = self.adapter(tmp_path)

        assert adapter.reply_window(target()) is ReplyWindow.CLOSED  # nothing looked yet
        adapter._report_for(target())  # noqa: SLF001 - the lookup a discovery tick makes

        assert target() in adapter.reachable()
        assert adapter.reply_window(target()) is not ReplyWindow.CLOSED

    def test_a_relay_is_addressed_to_the_recovered_socket_and_token(self, tmp_path: Path) -> None:
        """The route and the key a receipt settles on both come off the recovery."""
        adapter, _ = self.adapter(tmp_path)

        report = adapter._report_for(target())  # noqa: SLF001 - the registration lookup

        assert report is not None
        assert report.messaging_socket == Path(SOCKET)
        assert adapter._messaging_token(target()) == "a-real-token"  # noqa: SLF001

    def test_a_session_with_no_registry_record_is_unread_with_its_reason(
        self, tmp_path: Path
    ) -> None:
        """The acceptance criterion, end to end: unread, and the brief says why."""
        config = tmp_path / "config"
        (config / "sessions").mkdir(parents=True)
        (config / "projects").mkdir(parents=True)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )

        read = adapter._read_session(  # noqa: SLF001 - the reading the brief is composed from
            target(), WaitingFor(), state=SessionState.IDLE
        )

        assert read.progress.availability is ProgressAvailability.UNREADABLE
        assert read.progress.reason is not None
        assert "registered before this engine started" in read.progress.reason
        assert "registered before this engine started" in _newest(read.progress).words

    def test_a_session_that_was_never_looked_at_stays_not_read(self, tmp_path: Path) -> None:
        """A hook-registered path that does not exist yet is nobody having looked."""
        config = tmp_path / "config"
        (config / "sessions").mkdir(parents=True)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )
        adapter._reported[target()] = SessionReport(  # noqa: SLF001 - a hook report
            session_id=SESSION_ID, pid=LIVE_PID, transcript_path=config / "nothing.jsonl"
        )

        read = adapter._read_session(  # noqa: SLF001
            target(), WaitingFor(), state=SessionState.IDLE
        )

        assert read.progress.availability is ProgressAvailability.NOT_READ
        assert _newest(read.progress).words == "not read"


class TestPrecedenceAndCost:
    """A hook always wins, a recovery never overwrites, and a miss is paid for once."""

    def test_a_later_hook_registration_replaces_a_recovered_one(self, tmp_path: Path) -> None:
        config = tmp_path / "config"
        registry = config / "sessions"
        write_record(registry, entry())
        write_transcript(config / "projects", "-a", SESSION_ID)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )
        recovered = adapter._report_for(target())  # noqa: SLF001
        assert recovered is not None and recovered.recovered is True

        adapter._session_started(  # noqa: SLF001 - the Session's own hook, arriving late
            {
                SESSION_ID_FIELD: SESSION_ID,
                PID_FIELD: LIVE_PID,
                TRANSCRIPT_PATH_FIELD: str(tmp_path / "the-hook-said-this.jsonl"),
            }
        )

        held = adapter._report_for(target())  # noqa: SLF001
        assert held is not None
        assert held.recovered is False
        assert held.transcript_path == tmp_path / "the-hook-said-this.jsonl"

    def test_a_recovery_never_overwrites_a_report_already_held(self, tmp_path: Path) -> None:
        config = tmp_path / "config"
        write_record(config / "sessions", entry())
        write_transcript(config / "projects", "-a", SESSION_ID)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )
        hook_said = SessionReport(
            session_id=SESSION_ID, pid=LIVE_PID, transcript_path=tmp_path / "hook.jsonl"
        )
        adapter._reported[target()] = hook_said  # noqa: SLF001

        assert adapter._report_for(target()) == hook_said  # noqa: SLF001

    def test_a_miss_does_not_rescan_the_projects_directory_every_tick(self, tmp_path: Path) -> None:
        """The outcome is kept, refusals included, so a tick costs no reads at all."""
        config = tmp_path / "config"
        (config / "sessions").mkdir(parents=True)
        (config / "projects").mkdir(parents=True)
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )
        attempts = 0
        original = recovery_module.recover

        def counting(*args: object, **kwargs: object) -> recovery_module.Recovery:
            nonlocal attempts
            attempts += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(recovery_module, "recover", counting),
            mock.patch.object(adapter_module, "recover", counting),
        ):
            for _ in range(5):
                adapter._transcript_path(target())  # noqa: SLF001 - one discovery tick each

        assert attempts == 1

    def test_recovery_writes_nothing_at_all(self, tmp_path: Path) -> None:
        """Nothing about a registration is persisted (#74): `state.json` is untouched."""
        config = tmp_path / "config"
        registry = config / "sessions"
        write_record(registry, entry())
        write_key(registry, LIVE_PID, SOCKET)
        write_transcript(config / "projects", "-a", SESSION_ID)
        before = {path: path.stat().st_mtime_ns for path in sorted(config.rglob("*"))}

        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
        )
        adapter._report_for(target())  # noqa: SLF001

        after = {path: path.stat().st_mtime_ns for path in sorted(config.rglob("*"))}
        assert after == before


class TestARelayToARecoveredSession:
    """The route comes off the recovery, and a dead socket is a delivery failure."""

    @pytest.fixture
    def short(self) -> Iterator[Path]:
        """A directory short enough to bind a Unix socket under (103 bytes).

        `tmp_path` is not: the reply inbox this Relay needs is bound beside the
        Session's own socket, and pytest's directory alone spends more than the
        whole budget.
        """
        directory = Path(tempfile.mkdtemp(prefix="vc-", dir="/tmp"))
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def config(self, tmp_path: Path, socket_path: str) -> Path:
        config = tmp_path / "config"
        write_record(config / "sessions", entry(messagingSocketPath=socket_path))
        write_key(config / "sessions", LIVE_PID, socket_path)
        write_transcript(config / "projects", "-a", SESSION_ID)
        return config

    def test_a_relay_recovers_the_route_without_waiting_for_a_discovery_tick(
        self, tmp_path: Path, short: Path
    ) -> None:
        socket_path = str(short / "inbox.sock")
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            claude_config_directory=self.config(tmp_path, socket_path),
        )

        receipt = asyncio.run(adapter.answer_relay(target(), "hello", request_id=RequestId("r-1")))

        assert "no Claude Session is registered" not in (receipt.reason or "")
        assert adapter._inboxes[target()] == Path(socket_path)  # noqa: SLF001

    def test_a_recovered_socket_nothing_listens_on_is_a_delivery_failure_naming_it(
        self, tmp_path: Path, short: Path
    ) -> None:
        """Not "that Session does not exist" — the route was found and did not answer."""
        socket_path = str(short / "dead.sock")
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            claude_config_directory=self.config(tmp_path, socket_path),
        )

        receipt = asyncio.run(adapter.answer_relay(target(), "hello", request_id=RequestId("r-1")))

        assert receipt.outcome is Delivery.FAILED
        assert receipt.reason is not None
        assert "dead.sock" in receipt.reason


class TestTheHookTakeover:
    """A recovered registration is replaced the moment the Session's own hook reports."""

    def adapter(self, tmp_path: Path, socket_path: str) -> ClaudeAgentAdapter:
        config = tmp_path / "config"
        write_record(config / "sessions", entry(messagingSocketPath=socket_path))
        write_transcript(config / "projects", "-a", SESSION_ID)
        return ClaudeAgentAdapter(progress_capture=PROGRESS_CAPTURE, claude_config_directory=config)

    def test_a_hook_report_with_no_socket_takes_the_recovered_route_away(
        self, tmp_path: Path
    ) -> None:
        """Otherwise a Relay lands on a disk-read address the Session never gave us."""
        socket_path = str(tmp_path / "recovered.sock")
        adapter = self.adapter(tmp_path, socket_path)
        adapter._report_for(target())  # noqa: SLF001 - the recovery a tick makes
        assert target() in adapter.reachable()

        adapter._session_started(  # noqa: SLF001 - a build that exports no messaging variables
            {SESSION_ID_FIELD: SESSION_ID, PID_FIELD: LIVE_PID}
        )

        held = adapter.reported(target())
        assert held is not None and held.messaging_socket is None
        assert target() not in adapter.reachable()
        assert adapter.reply_window(target()) is ReplyWindow.CLOSED

    def test_a_hook_report_carrying_a_socket_replaces_the_recovered_one(
        self, tmp_path: Path
    ) -> None:
        adapter = self.adapter(tmp_path, str(tmp_path / "recovered.sock"))
        adapter._report_for(target())  # noqa: SLF001

        adapter._session_started(  # noqa: SLF001
            {
                SESSION_ID_FIELD: SESSION_ID,
                PID_FIELD: LIVE_PID,
                MESSAGING_SOCKET_FIELD: str(tmp_path / "the-hook-said-this.sock"),
            }
        )

        assert adapter._inboxes[target()] == tmp_path / "the-hook-said-this.sock"  # noqa: SLF001


class TestARecoveredRelaySettlesLikeAnyOther:
    """The receipt hangs on the reply key, and a recovered route earns it identically.

    The spec's word is *exactly as one to a hook-registered Session does*, so
    this drives the whole settlement — the recovered socket carries the frames,
    the token off the recovered key file rides on them, and the receiver's
    `held -> delivered` on our published reply address is what grades it.
    """

    def test_a_relay_on_a_recovered_socket_settles_delivered(self, tmp_path: Path) -> None:
        short = Path(tempfile.mkdtemp(prefix="vc-", dir="/tmp"))
        try:
            socket_path = short / "session.sock"
            config = short / "config"
            write_record(config / "sessions", entry(messagingSocketPath=str(socket_path)))
            write_key(config / "sessions", LIVE_PID, str(socket_path), peerToken="a-real-token")
            write_transcript(config / "projects", "-a", SESSION_ID)

            async def scenario() -> tuple[DeliveryReceipt, list[dict[str, object]]]:
                async with FakeInbox(
                    socket_path, statuses=((0.0, "held"), (0.01, "delivered"))
                ) as session:
                    adapter = ClaudeAgentAdapter(
                        progress_capture=PROGRESS_CAPTURE, claude_config_directory=config
                    )
                    try:
                        receipt = await adapter.answer_relay(
                            target(), "ship it", request_id=RequestId("r-1")
                        )
                        return receipt, session.received
                    finally:
                        await adapter.aclose()

            receipt, received = asyncio.run(scenario())

            assert receipt.outcome is Delivery.DELIVERED
            assert [frame["message"]["content"] for frame in received if frame["type"] == "user"]
            # The token came off the key file beside the recovered socket, and
            # rode in on the same auth frame a hook-registered Session's does.
            assert [frame["token"] for frame in received if frame.get("type") == "auth"] == [
                "a-real-token"
            ]
        finally:
            shutil.rmtree(short, ignore_errors=True)


class TestWhenARefusalIsTriedAgain:
    """A success is kept for the engine's life; only a refusal expires (ADR 0013)."""

    def adapter(self, config: Path, interval: float = 60.0) -> ClaudeAgentAdapter:
        return ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            claude_config_directory=config,
            settings=ClaudeSettings(recovery_retry_interval_seconds=interval),
        )

    def test_a_transcript_written_after_the_refusal_is_picked_up(self, tmp_path: Path) -> None:
        """The case that made permanent caching wrong: a Session too new to have one."""
        config = tmp_path / "config"
        write_record(config / "sessions", entry())
        (config / "projects").mkdir(parents=True)
        adapter = self.adapter(config, interval=60.0)
        clock = 1_000.0

        with mock.patch.object(adapter_module.time, "monotonic", lambda: clock):
            assert adapter._transcript_path(target()) is None  # noqa: SLF001
            transcript = write_transcript(config / "projects", "-a", SESSION_ID)
            clock += 60.0

            assert adapter._transcript_path(target()) == transcript  # noqa: SLF001
            assert adapter._unread_reason(target()) is None  # noqa: SLF001

    def test_a_refusal_stands_until_the_interval_has_passed(self, tmp_path: Path) -> None:
        config = tmp_path / "config"
        write_record(config / "sessions", entry())
        (config / "projects").mkdir(parents=True)
        adapter = self.adapter(config, interval=60.0)

        assert adapter._transcript_path(target()) is None  # noqa: SLF001
        write_transcript(config / "projects", "-a", SESSION_ID)

        # Still inside the floor, so the cached refusal is the answer — and it
        # keeps its reason, so the brief goes on saying why.
        assert adapter._transcript_path(target()) is None  # noqa: SLF001
        assert adapter._unread_reason(target()) is not None  # noqa: SLF001

    def test_a_success_is_never_re_attempted(self, tmp_path: Path) -> None:
        config = tmp_path / "config"
        write_record(config / "sessions", entry())
        write_transcript(config / "projects", "-a", SESSION_ID)
        adapter = self.adapter(config, interval=60.0)
        attempts = 0
        original = adapter_module.recover

        def counting(*args: object, **kwargs: object) -> recovery_module.Recovery:
            nonlocal attempts
            attempts += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        clock = 1_000.0
        with (
            mock.patch.object(adapter_module, "recover", counting),
            mock.patch.object(adapter_module.time, "monotonic", lambda: clock),
        ):
            for _ in range(5):
                adapter._transcript_path(target())  # noqa: SLF001
            clock += 86_400.0  # a whole day past any floor

            adapter._transcript_path(target())  # noqa: SLF001

        assert attempts == 1

    def test_a_refusal_is_re_attempted_at_most_once_per_interval(self, tmp_path: Path) -> None:
        """The ticket's cost bound: one directory scan a minute, not one a tick."""
        config = tmp_path / "config"
        (config / "sessions").mkdir(parents=True)
        (config / "projects").mkdir(parents=True)
        adapter = self.adapter(config, interval=60.0)
        attempts = 0
        original = adapter_module.recover

        def counting(*args: object, **kwargs: object) -> recovery_module.Recovery:
            nonlocal attempts
            attempts += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        clock = 1_000.0
        with (
            mock.patch.object(adapter_module, "recover", counting),
            mock.patch.object(adapter_module.time, "monotonic", lambda: clock),
        ):
            for _ in range(12):  # twelve five-second discovery ticks
                adapter._transcript_path(target())  # noqa: SLF001
            assert attempts == 1
            clock += 60.0
            adapter._transcript_path(target())  # noqa: SLF001

        assert attempts == 2


class TestARefusingRetryTakesTheRouteBack:
    """A route lives exactly as long as the reading that justified it."""

    def test_a_retry_that_refuses_withdraws_the_socket_the_first_attempt_registered(
        self, tmp_path: Path
    ) -> None:
        """Partial recovery registers a socket; pid reuse a minute later must end it.

        Otherwise `_deliver` goes on writing to a route this adapter has just
        decided belongs to another process — the delivery into the wrong
        conversation a Claude target carries a pid to prevent.
        """
        config = tmp_path / "config"
        write_record(config / "sessions", entry())
        (config / "projects").mkdir(parents=True)  # no transcript: a partial recovery
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            claude_config_directory=config,
            settings=ClaudeSettings(recovery_retry_interval_seconds=60.0),
        )
        clock = 1_000.0
        with mock.patch.object(adapter_module.time, "monotonic", lambda: clock):
            adapter._report_for(target())  # noqa: SLF001
            assert target() in adapter.reachable()

            # That pid is another process now.
            write_record(
                config / "sessions", entry(sessionId="99999999-0000-0000-0000-000000000000")
            )
            clock += 60.0

            assert adapter._report_for(target()) is None  # noqa: SLF001

        assert target() not in adapter.reachable()
        assert target() not in adapter._inboxes  # noqa: SLF001
        assert adapter.reply_window(target()) is ReplyWindow.CLOSED
