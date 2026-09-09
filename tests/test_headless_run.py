"""The Headless Run: seen, kept, and never announced (#319, ADR 0020 as amended).

On 2026-09-09, 19 of 189 Companion Channel pushes were ended lines for agentcrew
Witness runs — `claude --print` processes nobody can type into, that live for
seconds and that the user can never reply to. Each produced exactly one push and
nothing else, which is the noise this tier removes.

The rule is one fact and one place: each lane reports whether `ps -o tty=` named
a controlling terminal, and Bridge Core alone decides what that makes the run.
So these tests drive the fact in at the seam and read the *user-facing* answer
out — what was pushed, what the Roster Brief holds, what `/sessions` shows, what
a Relay to it does — because those are the sentences ADR 0021 §9 and
`CONTEXT.md`'s *Headless Run* are written in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent import _terminals
from gpt_voicecoding.adapters.agent._terminals import TerminalMemo
from gpt_voicecoding.adapters.agent.claude import discovery as claude_discovery
from gpt_voicecoding.control_plane import payloads
from gpt_voicecoding.core import briefing, menu
from gpt_voicecoding.core.errors import HeadlessRunError
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.relays import RelayPipeline
from gpt_voicecoding.core.router import InboundClass, InboundRouter
from gpt_voicecoding.core.sessions import Session, SessionRegistry, climbed_to
from gpt_voicecoding.seams.agent import (
    ApprovalVerdict,
    LaneDiscovery,
    SessionInspection,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget
from hub import CODEX, Hub

WORKSPACE = Path("/tmp/workspace")

#: One `claude --print` launched detached, as the two lanes read it: the pid is
#: in the process table and its `tty` column reads `??`.
WITNESS = SessionTarget(agent=AgentKind.CLAUDE, session_id="witness-1", pid=4242)


def row(
    target: SessionTarget = WITNESS,
    *,
    has_controlling_terminal: bool | None,
    state: SessionState = SessionState.IDLE,
) -> SessionInspection:
    """One lane's reading of one run, carrying the one fact this tier turns on."""
    return SessionInspection(
        target=target,
        workspace=WORKSPACE,
        state=state,
        has_controlling_terminal=has_controlling_terminal,
    )


def observed(*rows: SessionInspection, registry: SessionRegistry | None = None) -> SessionRegistry:
    """A roster that has seen those readings, in order, as one lane's whole truth."""
    held = registry if registry is not None else SessionRegistry()
    for reading in rows:
        held.observe(rows[0].target.agent, LaneDiscovery(rows=(reading,)), now=1.0)
    return held


class TestTheTierIsDecidedFromTheOneFact:
    """`False` is a Headless Run; `True` and `None` are both Sessions.

    The asymmetry is ADR 0020's own and is the reason `None` is a value rather
    than a default of `False`: silencing a real Session is the expensive error,
    and an extra ended line is the cheap one.
    """

    @pytest.mark.parametrize(
        ("terminal", "headless"),
        [(True, False), (False, True), (None, False)],
    )
    def test_it_is_read_off_the_controlling_terminal_alone(
        self, terminal: bool | None, headless: bool
    ) -> None:
        held = observed(row(has_controlling_terminal=terminal)).all()[0]

        assert held.is_headless_run is headless

    def test_nothing_else_on_the_row_decides_it(self) -> None:
        """ADR 0020's amendment refuses `entrypoint`, `kind`, `tmux` and the prompt.

        Nothing here can even express them, which is the point: the seam carries
        one fact for this question, so there is no second discriminator for a
        later reader to reach for.
        """
        fields = {field for field in SessionInspection.__dataclass_fields__}

        assert "has_controlling_terminal" in fields
        assert not fields & {"entrypoint", "kind", "tmux", "headless"}


class TestItIsNeverAnnounced:
    """No Stop Notice, no ended line — ADR 0021 §9 as amended."""

    def hub_holding_a_headless_run(self) -> Hub:
        """A hub whose roster has already seen the Witness run, and nothing else."""
        hub = Hub(sessions=())
        hub.state.sessions.observe(
            AgentKind.CLAUDE, LaneDiscovery(rows=(row(has_controlling_terminal=False),)), now=1.0
        )
        return hub

    def test_its_stop_pushes_nothing(self) -> None:
        hub = self.hub_holding_a_headless_run()

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))

        assert hub.channel.sent == []
        assert hub.call.spoken == []

    def test_its_stop_registers_no_anchor(self) -> None:
        """No Anchor row, so a reply naming it takes the unknown-Anchor path."""
        hub = self.hub_holding_a_headless_run()

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))

        assert len(hub.core.anchors) == 0

    def test_its_stop_is_still_written_to_the_roster(self) -> None:
        """Kept, not ignored: the row is what makes the verdict stable (#319).

        A run this engine refuses to announce is still a run it has seen, and
        the row is where the fact that it is silent lives. Dropping the reading
        would make the same run be decided afresh on every pass.
        """
        hub = self.hub_holding_a_headless_run()

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))

        assert hub.state.sessions.all()[0].state is SessionState.IDLE

    def test_its_end_pushes_no_ended_line(self) -> None:
        """The 19 pushes of 2026-09-09 were exactly this line."""
        assert self.gone(has_controlling_terminal=False) == []

    def test_a_sessions_end_still_pushes_one(self) -> None:
        """The control the test above needs: this path does announce an ending."""
        assert self.gone(has_controlling_terminal=True) != []

    def gone(self, *, has_controlling_terminal: bool | None) -> list[str]:
        """What the channel was sent when a run left the roster between two passes."""
        hub = Hub(sessions=())
        hub.state.sessions.observe(
            AgentKind.CLAUDE,
            LaneDiscovery(rows=(row(has_controlling_terminal=has_controlling_terminal),)),
            now=1.0,
        )
        hub.agent.discovery = LaneDiscovery()
        asyncio.run(hub.core.discover())
        asyncio.run(hub.core.drain())
        return hub.channel.sent

    def test_a_session_beside_it_is_announced_exactly_as_before(self) -> None:
        """Noise leaves by not announcing a non-Session, never by hiding a Session."""
        hub = self.hub_holding_a_headless_run()

        hub.emit(SessionStopped(target=CODEX, has_controlling_terminal=True))

        assert hub.channel.sent != []

    def test_it_says_once_in_the_log_why_it_is_silent(self, caplog) -> None:
        """User story 6: a silent run is explainable from `engine.log`.

        Once per row, on the pass that decides it — the verdict does not change,
        and repeating it every cadence would bury the run's other lines.
        """
        registry = SessionRegistry()
        with caplog.at_level("INFO"):
            observed(
                row(has_controlling_terminal=False),
                row(has_controlling_terminal=False),
                registry=registry,
            )

        said = [record.getMessage() for record in caplog.records]
        assert [line for line in said if "Headless Run" in line] == [
            f"{WITNESS} is a Headless Run: no controlling terminal"
        ]


class TestAStopThatBeatsEveryDiscoveryPass:
    """#216's stand-in row, judged on the fact the Stop itself carried.

    A Claude Stop can precede every pass — `register_session` starts the window
    watch from the `SessionStart` hook — so the row a Stop stands in for would
    be a Session by default and be announced once before any pass could correct
    it. That one push is exactly the noise this tier removes.
    """

    def test_it_produces_no_notice(self) -> None:
        hub = Hub(sessions=())

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))

        assert hub.channel.sent == []

    def test_the_row_it_stands_in_carries_the_tier(self) -> None:
        hub = Hub(sessions=())

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))

        assert hub.state.sessions.all()[0].is_headless_run

    def test_a_stop_that_could_not_tell_is_announced(self) -> None:
        """`None` is not `False`: an unread terminal leaves the run a Session."""
        hub = Hub(sessions=())

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=None))

        assert hub.channel.sent != []


class TestItIsOnNoRosterTheUserReads:
    """User story 4: never a row the user cannot reply to."""

    def headless(self) -> Session:
        return Session(
            target=WITNESS,
            workspace=WORKSPACE,
            first_seen=0.0,
            name=SessionName("GPT-VoiceCoding", "witness the run"),
            state=SessionState.IDLE,
            has_controlling_terminal=False,
        )

    def session(self) -> Session:
        return Session(
            target=CODEX,
            workspace=WORKSPACE,
            first_seen=0.0,
            name=SessionName("GPT-VoiceCoding", "port the log"),
            state=SessionState.IDLE,
            has_controlling_terminal=True,
        )

    def test_the_roster_brief_omits_it(self) -> None:
        brief = briefing.roster([self.session(), self.headless()], None)

        assert [str(shown.name) for shown in brief.rows] == ["GPT-VoiceCoding · port the log"]

    def test_the_sessions_screen_omits_it(self) -> None:
        """`/sessions` omits it by consuming that brief, so the rule is stated once."""
        screen = menu.roster_screen(briefing.roster([self.session(), self.headless()], None))

        assert screen.options == ("GPT-VoiceCoding · port the log",)

    def test_a_roster_of_nothing_but_headless_runs_is_empty(self) -> None:
        assert briefing.roster([self.headless()], None).rows == ()

    def test_the_registry_still_holds_it(self) -> None:
        """Seen and kept. "Appears in the roster" stays true of `status`."""
        registry = observed(row(has_controlling_terminal=False))

        assert [held.target for held in registry.live()] == [WITNESS]


class TestItCarriesItsTierOnTheControlPlaneWire:
    """`status` lists every row, so the row has to say what it is.

    Placed by the wrap-up ruling. The row stays on the wire — that is where
    "appears in the roster" is true, as it is for a Child Process — so a surface
    counting the Sessions a person can see running needs a fact to exclude it
    by. Without one, `shell/Sources/ShellCore/ControlPanel.swift` counted a
    `claude --print` launched detached as a Session the user was running.
    """

    def wire(self, *, has_controlling_terminal: bool | None) -> dict:
        return payloads.session_document(
            Session(
                target=WITNESS,
                workspace=WORKSPACE,
                first_seen=0.0,
                state=SessionState.IDLE,
                has_controlling_terminal=has_controlling_terminal,
            ),
            progress={},
        )

    def test_a_headless_runs_row_says_so(self) -> None:
        assert self.wire(has_controlling_terminal=False)["headless_run"] is True

    @pytest.mark.parametrize("terminal", [True, None])
    def test_a_sessions_row_does_not(self, terminal: bool | None) -> None:
        assert self.wire(has_controlling_terminal=terminal)["headless_run"] is False

    def test_it_travels_beside_the_other_tier_and_replaces_nothing(self) -> None:
        """Two tiers, two keys: a Child Process still says what it is."""
        row = self.wire(has_controlling_terminal=False)

        assert row["child"] == {"kind": "main", "parent": None}


class TestItIsRefusedAsARelayTarget:
    """Every surface, and by the registry rather than by a caller's memory."""

    def registry(self) -> SessionRegistry:
        return observed(row(has_controlling_terminal=False))

    def test_resolve_refuses_it(self) -> None:
        with pytest.raises(HeadlessRunError):
            self.registry().resolve(WITNESS)

    def test_the_relay_pipeline_refuses_before_any_adapter_is_touched(self) -> None:
        """One refusal for the control plane and a Companion Channel reply alike.

        Both arrive here — `bridge.relay` is the hub verb every surface calls —
        so a refusal raised in `resolve` is the one answer both get, and no
        adapter is asked to carry words into a run that cannot read them.
        """
        pipeline = RelayPipeline(agents={}, sessions=self.registry(), relays=RelayQueue())

        with pytest.raises(HeadlessRunError):
            asyncio.run(pipeline.relay(WITNESS, "are you there"))

    def test_the_refusal_names_the_tier(self) -> None:
        """A refusal a surface can speak, in the vocabulary `CONTEXT.md` defines."""
        with pytest.raises(HeadlessRunError) as refused:
            self.registry().resolve(WITNESS)

        assert "Headless Run" in str(refused.value)

    def test_an_approval_relay_is_refused_in_the_same_words(self) -> None:
        """An Approval Relay is a Relay, and this path does not go through `resolve`.

        A verdict carried into a run nobody can type into is the user's
        authority landing where they cannot see the dialog, so the refusal is
        asked for here explicitly rather than left to the gate above.
        """
        hub = Hub(sessions=())
        hub.state.sessions.register(
            Session(
                target=WITNESS,
                workspace=WORKSPACE,
                first_seen=0.0,
                state=SessionState.WAITING,
                waiting_for=WaitingFor(
                    kind=WaitingKind.PERMISSION, tool_name="Bash", approval_id="a1"
                ),
                has_controlling_terminal=False,
            )
        )

        with pytest.raises(HeadlessRunError):
            asyncio.run(hub.core.answer_approval("a1", ApprovalVerdict.ALLOW))

    def test_a_session_is_resolved_exactly_as_before(self) -> None:
        registry = observed(row(has_controlling_terminal=True))

        assert registry.resolve(WITNESS).target == WITNESS


class TestItIsNeverAnImplicitTarget:
    """A row nobody can type into is never the one bare words are routed to.

    Found by the ticket's review: refusing the Relay in `resolve` is not the
    whole of "no Relay from every surface", because the Companion Channel picks
    the target *before* it relays — and a router reading the whole live roster
    would either send words to the only Headless Run there was, or read a real
    Session beside one as an ambiguity for the user to resolve by name.
    """

    def registry(self, *rows: Session) -> SessionRegistry:
        held = SessionRegistry()
        for held_row in rows:
            held.register(held_row)
        return held

    def headless(self) -> Session:
        return Session(
            target=WITNESS,
            workspace=WORKSPACE,
            first_seen=0.0,
            state=SessionState.IDLE,
            has_controlling_terminal=False,
        )

    def session(self) -> Session:
        return Session(
            target=CODEX,
            workspace=WORKSPACE,
            first_seen=1.0,
            name=SessionName("GPT-VoiceCoding", "port the log"),
            state=SessionState.IDLE,
            has_controlling_terminal=True,
        )

    def test_bare_words_never_go_to_the_only_headless_run(self) -> None:
        router = InboundRouter(sessions=self.registry(self.headless()))

        found = router.classify("carry on")

        assert found.kind is not InboundClass.ANSWER_RELAY

    def test_a_session_beside_one_is_not_an_ambiguity(self) -> None:
        """The user is not asked to choose between a Session and a non-Session."""
        router = InboundRouter(sessions=self.registry(self.headless(), self.session()))

        found = router.classify("carry on")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX

    def test_it_is_not_the_session_the_voice_speaks_about(self) -> None:
        """`spoken_first` reads `sole_live`, so a Session beside one only rang."""
        registry = self.registry(self.headless(), self.session())

        assert registry.spoken_first == CODEX

    def test_the_registry_still_holds_it_beside_the_session(self) -> None:
        """Kept, and kept out of the choosing. `status` still carries both rows."""
        registry = self.registry(self.headless(), self.session())

        assert len(registry.live()) == 2
        assert [held.target for held in registry.addressable()] == [CODEX]


class TestTheVerdictClimbsAndNeverFalls:
    """The two readings edge cases, and the one that is neither."""

    def test_a_run_misread_as_headless_climbs_when_a_terminal_appears(self) -> None:
        registry = observed(row(has_controlling_terminal=False), row(has_controlling_terminal=True))

        assert not registry.all()[0].is_headless_run

    def test_it_is_announced_from_then_on_and_never_retroactively(self) -> None:
        """Nothing replays: the pushes it did not make while silent stay unmade."""
        hub = Hub(sessions=())
        hub.state.sessions.observe(
            AgentKind.CLAUDE, LaneDiscovery(rows=(row(has_controlling_terminal=False),)), now=1.0
        )
        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=False))
        hub.state.sessions.observe(
            AgentKind.CLAUDE, LaneDiscovery(rows=(row(has_controlling_terminal=True),)), now=2.0
        )

        hub.emit(SessionStopped(target=WITNESS, has_controlling_terminal=True))

        assert len(hub.channel.sent) == 1

    def test_a_session_whose_terminal_disappears_stays_a_session(self) -> None:
        """A tty column that goes blank is a misread, not a Session that lost a seat."""
        registry = observed(row(has_controlling_terminal=True), row(has_controlling_terminal=False))

        assert not registry.all()[0].is_headless_run

    def test_a_pid_that_left_the_table_is_not_read_as_an_answer(self) -> None:
        """`None` never overwrites, so a Headless Run that exits stays silent.

        This is the case that would otherwise produce the ended line: the run
        finishes, its pid leaves the process table between the roster read and
        the `ps`, and a reading taken as `True` would announce exactly the death
        this tier exists to keep quiet.
        """
        registry = observed(row(has_controlling_terminal=False), row(has_controlling_terminal=None))

        assert registry.all()[0].is_headless_run

    @pytest.mark.parametrize(
        ("held", "seen", "kept"),
        [
            (None, None, None),
            (None, True, True),
            (None, False, False),
            (False, None, False),
            (False, True, True),
            (False, False, False),
            (True, None, True),
            (True, False, True),
            (True, True, True),
        ],
    )
    def test_the_whole_merge_table(
        self, held: bool | None, seen: bool | None, kept: bool | None
    ) -> None:
        assert climbed_to(held, seen) is kept


class TestReadingTheControllingTerminal:
    """The shared reader both lanes use, against `ps` output rather than a machine."""

    def test_a_named_terminal_is_a_terminal(self) -> None:
        assert _terminals.reads_as("ttys004") is True

    def test_the_two_question_marks_are_macos_saying_none(self) -> None:
        assert _terminals.reads_as(" ?? ") is False

    def test_an_empty_column_is_not_an_answer(self) -> None:
        """`ps` printed no row, because the pid was gone before it looked."""
        assert _terminals.reads_as("") is None

    def test_the_whole_table_is_read_once_for_every_pid(self) -> None:
        asked: list[list[str]] = []

        async def run(argv: list[str]) -> str:
            asked.append(argv)
            return "  101 ttys001\n  102 ??\n"

        found = asyncio.run(_terminals.by_pid([101, 102, 103], run=run))

        assert found == {101: True, 102: False, 103: None}
        assert len(asked) == 1

    def test_a_table_that_cannot_be_read_answers_for_nobody(self) -> None:
        """Not a lane error: the lane found its Sessions, and this fact is missing."""

        async def refuse(argv: list[str]) -> str:
            raise OSError("no ps here")

        assert asyncio.run(_terminals.by_pid([101], run=refuse)) == {101: None}

    def test_a_pid_is_asked_about_once_and_remembered(self) -> None:
        """A process keeps the terminal it started with, so the sweep re-reads none."""
        asked: list[int] = []
        memo = TerminalMemo(read=lambda pid: (asked.append(pid), False)[1])

        answers = [memo.of(7), memo.of(7), memo.of(7)]

        assert answers == [False, False, False]
        assert asked == [7]

    def test_a_read_that_failed_is_asked_again(self) -> None:
        """Which is how a run misread once climbs rather than staying silent for life."""
        answers = iter([None, True])
        memo = TerminalMemo(read=lambda _: next(answers))

        assert [memo.of(7), memo.of(7)] == [None, True]


class TestTheClaudeLaneReportsTheFact:
    """One `ps` per discovery pass, joined against the pids the roster named."""

    def test_every_row_carries_what_the_table_said(self) -> None:
        roster = (
            '[{"sessionId": "a", "pid": 101, "kind": "interactive", "status": "idle",'
            ' "cwd": "/tmp/one"},'
            ' {"sessionId": "b", "pid": 102, "kind": "interactive", "status": "idle",'
            ' "cwd": "/tmp/two"}]'
        )
        commands: list[list[str]] = []

        async def run(argv: list[str]) -> claude_discovery.CommandResult:
            commands.append(argv)
            if argv[0] == "claude":
                return claude_discovery.CommandResult(code=0, stdout=roster, stderr="")
            return claude_discovery.CommandResult(
                code=0, stdout="  101 ttys001\n  102 ??\n", stderr=""
            )

        lane = asyncio.run(claude_discovery.discover(run=run))

        assert [held.has_controlling_terminal for held in lane.rows] == [True, False]
        assert len([argv for argv in commands if argv[0] != "claude"]) == 1

    def test_a_pid_the_table_no_longer_holds_is_unread(self) -> None:
        roster = (
            '[{"sessionId": "a", "pid": 101, "kind": "interactive", "status": "idle",'
            ' "cwd": "/tmp/one"}]'
        )

        async def run(argv: list[str]) -> claude_discovery.CommandResult:
            if argv[0] == "claude":
                return claude_discovery.CommandResult(code=0, stdout=roster, stderr="")
            return claude_discovery.CommandResult(code=0, stdout="", stderr="")

        lane = asyncio.run(claude_discovery.discover(run=run))

        assert lane.rows[0].has_controlling_terminal is None
