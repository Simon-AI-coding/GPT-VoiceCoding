"""The run can start or refuse, checked at CI speed (#351).

The real run needs this machine, these credentials and those bots, so none of it
runs in CI. §5's refusals are the exception that has to: a refusal is the harness
deciding **not** to spend five minutes, and a refusal that does not fire is
invisible — the run simply goes red later, somewhere else, for a reason nobody
attributes to the environment. So every row of §5's table is driven here against
a machine that is wrong in exactly one way.

What each class pins:

* `TestEveryRefusal` — one test per row of `docs/acceptance-design.md` §5, plus
  the arranged machine that must **not** refuse.
* `TestTheOrder` — the session lock is taken before anything two runs collide
  over, and the costly check is last but for the two HTTP calls.
* `TestWhatARefusalWrites` — non-zero exit, a valid `verdict.json` naming the
  refusal, five SKIPPED rows per lane, and no engine started.
* `TestTheProbe` — §2 item 0b: SIGINT and never a kill, and the frame count read
  out of the probe's own words.
* `TestCredentialsReachNoArtifact` — §8, read off a written journal and verdict.
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import items
import preflight
import pytest
import support
import telegram_person
from items import Item

from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramError
from gpt_voicecoding.installation import codex_runtime

ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"
REPOSITORY = Path(__file__).resolve().parents[1]

#: The variable the operator's own config names, and the token each lane holds.
#: Spelled here so a test can assert the *name* reaches the journal and the value
#: never does.
TOKEN_VARIABLE = "GVC_TEST_BOT_TOKEN"
CLAUDE_TOKEN = "111:claude-lane-secret"  # noqa: S105 - a fake, and the point is it never lands
CODEX_TOKEN = "222:codex-lane-secret"  # noqa: S105 - the same
CHAT_ID = "424242"


def _bundle(root: Path) -> Path:
    bundle = root / "GPT-VoiceCoding.app"
    interpreter = bundle.joinpath(*support.ENGINE_PARTS, "bin", "python3")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    return bundle


def _source_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        "[adapters.settings.companion_channel]\n"
        f'token_env = "{TOKEN_VARIABLE}"\n'
        f'chat_id = "{CHAT_ID}"\n'
    )
    return path


def _claude_config(root: Path, *, authenticated: bool = True) -> Path:
    directory = root / "claude-config"
    directory.mkdir()
    recorded: dict[str, Any] = {"userID": "u"}
    if authenticated:
        recorded[preflight.CLAUDE_ACCOUNT_KEY] = {"emailAddress": "someone@example.com"}
    (directory / preflight.CLAUDE_STATE_NAME).write_text(json.dumps(recorded))
    return directory


def _answers(**overrides: Any) -> Any:
    """A Bot API that answers, unless a test says one call does not."""

    def ask(token: str, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if method in overrides:
            raise overrides[method]
        return {"username": f"bot-{token.split(':')[0]}", "id": int(token.split(":")[0])}

    return ask


def arranged(root: Path, **changes: Any) -> preflight.Machine:
    """A machine every check passes, with whatever one test breaks.

    Built rather than mocked: each field is the reading a check makes, so a test
    that changes one is a machine that is wrong in exactly that way — which is
    the only way to know a refusal fires *for its own reason*.
    """
    run_directory = root / "run"
    run_directory.mkdir()
    probe = root / "rt_prototype.py"
    probe.write_text("print('probe')\n")
    bundle = _bundle(root)
    settings: dict[str, Any] = {
        "lanes": items.LANES,
        "run_directory": run_directory,
        "repository": REPOSITORY,
        "environ": {TOKEN_VARIABLE: CLAUDE_TOKEN, f"{TOKEN_VARIABLE}_2": CODEX_TOKEN},
        "home": root,
        "bundle": bundle,
        "engine_socket": root / "nothing-listens-here.sock",
        "source_config": _source_config(root),
        "claude_config": _claude_config(root),
        "codex_home": root / ".codex",
        "codex_control_socket": root / ".codex" / "control.sock",
        "probe_script": probe,
        "writable_roots": (),
        "provenance": lambda: support.Provenance(bundle, "abc1234", True, ()),
        "path_of_login_shell": lambda: "/usr/local/bin:/usr/bin:/bin",
        "which": lambda binary, path: f"/usr/local/bin/{binary}",  # noqa: ARG005
        "foreign_codex": lambda: None,
        # Answers for the operator's shared Codex app-server and for nothing
        # else: a machine where *both* sockets answer is a machine with a live
        # engine on it, which is its own refusal.
        "server_live": lambda path: path == root / ".codex" / "control.sock",
        "ask_bot": _answers(),
        "take_session_lock": preflight._no_lock,
        "reconcile_trust": lambda _: (),
    }
    settings.update(changes)
    return preflight.Machine(**settings)


def refusal(machine: preflight.Machine, journal: support.Journal) -> preflight.Refused:
    with pytest.raises(preflight.Refused) as refused, preflight.Preflight(machine, journal):
        pass  # pragma: no cover - the context manager raises on entry
    return refused.value


@pytest.fixture
def journal(tmp_path: Path) -> support.Journal:
    return support.Journal(tmp_path / support.JOURNAL_NAME)


class TestEveryRefusal:
    """One test per row of §5's table, each fired by one wrong reading."""

    def test_an_arranged_machine_is_not_refused(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        with preflight.Preflight(arranged(tmp_path), journal) as passed:
            assert passed.startswith(support.JOURNAL_NAME)
        events = [line["event"] for line in journal.read()]
        assert "preflight.passed" in events

    def test_the_table_and_the_checks_are_the_same_fourteen(self) -> None:
        """§5 lists fifteen rows; the stale trust row is explicitly not a refusal."""
        table = [
            line
            for line in (REPOSITORY / "docs" / "acceptance-design.md").read_text().splitlines()
            if line.startswith("| ") and "|" in line[2:]
        ]
        assert len(preflight.Preflight.ORDER) == 14
        assert len(set(preflight.Preflight.ORDER)) == 14
        assert table, "the design document is local-only but must be present on this machine"

    def test_a_missing_bundle(self, tmp_path: Path, journal: support.Journal) -> None:
        refused = refusal(arranged(tmp_path, bundle=tmp_path / "nowhere.app"), journal)
        assert refused.check == "bundle"
        assert "no bundle at" in refused.reason

    def test_a_bundle_with_no_interpreter(self, tmp_path: Path, journal: support.Journal) -> None:
        empty = tmp_path / "Empty.app"
        empty.mkdir()
        refused = refusal(arranged(tmp_path, bundle=empty), journal)
        assert "no runnable engine interpreter" in refused.reason

    def test_a_bundled_interpreter_nothing_can_run(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """Present is not the question — the probe *runs* it (§2 item 0b).

        A `Popen` on an unrunnable interpreter raises where the probe's row
        should have been read, and a run-level row that was never written is a
        red nobody attributes to the bundle.
        """
        unrunnable = tmp_path / "Unrunnable.app"
        interpreter = unrunnable.joinpath(*support.ENGINE_PARTS, "bin", "python3")
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o644)
        refused = refusal(arranged(tmp_path, bundle=unrunnable), journal)
        assert refused.check == "bundle"
        assert "no runnable engine interpreter" in refused.reason

    def test_a_bundle_that_is_not_this_checkout(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        differs = support.Provenance(tmp_path, "abc1234", False, ("core/bridge.py differs",))
        refused = refusal(arranged(tmp_path, provenance=lambda: differs), journal)
        assert refused.check == "bundle"
        assert "core/bridge.py differs" in refused.reason

    def test_a_missing_realtime_probe_script(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, probe_script=None), journal)
        assert refused.check == "realtime probe script"
        assert preflight.REALTIME_PROBE_VARIABLE in refused.reason

    def test_a_live_engine_answering_the_products_shared_socket(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, server_live=lambda _: True), journal)
        assert refused.check == "live engine"
        assert "will not stop it for you" in refused.reason

    def test_a_run_root_codex_may_already_write(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """An `approval` that could never fire is worse than one that fails (§5)."""
        machine = arranged(tmp_path, writable_roots=(tmp_path,))
        refused = refusal(machine, journal)
        assert refused.check == "writable run root"
        assert support.ACCEPTANCE_ROOT_VARIABLE in refused.reason

    def test_a_run_root_under_the_operators_own_temporary_directory(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(
            tmp_path,
            environ={
                TOKEN_VARIABLE: CLAUDE_TOKEN,
                f"{TOKEN_VARIABLE}_2": CODEX_TOKEN,
                preflight.TEMPORARY_DIRECTORY_VARIABLE: str(tmp_path),
            },
        )
        assert refusal(machine, journal).check == "writable run root"

    def test_a_claude_config_directory_that_is_not_there(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, claude_config=tmp_path / "gone"), journal)
        assert refused.check == "claude config directory"
        assert "is not there" in refused.reason

    def test_a_claude_config_directory_nobody_logged_in_to(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """§4.1: a fresh directory is logged out, because the Keychain entry is keyed to it."""
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        directory = _claude_config(fresh, authenticated=False)
        refused = refusal(arranged(tmp_path, claude_config=directory), journal)
        assert refused.check == "claude config directory"
        assert "not authenticated" in refused.reason

    def test_the_claude_config_directory_is_not_read_when_that_lane_is_not_walked(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """A refusal about work nobody asked for is a false refusal (§6)."""
        machine = arranged(tmp_path, lanes=("codex",), claude_config=tmp_path / "gone")
        with preflight.Preflight(machine, journal):
            pass

    def test_no_shared_codex_app_server_at_the_derived_socket(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, server_live=lambda _: False), journal)
        assert refused.check == "codex app-server"
        assert "ADR 0022" in refused.reason or "would otherwise run its own core" in refused.reason

    def test_the_codex_app_server_is_not_dialled_when_that_lane_is_not_walked(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path, lanes=("claude",), server_live=lambda _: False)
        with preflight.Preflight(machine, journal):
            pass

    def test_another_run_holding_the_user_account_session(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        holder = telegram_person.LockHolder(
            4242, "/runs/20260910T000000Z", telegram_person.ACCEPTANCE_RUN_HOLDER
        )

        def taken() -> Any:
            raise telegram_person.SessionInUse(holder)

        refused = refusal(arranged(tmp_path, take_session_lock=taken), journal)
        assert refused.check == "session lock"
        assert "4242" in refused.reason and "20260910T000000Z" in refused.reason

    def test_a_lane_bot_token_variable_unset(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, environ={TOKEN_VARIABLE: CLAUDE_TOKEN}), journal)
        assert refused.check == "bot token variable"
        assert f"{TOKEN_VARIABLE}_2" in refused.reason

    def test_no_engine_configuration_to_read_the_variable_name_from(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, source_config=tmp_path / "gone.toml"), journal)
        assert refused.check == "bot token variable"

    def test_both_lanes_on_one_bot_token(self, tmp_path: Path, journal: support.Journal) -> None:
        machine = arranged(
            tmp_path,
            environ={TOKEN_VARIABLE: CLAUDE_TOKEN, f"{TOKEN_VARIABLE}_2": CLAUDE_TOKEN},
        )
        refused = refusal(machine, journal)
        assert refused.check == "one bot token for both lanes"
        assert "take each other's updates" in refused.reason

    def test_a_login_shell_path_that_cannot_be_read(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        refused = refusal(arranged(tmp_path, path_of_login_shell=lambda: None), journal)
        assert refused.check == "login shell PATH"
        assert "launchd" in refused.reason

    def test_an_agent_binary_absent_from_that_path(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(
            tmp_path,
            which=lambda binary, path: None if binary == "codex" else f"/usr/bin/{binary}",  # noqa: ARG005
        )
        refused = refusal(machine, journal)
        assert refused.check == "agent binary"
        assert "codex" in refused.reason

    def test_a_foreign_codex_session_live_on_the_machine(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path, foreign_codex=lambda: "pid 91 in /Users/simon/work")
        refused = refusal(machine, journal)
        assert refused.check == "foreign codex"
        assert "pid 91" in refused.reason

    def test_a_bot_that_does_not_answer_getme(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path, ask_bot=_answers(getMe=TelegramError("401", "Unauthorized")))
        refused = refusal(machine, journal)
        assert refused.check == "bot reachable"
        assert "dead token" in refused.reason

    def test_a_chat_the_account_never_started(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(
            tmp_path, ask_bot=_answers(getChat=TelegramError("400", "chat not found"))
        )
        refused = refusal(machine, journal)
        assert refused.check == "chat opened"
        assert "/start" in refused.reason

    def test_a_stale_codex_trust_row_is_reconciled_and_not_refused(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """§5's one row that is not a refusal — journalled before the lane starts (§4.1)."""
        left_behind = ("/runs/20260909T000000Z/workspace-codex",)
        machine = arranged(tmp_path, reconcile_trust=lambda _: left_behind)
        with preflight.Preflight(machine, journal):
            pass
        (recorded,) = [line for line in journal.read() if line["event"] == "trust.reconciled"]
        assert recorded["workspaces"] == list(left_behind)
        assert recorded["agent"] == "codex"

    def test_a_machine_with_no_stale_row_journals_no_reconciliation(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """§4.1's line is about a **removal**; a run that removed nothing did not."""
        with preflight.Preflight(arranged(tmp_path), journal):
            pass
        assert [one for one in journal.read() if one["event"] == "trust.reconciled"] == []


class TestTheOrder:
    """Two orderings that are not preference (§5, #203)."""

    def test_the_session_lock_precedes_every_credential_and_the_reconcile(self) -> None:
        order = preflight.Preflight.ORDER
        after = order[order.index("session lock") :]
        for later in ("bot token variable", "bot reachable", "chat opened"):
            assert later in after, f"{later} must be asked after the lock is held"

    def test_a_second_run_refuses_before_it_reads_a_token(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """The refusal has to arrive *before* the collision it exists to prevent."""
        asked: list[str] = []

        def ask(token: str, method: str, parameters: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
            asked.append(method)
            return {"username": "bot", "id": 1}

        def taken() -> Any:
            raise telegram_person.SessionInUse(
                telegram_person.LockHolder(7, "/runs/one", telegram_person.ACCEPTANCE_RUN_HOLDER)
            )

        refusal(arranged(tmp_path, take_session_lock=taken, ask_bot=ask), journal)
        assert asked == []

    def test_the_lock_is_released_when_a_later_check_refuses(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        released: list[str] = []

        class Lock:
            def release(self) -> None:
                released.append("released")

        machine = arranged(
            tmp_path, take_session_lock=Lock, foreign_codex=lambda: "pid 5 in /elsewhere"
        )
        refusal(machine, journal)
        assert released == ["released"]

    def test_the_one_costly_check_is_asked_after_every_free_one(self) -> None:
        order = preflight.Preflight.ORDER
        assert order.index("foreign codex") > order.index("login shell PATH")


class TestWhatARefusalWrites:
    """§5: non-zero exit, a valid verdict naming the refusal, and no engine started."""

    def test_a_refusal_is_refused_for_the_whole_run_with_a_skipped_row_per_lane_item(
        self, tmp_path: Path
    ) -> None:
        journal = support.Journal(tmp_path / support.JOURNAL_NAME)
        verdict = support.Verdict(
            run_id="20260910T000000Z",
            selection=items.select(),
            lanes=items.LANES,
            journal=journal,
        )
        verdict.refuse("live engine: the shell's engine is answering")
        document = verdict.document()
        assert document["result"] == "REFUSED"
        (row,) = [one for one in document["run"] if one["item"] == str(Item.PREFLIGHT)]
        assert row["verdict"] == "REFUSED"
        assert support.resolve(tmp_path, row["evidence"])["why"].startswith("live engine")
        for lane in items.LANES:
            skipped = document["lanes"][lane]
            assert len(skipped) == 5
            assert {one["verdict"] for one in skipped} == {"SKIPPED"}

    def test_nothing_a_check_does_starts_an_engine(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """Every check is a read (§5). The pin is that the run tree stays empty."""
        machine = arranged(tmp_path, foreign_codex=lambda: "pid 5 in /elsewhere")
        refusal(machine, journal)
        assert list(machine.run_directory.rglob("*")) == []

    def test_the_lane_runs_cannot_begin_before_preflight_has(self) -> None:
        """ "never starts an engine" holds by the fixture graph, not by care."""
        source = (ACCEPTANCE / "conftest.py").read_text()
        tree = compile(source, "conftest.py", "exec", flags=0, dont_inherit=True)
        assert tree is not None
        for name in ("lane_runs", "probe_run"):
            signature = source.split(f"def {name}(")[1].split(")")[0]
            assert "preflight" in signature, f"{name} must list preflight first"

    def test_a_refused_run_exits_non_zero_and_leaves_a_readable_verdict(
        self, tmp_path: Path
    ) -> None:
        """The whole thing, out of process: the exit code and the file a reader gets.

        Refused on the **first** check, so nothing after it is reached: no PATH is
        read, no socket is dialled and no token is looked for. That is what makes
        this cheap enough to keep in the fast suite.
        """
        root = tmp_path / "runs"
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "acceptance", str(ACCEPTANCE)],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                support.ACCEPTANCE_ROOT_VARIABLE: str(root),
                support.BUNDLE_VARIABLE: str(tmp_path / "no-such-bundle.app"),
            },
        )
        assert finished.returncode != 0, finished.stdout
        assert "preflight refused" in finished.stdout
        (written,) = list(root.glob(f"*/{support.VERDICT_NAME}"))
        document = json.loads(written.read_text())
        assert document["result"] == "REFUSED"
        (row,) = [one for one in document["run"] if one["item"] == str(Item.PREFLIGHT)]
        assert row["verdict"] == "REFUSED"
        for lane in items.LANES:
            assert len(document["lanes"][lane]) == 5
        run_directory = written.parent
        assert not list(run_directory.glob("engine-*/engine.log"))
        assert not list(run_directory.glob("pty-*.log"))


class TestTheProbe:
    """§2 item 0b — the maintainer's script, its deadline, and its SIGINT."""

    def test_the_frame_count_is_read_out_of_the_probes_own_words(self) -> None:
        frames, heard = preflight.frames_in(
            "first remote audio frame received\ntotal remote frames: 1493\n"
        )
        assert (frames, heard) == (1493, True)

    def test_a_probe_that_printed_no_total_received_nothing(self) -> None:
        assert preflight.frames_in("connecting…\n") == (0, False)

    def test_the_probe_is_ended_with_sigint_and_never_with_a_kill(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """A `kill()` loses the frame-count line, which the shutdown prints (§2)."""
        machine = arranged(tmp_path)
        signalled: list[int] = []

        class Probe:
            pid = 909
            returncode = 0
            waits = 0

            def wait(self, timeout: float) -> int:  # noqa: ARG002
                Probe.waits += 1
                if Probe.waits == 1:
                    raise subprocess.TimeoutExpired("probe", timeout)
                return 0

            def kill(self) -> None:  # pragma: no cover - a kill is the failure
                raise AssertionError("the probe must be interrupted, never killed")

        def popen(command: list[str], **_: Any) -> Probe:  # noqa: ARG001
            (machine.run_directory / preflight.PROBE_TRANSCRIPT_NAME).write_text(
                "total remote frames: 12\n"
            )
            return Probe()

        reading, evidence = preflight.run_realtime_probe(
            machine,
            journal,
            path="/usr/bin",
            popen=popen,
            interrupt=lambda pid, number: signalled.append(number),
        )
        assert signalled == [signal.SIGINT]
        assert reading.passed and reading.frames == 12
        assert support.resolve(tmp_path, evidence)["frames"] == 12

    def test_the_probe_runs_on_the_bundles_own_interpreter(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        """`aiortc` and `av` are in the `.app`; another Python proves another stack."""
        machine = arranged(tmp_path)
        seen: list[list[str]] = []

        class Probe:
            pid = 1
            returncode = 0

            def wait(self, timeout: float) -> int:  # noqa: ARG002
                return 0

        def popen(command: list[str], **_: Any) -> Probe:
            seen.append(command)
            (machine.run_directory / preflight.PROBE_TRANSCRIPT_NAME).write_text("")
            return Probe()

        preflight.run_realtime_probe(machine, journal, path="/usr/bin", popen=popen)
        (command,) = seen
        assert command[0] == str(support.bundled_python(machine.bundle))
        assert command[1] == str(machine.probe_script)
        assert "--silent" in command


class TestCredentialsReachNoArtifact:
    """§8 — read off what a run actually wrote, never asserted about intentions."""

    def test_a_journal_written_with_the_tokens_in_the_environment_carries_neither(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path)
        with preflight.Preflight(machine, journal):
            pass
        written = journal.path.read_text()
        assert CLAUDE_TOKEN not in written and CODEX_TOKEN not in written
        assert TOKEN_VARIABLE in written, "the variable's name is what a reader needs"

    def test_the_verdict_carries_no_token_either(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path)
        verdict = support.Verdict(
            run_id="20260910T000000Z",
            selection=items.select(),
            lanes=items.LANES,
            journal=journal,
        )
        with preflight.Preflight(machine, journal) as passed:
            verdict.record(Item.PREFLIGHT, support.PASS, passed)
        written = verdict.write(tmp_path / support.VERDICT_NAME).read_text()
        assert CLAUDE_TOKEN not in written and CODEX_TOKEN not in written

    def test_a_run_directory_is_scanned_clean_at_the_end(
        self, tmp_path: Path, journal: support.Journal
    ) -> None:
        machine = arranged(tmp_path)
        with preflight.Preflight(machine, journal):
            pass
        (machine.run_directory / support.JOURNAL_NAME).write_text(journal.path.read_text())
        assert (
            support.scan_for_credentials(
                machine.run_directory, (CLAUDE_TOKEN, CODEX_TOKEN, "an-api-hash")
            )
            == ()
        )

    def test_the_scan_is_a_reading_and_not_a_promise(self, tmp_path: Path) -> None:
        """A rule about what is absent is only as good as the reading that looks."""
        leaked = tmp_path / "engine-claude" / "config.toml"
        leaked.parent.mkdir(parents=True)
        leaked.write_text(f'token = "{CLAUDE_TOKEN}"\n')
        assert support.scan_for_credentials(tmp_path, (CLAUDE_TOKEN,)) == (
            "engine-claude/config.toml carries a credential",
        )

    def test_the_client_journals_no_api_id_hash_or_session_path(self) -> None:
        """§8: the user account's own credentials appear in no journal line."""
        source = (ACCEPTANCE / "telegram_person.py").read_text()
        for line in source.splitlines():
            if "_journal(" in line or "journal(" in line:
                for forbidden in ("api_id", "api_hash", "session_path", "credentials"):
                    assert forbidden not in line, line


class TestTheRealReadings:
    """The wiring `Machine.real()` does, and the three readers only it uses.

    Every check above is driven against a fake, which is what makes fourteen
    refusals affordable — and leaves exactly one thing untested: that the real
    machine is wired to the things that really answer. These are reads, so they
    are safe to run in CI; none of them dials anything off this host.
    """

    def test_the_real_machine_wires_every_reading(self, tmp_path: Path) -> None:
        machine = preflight.Machine.real(
            run_directory=tmp_path, repository=REPOSITORY, lanes=items.LANES
        )
        assert machine.bundle == support.bundle_path(dict(os.environ))
        assert machine.engine_socket.name.endswith(".sock")
        assert machine.codex_control_socket.parts[-2:] == codex_runtime.CONTROL_SOCKET_PARTS
        assert Path("/tmp") in machine.writable_roots
        assert machine.lanes == items.LANES

    def test_the_login_shell_path_is_bounded_by_its_two_sentinels(self) -> None:
        """A third sentinel means something other than the `printf` wrote one."""
        sentinel = preflight.PATH_SENTINEL

        def printed(text: str) -> Any:
            return lambda *_, **__: subprocess.CompletedProcess([], 0, text, "")

        stated = {"SHELL": "/bin/sh"}
        assert preflight.login_shell_path(stated, printed(f"hi\n{sentinel}/usr/bin{sentinel}")) == (
            "/usr/bin"
        )
        assert (
            preflight.login_shell_path(stated, printed(f"{sentinel}x{sentinel}y{sentinel}")) is None
        )
        assert preflight.login_shell_path(stated, printed("no sentinels")) is None
        assert preflight.login_shell_path(stated, printed(f"{sentinel}relative{sentinel}")) is None

    def test_a_shell_that_is_not_executable_reads_no_path_at_all(self) -> None:
        assert preflight.login_shell_path({"SHELL": "/no/such/shell"}) is None
        assert preflight.login_shell_path({}) is None

    def test_a_socket_file_nothing_listens_on_is_not_a_live_server(self, tmp_path: Path) -> None:
        """Debris, in the product's own word: an engine killed leaves its file.

        Both socket rows of §5 turn on this. A harness that read `exists()` would
        refuse every run after a `SIGKILL` until somebody deleted the file by
        hand — a false refusal, which is the false verdict pointed the other way.
        """
        stale = tmp_path / "app-server-control.sock"
        stale.write_text("")
        assert preflight.answering(stale) is False
        assert preflight.answering(tmp_path / "absent.sock") is False

    def test_a_socket_something_is_listening_on_is_live(self) -> None:
        # Bound under `/tmp` rather than under `tmp_path`: Darwin caps an
        # `AF_UNIX` path at 103 bytes and pytest's own is already longer.
        import socket as sockets
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as short:  # noqa: S108
            listening = Path(short) / "live.sock"
            with sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM) as server:
                server.bind(str(listening))
                server.listen(1)
                assert preflight.answering(listening) is True

    def test_the_operators_own_writable_roots_are_read_and_never_assumed(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "config.toml").write_text(
            '[sandbox_workspace_write]\nwritable_roots = ["~/scratch", "/opt/work"]\n'
        )
        assert preflight.codex_writable_roots(tmp_path) == (
            Path.home() / "scratch",
            Path("/opt/work"),
        )

    def test_a_codex_home_with_no_config_states_no_roots(self, tmp_path: Path) -> None:
        assert preflight.codex_writable_roots(tmp_path) == ()

    def test_the_probe_script_is_found_beside_the_primary_checkout(self) -> None:
        stated = {preflight.REALTIME_PROBE_VARIABLE: str(Path(__file__))}
        assert preflight.realtime_probe_path(REPOSITORY, stated) == Path(__file__)
        absent = {preflight.REALTIME_PROBE_VARIABLE: str(REPOSITORY / "no-such-probe.py")}
        assert preflight.realtime_probe_path(REPOSITORY, absent) is None

    def test_the_lanes_own_token_variable_is_the_second_lanes_suffix(self, tmp_path: Path) -> None:
        """§4.2 item 4: lane two's is lane one's name with `_2`."""
        machine = arranged(tmp_path)
        assert machine.token_variable(items.LANES[0]) == TOKEN_VARIABLE
        assert machine.token_variable(items.LANES[1]) == f"{TOKEN_VARIABLE}_2"


def test_the_two_chat_operations_and_no_more() -> None:
    """The interface ticket 4 couples to: `read` by id and `reply` to an id (§2, §9).

    Counted off the class rather than asserted about in prose: a `search`, a
    `latest` or a `messages_after` growing back here is the thing this rule
    exists to prevent, and the old harness had all three.
    """
    # Every public name, not only the functions: a `client` property handing out
    # the raw Telethon client is a search, a "latest" and everything else this
    # rule forbids, and `inspect.isfunction` cannot see one.
    public = {name for name in vars(telegram_person.PersonConnection) if not name.startswith("_")}
    assert {"read", "reply"} <= public
    assert public - {"read", "reply", "peer", "open", "close", "run"} == set()
    read = inspect.signature(telegram_person.PersonConnection.read)
    assert list(read.parameters) == ["self", "peer", "message_id"]
    reply = inspect.signature(telegram_person.PersonConnection.reply)
    assert list(reply.parameters) == ["self", "peer", "reply_to_message_id", "text"]
