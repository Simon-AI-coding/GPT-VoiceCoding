"""The user-account client's lock and its credentials, at CI speed (#351).

Two rules of `tests/acceptance/telegram_person.py` are ordinary code and are
pinned here; the client itself is not, because it needs a real Telegram account.

* **The session lock** (`docs/acceptance-design.md` §5). Two acceptance runs
  share one SQLite session behind one client and one `config.toml` a thread lock
  says nothing about across processes, so the second run refuses — naming the
  holder's pid and run directory — and **a dead holder is a ghost, not a
  holder**. Both halves are driven here, the second with a pid the kernel has
  certainly reclaimed.
* **The credential precedence** (§8): the environment wins, the 0600 file is the
  fallback, and neither is ever hard-coded.

Nothing here imports `telethon`: it is imported inside the functions that need
it, which is what lets this module load — and this suite collect — on a machine
that never installed the `acceptance` extra.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import deadlines
import pytest
import telegram_person

RUN_DIRECTORY = "/runs/20260910T012345Z"


def held(directory: Path, **changes: object) -> telegram_person.PersonSessionLock:
    settings: dict[str, object] = {
        "held_by": telegram_person.ACCEPTANCE_RUN_HOLDER,
        "directory": directory,
        "run_directory": RUN_DIRECTORY,
    }
    settings.update(changes)
    return telegram_person.PersonSessionLock(**settings)  # type: ignore[arg-type]


class TestTheSessionLock:
    """§5 — one acceptance run per machine, refused rather than kept by hand."""

    def test_the_first_holder_takes_it_and_writes_who_it_is(self, tmp_path: Path) -> None:
        with held(tmp_path) as lock:
            recorded = json.loads(lock.path.read_text())
        assert recorded["pid"] == os.getpid()
        assert recorded["run_directory"] == RUN_DIRECTORY
        assert recorded["held_by"] == telegram_person.ACCEPTANCE_RUN_HOLDER

    def test_a_second_holder_is_refused_and_the_refusal_names_the_first(
        self, tmp_path: Path
    ) -> None:
        with held(tmp_path), pytest.raises(telegram_person.SessionInUse) as refused:
            held(tmp_path).acquire()
        assert refused.value.holder.pid == os.getpid()
        assert refused.value.holder.run_directory == RUN_DIRECTORY
        assert str(os.getpid()) in str(refused.value)
        assert RUN_DIRECTORY in str(refused.value)

    def test_a_status_check_and_a_run_are_told_apart(self, tmp_path: Path) -> None:
        """Both take the same lock, and only the writer knows which it is (§8)."""
        with held(tmp_path, held_by=telegram_person.STATUS_CHECK_HOLDER):
            with pytest.raises(telegram_person.SessionInUse) as refused:
                held(tmp_path).acquire()
        assert telegram_person.STATUS_CHECK_HOLDER in str(refused.value)
        assert telegram_person.ACCEPTANCE_RUN_HOLDER not in str(refused.value)

    def test_a_dead_holder_is_a_ghost_and_not_a_holder(self, tmp_path: Path) -> None:
        """A record naming a process that is gone is waited out, never quoted (§5).

        Pid 0 is never a process this reader can find by `os.kill(pid, 0)` in the
        sense that matters: what the rule turns on is `ProcessLookupError`, and a
        record for a pid nothing holds must not be reported as a live holder.
        """
        telegram_person.session_lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        telegram_person.session_lock_path(tmp_path).write_text(
            json.dumps({"pid": _a_reclaimed_pid(), "run_directory": RUN_DIRECTORY, "held_by": "x"})
        )
        assert telegram_person.read_lock_holder(tmp_path, **_at_once()) == (
            telegram_person.LockHolder(None, None, None)
        )

    def test_a_dead_holders_record_does_not_stop_the_next_run(self, tmp_path: Path) -> None:
        """`flock` is released by the kernel, so the file alone never holds a run out."""
        telegram_person.session_lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        telegram_person.session_lock_path(tmp_path).write_text(
            json.dumps({"pid": _a_reclaimed_pid(), "run_directory": "/runs/old", "held_by": "x"})
        )
        with held(tmp_path) as lock:
            assert lock.held

    def test_an_unreadable_record_is_named_as_unknown_and_never_guessed(
        self, tmp_path: Path
    ) -> None:
        telegram_person.session_lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        telegram_person.session_lock_path(tmp_path).write_text("not json")
        holder = telegram_person.read_lock_holder(tmp_path, **_at_once())
        assert holder.pid is None
        assert telegram_person.UNKNOWN_HOLDER in str(holder)

    def test_a_clean_release_leaves_no_identity_for_the_next_contender(
        self, tmp_path: Path
    ) -> None:
        with held(tmp_path) as lock:
            path = lock.path
        assert path.read_text() == ""

    def test_releasing_twice_is_not_an_error(self, tmp_path: Path) -> None:
        lock = held(tmp_path).acquire()
        lock.release()
        lock.release()
        assert not lock.held

    def test_the_lock_file_is_owner_only(self, tmp_path: Path) -> None:
        """It records a run's identity beside a bearer credential for an account."""
        with held(tmp_path) as lock:
            mode = stat.S_IMODE(lock.path.stat().st_mode)
        assert mode == telegram_person.PRIVATE_FILE

    def test_the_lock_moves_with_the_session_it_guards(self, tmp_path: Path) -> None:
        """A lock left behind would refuse a run that was sharing nothing."""
        moved = {telegram_person.PERSON_DIRECTORY_VARIABLE: str(tmp_path / "elsewhere")}
        assert (
            telegram_person.session_lock_path(telegram_person.person_directory(moved))
            == tmp_path / "elsewhere" / telegram_person.LOCK_FILE
        )


class TestTheCredentials:
    """§8 — the environment wins, the 0600 file is the fallback."""

    def test_the_environment_wins_over_the_file(self, tmp_path: Path) -> None:
        telegram_person.store_credentials(telegram_person.ApiCredentials(1, "from-file"), tmp_path)
        stated = {
            telegram_person.API_ID_VARIABLE: "2",
            telegram_person.API_HASH_VARIABLE: "from-environment",
        }
        assert telegram_person.load_credentials(tmp_path, stated) == (
            telegram_person.ApiCredentials(2, "from-environment")
        )

    def test_the_file_answers_when_the_environment_says_nothing(self, tmp_path: Path) -> None:
        telegram_person.store_credentials(telegram_person.ApiCredentials(1, "from-file"), tmp_path)
        assert telegram_person.load_credentials(tmp_path, {}) == telegram_person.ApiCredentials(
            1, "from-file"
        )

    def test_half_an_environment_is_not_half_an_answer(self, tmp_path: Path) -> None:
        """A stated `api_id` paired with a stored `api_hash` is a pair nobody chose."""
        telegram_person.store_credentials(telegram_person.ApiCredentials(1, "from-file"), tmp_path)
        stated = {telegram_person.API_ID_VARIABLE: "2"}
        assert telegram_person.load_credentials(tmp_path, stated).api_id == 1

    def test_neither_is_a_refusal_naming_the_one_time_login(self, tmp_path: Path) -> None:
        with pytest.raises(telegram_person.PersonError) as refused:
            telegram_person.load_credentials(tmp_path, {})
        assert telegram_person.API_ID_VARIABLE in str(refused.value)
        assert "login" in str(refused.value)

    def test_the_stored_file_is_owner_only(self, tmp_path: Path) -> None:
        path = telegram_person.store_credentials(
            telegram_person.ApiCredentials(1, "hash"), tmp_path
        )
        assert stat.S_IMODE(path.stat().st_mode) == telegram_person.PRIVATE_FILE
        assert stat.S_IMODE(tmp_path.stat().st_mode) == telegram_person.PRIVATE_DIRECTORY

    def test_nothing_about_the_account_is_hard_coded(self) -> None:
        """Both variables and the directory are overridable; no value is in the source."""
        source = (Path(__file__).resolve().parent / "acceptance" / "telegram_person.py").read_text()
        assert "my.telegram.org" in source, "the source may name where a person gets them"
        assert "api_hash=" not in source


def _at_once() -> dict[str, object]:
    """A clock that has already run out, so a test never sleeps for an answer.

    `deadlines.wait` takes its clock and its sleep for exactly this: the rule
    being checked is what an *expired* read answers, and the second it waits is
    the width of a real two-syscall window, not something a test has to spend.
    """
    return {"now": _Elapsed(), "sleep": lambda _: None}


class _Elapsed:
    """Monotonic, and already past any budget on the second reading."""

    def __init__(self) -> None:
        self.readings = 0.0

    def __call__(self) -> float:
        self.readings += deadlines.DEADLINES["HOLDER_RECORD_SECONDS"]
        return self.readings


def _a_reclaimed_pid() -> int:
    """A pid nothing on this machine holds, found rather than assumed."""
    for candidate in range(99999, 90000, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise AssertionError("no free pid to stand in for a dead holder")  # pragma: no cover
