"""The Anchor Table — Bridge Core's memory of which message a reply answers (ADR 0021 §2).

Memory only, keyed by the provider's message id alone, rows gone with their
Session, each Session keeping its newest N. No timer: a day-old notice is a
legitimate thing to reply to.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.core.anchors import Anchor, AnchorKind, AnchorTable, Screen
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)


def notice(target: SessionTarget = CODEX, *, at: float = 1.0, **fields: object) -> Anchor:
    return Anchor(kind=AnchorKind.NOTICE, target=target, sent_at=at, **fields)  # type: ignore[arg-type]


class TestARowIsFoundByTheIdItLandedUnder:
    def test_a_registered_id_finds_its_row(self) -> None:
        table = AnchorTable(rows_per_target=100)
        row = notice(options=("main", "feature"))

        table.register(("40",), row)

        assert table.lookup("40") is row

    def test_every_part_of_a_split_send_finds_the_same_row(self) -> None:
        table = AnchorTable(rows_per_target=100)
        row = notice()

        table.register(("40", "41"), row)

        assert table.lookup("40") is row
        assert table.lookup("41") is row

    def test_an_unknown_id_finds_nothing(self) -> None:
        table = AnchorTable(rows_per_target=100)
        table.register(("40",), notice())

        assert table.lookup("41") is None
        assert table.lookup("") is None

    def test_a_send_that_landed_nowhere_registers_nothing(self) -> None:
        """A toast and a failed send have no ids; there is nothing to reply to."""
        table = AnchorTable(rows_per_target=100)

        table.register((), notice())

        assert table.newest() is None
        assert len(table) == 0


class TestTheNewestAnchor:
    def test_the_newest_is_the_last_registered(self) -> None:
        table = AnchorTable(rows_per_target=100)
        first = notice(CODEX, at=1.0)
        second = notice(CLAUDE, at=2.0)
        table.register(("1",), first)
        table.register(("2",), second)

        assert table.newest() is second

    def test_an_empty_table_has_no_newest(self) -> None:
        assert AnchorTable(rows_per_target=100).newest() is None

    def test_dropping_the_newest_row_reveals_the_one_before_it(self) -> None:
        table = AnchorTable(rows_per_target=100)
        older = notice(CODEX, at=1.0)
        table.register(("1",), older)
        table.register(("2",), notice(CLAUDE, at=2.0))

        table.drop(CLAUDE)

        assert table.newest() is older


class TestRowsGoWithTheirSession:
    def test_dropping_a_session_forgets_every_row_of_it_and_no_other(self) -> None:
        table = AnchorTable(rows_per_target=100)
        table.register(("1", "2"), notice(CODEX))
        table.register(("3",), notice(CLAUDE))

        table.drop(CODEX)

        assert table.lookup("1") is None
        assert table.lookup("2") is None
        assert table.lookup("3") is not None
        assert len(table) == 1

    def test_dropping_a_session_with_no_rows_is_not_an_error(self) -> None:
        table = AnchorTable(rows_per_target=100)
        table.drop(CODEX)
        assert len(table) == 0

    def test_trimming_to_the_roster_forgets_rekeyed_sessions_and_keeps_threads(self) -> None:
        """A Codex row re-keyed on its first turn (#73) is not an ending discovery
        reports, so the table is trimmed to what the roster holds now."""
        table = AnchorTable(rows_per_target=100)
        table.register(("1",), notice(CODEX))
        table.register(("2",), notice(CLAUDE))
        table.register(("3",), Anchor(kind=AnchorKind.ASSISTANT, target="thread-9"))

        table.keep_sessions([CLAUDE])

        assert table.lookup("1") is None
        assert table.lookup("2") is not None
        assert table.lookup("3") is not None


class TestEachSessionKeepsItsNewestN:
    def test_the_n_plus_first_row_evicts_the_oldest_of_that_session(self) -> None:
        table = AnchorTable(rows_per_target=2)
        table.register(("1",), notice(CODEX, at=1.0))
        table.register(("2",), notice(CODEX, at=2.0))

        table.register(("3",), notice(CODEX, at=3.0))

        assert table.lookup("1") is None
        assert table.lookup("2") is not None
        assert table.lookup("3") is not None

    def test_the_cap_is_per_session_not_per_table(self) -> None:
        table = AnchorTable(rows_per_target=1)
        table.register(("1",), notice(CODEX))
        table.register(("2",), notice(CLAUDE))

        assert table.lookup("1") is not None
        assert table.lookup("2") is not None

    def test_eviction_forgets_every_id_of_the_evicted_row(self) -> None:
        table = AnchorTable(rows_per_target=1)
        table.register(("1", "2"), notice(CODEX))

        table.register(("3",), notice(CODEX))

        assert table.lookup("1") is None
        assert table.lookup("2") is None

    def test_a_cap_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AnchorTable(rows_per_target=0)


class TestTheRowShape:
    def test_a_row_holds_neither_telegrams_echo_nor_the_name(self) -> None:
        """The name is re-read from the roster; the words Telegram echoes back are
        never read (ADR 0021 §2). The brief Core itself composed is held, and only
        because the edit that closes a notice re-fills it as sent (§8, #266)."""
        fields = set(Anchor.__dataclass_fields__)
        assert fields == {
            "kind",
            "target",
            "options",
            "picks",
            "approval_id",
            "sent_at",
            "notice",
        }

    def test_a_permission_row_carries_its_approval_id(self) -> None:
        row = Anchor(
            kind=AnchorKind.NOTICE,
            target=CODEX,
            options=("allow", "deny"),
            approval_id="p-1",
            sent_at=1.0,
        )
        assert row.approval_id == "p-1"
        assert row.options == ("allow", "deny")

    def test_an_assistant_row_targets_a_thread_id(self) -> None:
        """#265's rows: the target is the thread id, not a Session (ADR 0021 §7)."""
        row = Anchor(kind=AnchorKind.ASSISTANT, target="thread-9", sent_at=1.0)
        table = AnchorTable(rows_per_target=100)
        table.register(("7",), row)

        assert table.lookup("7") is row
        table.drop("thread-9")
        assert table.lookup("7") is None

    def test_a_menu_row_may_stand_for_something_other_than_its_labels(self) -> None:
        """#264: a roster screen's labels are names, and a press resolves to a Session.

        The label is what the user saw; `picks` is what position N *means* —
        resolved by position and never by the label's text (ADR 0021 §6), so
        two Sessions sharing a name are still two rows the row tells apart.
        """
        row = Anchor(
            kind=AnchorKind.MENU,
            target=Screen.ROSTER,
            options=("gpt-voicecoding · a task", "gpt-voicecoding · a task (claude:def:100)"),
            picks=(CODEX, CLAUDE),
            sent_at=1.0,
        )
        assert row.picks[1] == CLAUDE

    def test_a_row_with_picks_has_one_per_label(self) -> None:
        with pytest.raises(ValueError, match="one pick per label"):
            Anchor(kind=AnchorKind.MENU, target=Screen.CONFIG, options=("a", "b"), picks=("a",))

    def test_a_screen_row_is_nobodys_and_survives_trimming_to_the_roster(self) -> None:
        """A menu screen about the whole engine is not a Session's row (#264)."""
        table = AnchorTable(rows_per_target=100)
        table.register(("7",), Anchor(kind=AnchorKind.MENU, target=Screen.ROSTER, sent_at=1.0))
        table.register(("8",), notice(CODEX, at=2.0))

        table.keep_sessions([])

        assert table.lookup("7") is not None
        assert table.lookup("8") is None

    def test_each_screen_keeps_its_own_newest_n(self) -> None:
        """The cap is per target, and a screen is a target: an old roster falls out."""
        table = AnchorTable(rows_per_target=1)
        table.register(("1",), Anchor(kind=AnchorKind.MENU, target=Screen.ROSTER, sent_at=1.0))
        table.register(("2",), Anchor(kind=AnchorKind.MENU, target=Screen.CONFIG, sent_at=2.0))
        table.register(("3",), Anchor(kind=AnchorKind.MENU, target=Screen.ROSTER, sent_at=3.0))

        assert table.lookup("1") is None
        assert table.lookup("2") is not None
        assert table.lookup("3") is not None
