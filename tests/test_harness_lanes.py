"""Two isolated lanes, checked at CI speed (#352).

The real run needs this machine, these agents and those bots. What can be read
at CI speed is everything about **isolation** — which is a design property and
not a cleanup routine (`docs/acceptance-design.md` §4), and therefore ordinary
code that either holds or does not:

* `TestTheDerivedConfig` — §4.2's eight separations and every drop, plus the one
  reading that matters most: the derived file is a configuration the *engine*
  could start from.
* `TestTheClaudeLanesConfigDirectory` — §4.1: the trust grant lands in the lane's
  own `CLAUDE_CONFIG_DIR` and the harness has no path to `~/.claude.json` at all.
* `TestTheCodexLanesTrustRow` — §4.1: one row in `CODEX_HOME`-if-set-else-
  `~/.codex`, written and removed as one block with a backup beside the file,
  and reconciled when a killed run left one behind.
* `TestTheHandStartedSession` — §4.4's launch rules, which are launch rules and
  not any step's (#73).
* `TestTheModelPins` — §3, including the one the Codex lane's roster row depends
  on: no `-c`, ever (#232).

Nothing here starts an engine, opens a chat or spends a turn.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

import deadlines
import hand_started
import items
import journey
import pytest
import support

from gpt_voicecoding import config as engine_config
from gpt_voicecoding.adapters.agent.codex import shared_daemon as codex_shared_daemon
from gpt_voicecoding.installation import claude_hooks, codex_runtime
from gpt_voicecoding.seams.identity import AgentKind

ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"

#: The operator's real configuration, in the shape this machine's actually has:
#: the launcher tables naming their real project directories, both agent kinds,
#: a `token_env` naming a variable, no `[engine] socket_path` and no `[log] path`
#: at all — the derived config has to *add* the separations, not only redirect
#: them.
SOURCE = """
[engine]

[launch]
default_agent = "claude"

[[launch.projects]]
name = "GPT-VoiceCoding"
workspace = "/Users/simon/Documents/coding/GPT-VoiceCoding"
spoken_aliases = ["voicecoding"]

[[launch.projects]]
name = "property"
workspace = "/Users/simon/Documents/coding/property"
spoken_aliases = ["property"]

[adapters]
call = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"
session_launcher = "gpt_voicecoding.adapters.session_launcher:direct_child_launcher"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"
codex = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.companion_channel]
token_env = "GVC_TEST_BOT_TOKEN"
chat_id = "8760621971"

[adapters.settings.call]
workspace = "~/code"

[policy]

[log]
max_bytes = 8388608
retained_files = 3
stripped_environment_prefixes = ["Malloc"]

[delegate]
model = "whatever-the-operator-last-used"
cli = "/Applications/GPT-VoiceCoding.app/Contents/Resources/engine/bin/bridgectl"
"""

CLAUDE_LANE, CODEX_LANE = items.LANES


def _source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "source-config.toml"
    path.write_text(SOURCE)
    return path


def _derived(root: Path, lane: str = CLAUDE_LANE) -> support.DerivedConfig:
    """One lane's derived configuration, with every separation §4.2 asks for."""
    return support.derive_config(
        source=_source(root),
        engine_directory=root / f"engine-{lane}",
        workspace=root / f"workspace-{lane}",
        socket_path=Path(f"/tmp/gvc-acceptance-{lane}/control.sock"),
        token_variable="GVC_TEST_BOT_TOKEN" if lane == CLAUDE_LANE else "GVC_TEST_BOT_TOKEN_2",
        codex_socket_directory=Path(f"/tmp/gvc-acceptance-{lane}"),
        delegate_model=journey.DELEGATED_TURN_MODEL,
        dropped_agents=journey.lane(lane).dropped_agents,
    )


def _code_strings(source: Path) -> list[str]:
    """Every string literal in a module that is **not** a docstring.

    The distinction is the whole point of reading the source rather than
    grepping it: a rule that says a path is never spelled has to survive being
    written about, and this harness's modules are mostly prose.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    documented = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
    ]


def _fake_bundle(root: Path, *, bridgectl: bool = False, exit_code: int = 0) -> Path:
    """A bundle shaped like the real one, with scripts that say what they were asked.

    Enough of the `.app` for the two things a lane does with it — run the
    engine on its own interpreter, and run its own `bridgectl` — and nothing
    else: what is being read here is the harness's launch, not the product.
    """
    bundle = root / "GPT-VoiceCoding.app"
    binaries = bundle.joinpath(*support.ENGINE_PARTS, "bin")
    binaries.mkdir(parents=True, exist_ok=True)
    interpreter = binaries / "python3"
    interpreter.write_text("#!/bin/sh\nexit 9\n")
    interpreter.chmod(0o755)
    if bridgectl:
        surface = binaries / "bridgectl"
        surface.write_text(f'#!/bin/sh\necho "$@"\nexit {exit_code}\n')
        surface.chmod(0o755)
    return bundle


def _journal(root: Path) -> support.Journal:
    """A real journal, because a trust arrangement is journalled and never graded."""
    return support.Journal(root / support.JOURNAL_NAME)


def _document(derived: support.DerivedConfig) -> dict:
    return tomllib.loads(derived.path.read_text())


class TestTheDerivedConfig:
    """§4.2 — the operator's real config with the eight separations replaced."""

    def test_the_engine_socket_state_and_log_are_the_runs_own(self, tmp_path: Path) -> None:
        derived = _derived(tmp_path)
        document = _document(derived)
        assert document["engine"]["socket_path"] == str(derived.socket_path)
        assert Path(document["engine"]["state_path"]) == derived.state_path
        assert Path(document["log"]["path"]) == derived.log_path
        assert derived.state_path.parent == tmp_path / f"engine-{CLAUDE_LANE}"
        assert derived.log_path.parent == tmp_path / f"engine-{CLAUDE_LANE}"

    def test_the_engine_is_told_which_variable_holds_this_lanes_token(self, tmp_path: Path) -> None:
        """§4.2 item 4: the engine is *told* its token variable, never given a token."""
        derived = _derived(tmp_path, CODEX_LANE)
        channel = _document(derived)["adapters"]["settings"]["companion_channel"]
        assert channel["token_env"] == "GVC_TEST_BOT_TOKEN_2"
        assert derived.token_variable == "GVC_TEST_BOT_TOKEN_2"

    def test_the_chat_is_the_one_the_operator_configured(self, tmp_path: Path) -> None:
        """§4.2 item 5: one private chat per bot — and a private chat is the *account*.

        A Telegram private chat is addressed by the user's own id, which is the
        same whichever bot is talking, so the separation is carried entirely by
        item 4's token. There is nothing to replace here, and the value is
        carried out so the run can read by id without parsing the config again.
        """
        derived = _derived(tmp_path, CODEX_LANE)
        assert derived.chat_id == "8760621971"

    def test_the_engines_own_codex_app_server_socket_is_this_lanes(self, tmp_path: Path) -> None:
        """§4.2 item 6: keyed per machine by default, so lane two's engine died at start."""
        derived = _derived(tmp_path, CODEX_LANE)
        settings = _document(derived)["adapters"]["settings"]
        assert settings["agent.codex"]["socket_directory"] == f"/tmp/gvc-acceptance-{CODEX_LANE}"

    def test_the_delegated_turn_is_billed_to_the_lane_model_and_not_the_operators(
        self, tmp_path: Path
    ) -> None:
        """§3: `[delegate] model` is pinned so no path bills what the operator last used."""
        assert _document(_derived(tmp_path))["delegate"]["model"] == journey.DELEGATED_TURN_MODEL
        assert journey.DELEGATED_TURN_MODEL == journey.lane(CODEX_LANE).model

    def test_the_launcher_tables_are_dropped(self, tmp_path: Path) -> None:
        """The real config names the operator's own project directories."""
        derived = _derived(tmp_path)
        document = _document(derived)
        assert "launch" not in document
        assert "session_launcher" not in document["adapters"]
        written = derived.path.read_text()
        assert "[launch]" not in written and "[[launch.projects]]" not in written
        assert "/Users/simon/Documents/coding/property" not in written

    def test_the_codex_lane_drops_the_claude_agent_kind_and_its_settings(
        self, tmp_path: Path
    ) -> None:
        """§4.3: one claimant for the machine's one Claude approval address (#202)."""
        document = _document(_derived(tmp_path, CODEX_LANE))
        assert set(document["adapters"]["agents"]) == {CODEX_LANE}
        assert "agent.claude" not in document["adapters"]["settings"]

    def test_the_claude_lane_keeps_both_agent_kinds(self, tmp_path: Path) -> None:
        assert set(_document(_derived(tmp_path))["adapters"]["agents"]) == set(items.LANES)

    def test_what_was_dropped_is_written_down_beside_the_derived_config(
        self, tmp_path: Path
    ) -> None:
        derived = _derived(tmp_path, CODEX_LANE)
        dropped = json.loads((derived.path.parent / support.DROPPED_NAME).read_text())
        assert set(dropped) == {
            "launch",
            "adapters.session_launcher",
            f"adapters.agents.{CLAUDE_LANE}",
        }
        assert tuple(dropped) == derived.dropped

    def test_the_derived_config_is_one_the_engine_could_start_from(self, tmp_path: Path) -> None:
        """The reading that catches a drop that leaves the engine unable to start.

        `[adapters.settings]` is checked against the seams the engine actually
        built, so a settings table for an adapter that is no longer listed is a
        key that "names no seam this engine fills" — the refusal that stopped
        **both** engines on run `20260902T013222Z`.
        """
        for lane in items.LANES:
            loaded = engine_config.load(_derived(tmp_path / lane, lane).path)
            assert loaded.delegated_turn_model == journey.DELEGATED_TURN_MODEL

    def test_the_token_itself_is_never_in_the_config(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        """§4.2: the engine is told a variable name; the value stays in the environment."""
        monkeypatch.setenv("GVC_TEST_BOT_TOKEN", "111:a-real-looking-token")
        derived = _derived(tmp_path)
        assert "111:a-real-looking-token" not in derived.path.read_text()
        assert not support.scan_for_credentials(tmp_path, ["111:a-real-looking-token"])

    def test_the_workspace_is_the_lanes_own_and_is_carried_out(self, tmp_path: Path) -> None:
        """§4.2 item 7: the fresh `git init` directory the trust grant's subject is."""
        assert _derived(tmp_path).workspace == tmp_path / f"workspace-{CLAUDE_LANE}"


class TestTheClaudeLanesConfigDirectory:
    """§4.1 — the lane owns a `CLAUDE_CONFIG_DIR`, and writes the run's trust into it.

    The variable moves everything the lane touches: settings and hooks, the
    session registry, transcripts, credentials, and the trust grant
    (`$CLAUDE_CONFIG_DIR/.claude.json`, on #217's measurement). So the grant a
    run writes lives in the lane's directory and nowhere else, and there is
    nothing to reconcile after a killed run.
    """

    def _environment(self, directory: Path) -> dict[str, str]:
        return {claude_hooks.CONFIG_DIRECTORY_VARIABLE: str(directory)}

    def test_the_grant_lands_in_the_lanes_own_config_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "claude-config"
        directory.mkdir()
        (directory / support.CLAUDE_STATE_NAME).write_text(json.dumps({"projects": {}}))
        workspace = tmp_path / "workspace-claude"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CLAUDE_LANE,
            environment=self._environment(directory),
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            state = json.loads((directory / support.CLAUDE_STATE_NAME).read_text())
        assert state["projects"][str(workspace)][support.CLAUDE_TRUST_KEY] is True

    def test_both_spellings_are_granted_when_they_differ(self, tmp_path: Path) -> None:
        """Resolved and realpath: `claude agents --json` reports a Session's `cwd` resolved."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        workspace = link / "workspace-claude"
        workspace.mkdir()
        directory = tmp_path / "claude-config"
        directory.mkdir()
        (directory / support.CLAUDE_STATE_NAME).write_text("{}")
        with support.TrustGate(
            workspace,
            agent=CLAUDE_LANE,
            environment=self._environment(directory),
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            granted = json.loads((directory / support.CLAUDE_STATE_NAME).read_text())["projects"]
        assert set(granted) == {str(workspace), os.path.realpath(workspace)}
        assert all(one[support.CLAUDE_TRUST_KEY] is True for one in granted.values())

    def test_an_entry_that_says_false_is_granted_and_its_other_keys_kept(
        self, tmp_path: Path
    ) -> None:
        """The **value** is checked, not the key's presence (#217, by a second route).

        An entry carrying the operator's `allowedTools` and no verdict at all —
        or one that says `false` — answers "not trusted" while being present, so
        `path not in projects` reported `trust.already` and granted nothing.
        """
        directory = tmp_path / "claude-config"
        directory.mkdir()
        workspace = tmp_path / "workspace-claude"
        workspace.mkdir()
        (directory / support.CLAUDE_STATE_NAME).write_text(
            json.dumps(
                {
                    "projects": {
                        str(workspace): {
                            support.CLAUDE_TRUST_KEY: False,
                            "allowedTools": ["Read"],
                        },
                        "/a/directory/the/operator/opened/once": {"allowedTools": ["Bash"]},
                        "/somewhere/of/the/operators": {support.CLAUDE_TRUST_KEY: True},
                    }
                }
            )
        )
        with support.TrustGate(
            workspace,
            agent=CLAUDE_LANE,
            environment=self._environment(directory),
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            projects = json.loads((directory / support.CLAUDE_STATE_NAME).read_text())["projects"]
        assert projects[str(workspace)][support.CLAUDE_TRUST_KEY] is True
        assert projects[str(workspace)]["allowedTools"] == ["Read"]
        assert projects["/somewhere/of/the/operators"] == {support.CLAUDE_TRUST_KEY: True}

    def test_the_harness_has_no_path_to_the_operators_own_claude_state(self) -> None:
        """§4.1: `~/.claude.json` and `~/.claude/` are never touched.

        The old harness resolved this file from the *home directory* when the
        variable was unset, which is #217's second half: a grant written where
        the Session was not reading. There is no such fallback now — an unset
        variable is a mistake at the call site, because the lane's directory is
        the only file this harness may write a grant into.
        """
        with pytest.raises(support.NoConfigDirectory):
            support.claude_state_path({})
        with pytest.raises(support.NoConfigDirectory):
            support.claude_state_path({claude_hooks.CONFIG_DIRECTORY_VARIABLE: "  "})

    def test_the_claude_state_file_is_spelled_once_and_joined_in_one_place(self) -> None:
        """Read off the modules, because "never touched" is only as good as the reading.

        Two readings, and between them there is no path from this harness to
        `~/.claude.json` or `~/.claude/`: the file's name exists in exactly one
        module, and the one function that turns it into a path resolves the
        directory through the product's own `CLAUDE_CONFIG_DIR` rule and never
        sees a home directory at all.
        """
        spelled = sorted(
            f"{source.name}: {value!r}"
            for source in sorted(ACCEPTANCE.glob("*.py"))
            for value in _code_strings(source)
            if ".claude" in value
        )
        assert spelled == [f"support.py: {support.CLAUDE_STATE_NAME!r}"], spelled
        joining = inspect.getsource(support.claude_state_path)
        assert "default_config_directory" in joining
        assert "home" not in joining.split('"""')[-1]

    def test_a_grant_writes_nothing_under_the_home_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        directory = tmp_path / "claude-config"
        directory.mkdir()
        workspace = tmp_path / "workspace-claude"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CLAUDE_LANE,
            environment=self._environment(directory),
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
            home=home,
        ):
            pass
        assert sorted(one.name for one in home.iterdir()) == []


class TestTheCodexLanesTrustRow:
    """§4.1 — one row in the operator's own file, and the one place the rule bends."""

    def _config(self, root: Path, *, existing: str = "") -> Path:
        codex_home = root / "dot-codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        path = codex_home / support.CONFIG_NAME
        path.write_text(
            existing
            or 'model = "gpt-5.6-luna"\n\n[projects."/Users/simon/x"]\ntrust_level = "trusted"\n'
        )
        return path

    def test_the_path_follows_codex_home_and_is_never_hardcoded(self, tmp_path: Path) -> None:
        """#219 is the bug this sentence exists to bury."""
        stated = tmp_path / "elsewhere"
        assert (
            support.codex_config_path({codex_runtime.CODEX_HOME_VARIABLE: str(stated)})
            == stated / support.CONFIG_NAME
        )
        assert support.codex_config_path({}, home=tmp_path) == (
            tmp_path / codex_runtime.DEFAULT_CODEX_HOME_NAME / support.CONFIG_NAME
        )

    def test_exactly_one_row_is_written_and_the_operators_file_is_otherwise_untouched(
        self, tmp_path: Path
    ) -> None:
        path = self._config(tmp_path)
        before = path.read_text()
        workspace = tmp_path / "workspace-codex"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CODEX_LANE,
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(path.parent)},
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            during = path.read_text()
            written = tomllib.loads(during)
        assert before in during
        assert set(written["projects"]) == {"/Users/simon/x", os.path.realpath(workspace)}
        assert written["projects"][os.path.realpath(workspace)] == {"trust_level": "trusted"}

    def test_the_row_is_removed_at_teardown_and_the_file_is_as_it_was(self, tmp_path: Path) -> None:
        path = self._config(tmp_path)
        before = path.read_text()
        workspace = tmp_path / "workspace-codex"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CODEX_LANE,
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(path.parent)},
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            pass
        assert path.read_text() == before

    def test_a_backup_sits_beside_the_file_before_a_byte_is_written(self, tmp_path: Path) -> None:
        """A read-modify-write of the operator's real file, done as one block."""
        path = self._config(tmp_path)
        before = path.read_text()
        workspace = tmp_path / "workspace-codex"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CODEX_LANE,
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(path.parent)},
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            (backup,) = sorted(path.parent.glob(f"{support.CONFIG_NAME}.*"))
            assert backup.read_text() == before

    def test_a_missing_file_is_an_answer_rather_than_a_crash(self, tmp_path: Path) -> None:
        """Codex's own credentials and config are the operator's; an absent file is theirs too."""
        workspace = tmp_path / "workspace-codex"
        workspace.mkdir()
        with support.TrustGate(
            workspace,
            agent=CODEX_LANE,
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(tmp_path / "nothing-here")},
            journal=_journal(tmp_path),
            run_id="20260910T000000Z",
        ):
            pass


class TestReconcilingWhatAKilledRunLeft:
    """§4.1 and §5's one row that is *not* a refusal."""

    def test_a_row_under_the_acceptance_root_is_removed_and_named(self, tmp_path: Path) -> None:
        root = tmp_path / "acceptance"
        stale = root / "20260901T000000Z" / "workspace-codex"
        stale.mkdir(parents=True)
        codex_home = tmp_path / "dot-codex"
        codex_home.mkdir()
        path = codex_home / support.CONFIG_NAME
        path.write_text(
            'model = "gpt-5.6-luna"\n\n'
            '[projects."/Users/simon/x"]\ntrust_level = "trusted"\n\n'
            f'[projects."{stale}"]\ntrust_level = "trusted"\n'
        )
        removed = support.reconcile_codex_trust(
            root, environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)}
        )
        assert removed == (str(stale),)
        left = tomllib.loads(path.read_text())
        assert set(left["projects"]) == {"/Users/simon/x"}
        assert left["model"] == "gpt-5.6-luna"

    def test_a_row_of_the_operators_own_is_left_alone(self, tmp_path: Path) -> None:
        codex_home = tmp_path / "dot-codex"
        codex_home.mkdir()
        path = codex_home / support.CONFIG_NAME
        original = '[projects."/Users/simon/coding/property"]\ntrust_level = "trusted"\n'
        path.write_text(original)
        assert (
            support.reconcile_codex_trust(
                tmp_path / "acceptance",
                environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)},
            )
            == ()
        )
        assert path.read_text() == original

    def test_no_file_is_nothing_removed(self, tmp_path: Path) -> None:
        assert (
            support.reconcile_codex_trust(
                tmp_path / "acceptance",
                environment={codex_runtime.CODEX_HOME_VARIABLE: str(tmp_path / "absent")},
            )
            == ()
        )


#: A child that proves what §4.4's rules are for, and nothing else: it opens its
#: **controlling** terminal (which is what `ps -o tty=` names and what the Codex
#: roster is built on), says so, and then echoes one line back so a typed turn
#: can be read as having arrived.
CHILD = (
    "import os, sys\n"
    "os.write(os.open('/dev/tty', os.O_RDWR), b'CONTROLLING\\n')\n"
    "sys.stdout.write('READY ' + repr(sorted(os.environ)) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdout.write('TYPED ' + sys.stdin.readline())\n"
    "sys.stdout.flush()\n"
)


class TestTheHandStartedSession:
    """§4.4 — starting a Session the product will list. Launch rules, not a step's (#73)."""

    def test_the_environment_is_scrubbed_of_every_agent_marker(self) -> None:
        """A `claude` that inherits these is treated as a child session — #73.

        Transcript off, and *absent from `claude agents --json` altogether*, so a
        harness that did not scrub them would start runs the product is right to
        ignore and read the roster's correct silence as a bug.
        """
        inherited = {
            "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/whatever.sock",
            "CLAUDE_CODE_SSE_PORT": "1234",
            "CLAUDECODE": "1",
            "CLAUDE_PID": "999",
            "CLAUDE_EFFORT": "high",
            "HOME": "/Users/simon",
            "LANG": "en_NZ.UTF-8",
        }
        arranged = hand_started.terminal_environment("/usr/bin", base=inherited)
        assert not [name for name in arranged if name.startswith("CLAUDE")]
        assert arranged["HOME"] == "/Users/simon"
        assert arranged["LANG"] == "en_NZ.UTF-8"

    def test_home_and_path_are_extended_and_never_replaced(self) -> None:
        """§4.4: or the agent cannot authenticate, and the engine finds neither binary."""
        arranged = hand_started.terminal_environment(
            "/opt/homebrew/bin:/usr/bin",
            base={"HOME": "/Users/simon", "PATH": "/usr/bin:/Users/simon/.local/bin"},
        )
        entries = arranged["PATH"].split(os.pathsep)
        assert entries[:2] == ["/opt/homebrew/bin", "/usr/bin"]
        assert "/Users/simon/.local/bin" in entries
        assert len(entries) == len(set(entries))
        assert arranged["HOME"] == "/Users/simon"

    def test_the_lanes_config_directory_is_exported_into_the_session(self) -> None:
        """§4.1: the variable moves the registry, the transcripts and the trust grant."""
        arranged = hand_started.terminal_environment(
            "/usr/bin",
            base={"HOME": "/Users/simon"},
            extra={claude_hooks.CONFIG_DIRECTORY_VARIABLE: "/somewhere/claude-config"},
        )
        assert arranged[claude_hooks.CONFIG_DIRECTORY_VARIABLE] == "/somewhere/claude-config"

    def test_the_binary_is_resolved_and_a_shell_function_is_not_the_command(
        self, tmp_path: Path
    ) -> None:
        """The operator's `~/.zshrc` wraps both names into another product (§4.4).

        Resolution is a PATH lookup for an executable **file**, so a function of
        that name cannot apply: there is no shell in the launch at all.
        """
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert hand_started.resolve("claude", str(tmp_path)) == binary
        assert hand_started.resolve("claude", "/nowhere") is None

    def test_the_boot_prompt_is_last_and_may_not_be_empty(self) -> None:
        """§3, #110: emptiness is the mechanism — `""` skips no update gate."""
        flags = ("-m", "gpt-5.6-luna")
        assert hand_started.launch_arguments(flags, "say READY") == (*flags, "say READY")
        assert hand_started.launch_arguments(flags, None) == flags
        with pytest.raises(ValueError, match="non-empty"):
            hand_started.launch_arguments(flags, "   ")

    def test_the_controlling_terminal_comes_from_an_exec_d_shim(self) -> None:
        """§4.4 and #208: a pty is not a controlling terminal, and `ps -o tty=` names only one.

        Pinned by reading the launch rather than by trusting it: the shim calls
        `login_tty`, the module never reaches for `preexec_fn` — this process is
        threaded by design, two lanes at once, which is the case Python
        documents `preexec_fn` as unsafe for — and `start_new_session` stays
        beside the shim because `stop()` reads a process group and `Popen`
        returns before the shim's own `setsid` has run (it killed a real run on
        2026-09-02).
        """
        assert "login_tty" in hand_started.TAKE_THE_TERMINAL
        assert "execv" in hand_started.TAKE_THE_TERMINAL
        source = (ACCEPTANCE / "hand_started.py").read_text()
        assert "preexec_fn" not in _code_strings(ACCEPTANCE / "hand_started.py")
        assert "preexec_fn=" not in source
        started = [
            keyword.value.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "start_new_session" and isinstance(keyword.value, ast.Constant)
        ]
        assert started == [True]

    def test_a_typed_turn_reaches_a_session_that_holds_its_own_terminal(
        self, tmp_path: Path
    ) -> None:
        """One real pty, one real child: the launch rules above, actually applied.

        Cheap enough for CI — the child is an interpreter that says what it can
        see — and it is the one reading that cannot be faked: a Session on a
        non-controlling pty is one the product is right to refuse (#208), so a
        harness whose launch quietly lost the terminal would grade correct
        product behaviour as a red.
        """
        session = hand_started.Session(
            lane=CLAUDE_LANE,
            binary=Path(sys.executable),
            arguments=("-c", CHILD),
            workspace=tmp_path,
            environment=hand_started.terminal_environment(os.environ["PATH"]),
            journal=_journal(tmp_path),
            transcript=tmp_path / "pty-claude.log",
        )
        session.start()
        try:
            assert session.pid is not None
            deadlines.wait(
                "ROSTER_SECONDS",
                lambda: "READY" in session.screen_tail(),
                what="the child saying what it can see",
            )
            session.submit("hello", sleep=lambda _: None)
            typed = deadlines.wait(
                "ROSTER_SECONDS",
                lambda: "TYPED hello" in session.screen_tail(),
                what="the typed line coming back",
            )
        finally:
            session.stop()
        assert typed
        transcript = (tmp_path / "pty-claude.log").read_text(errors="replace")
        assert "CONTROLLING" in transcript, transcript
        assert "no such device" not in transcript.lower()

    def test_the_scrub_is_evidenced_against_the_child_and_not_against_the_harness(
        self, tmp_path: Path
    ) -> None:
        """The row that rests on this line claims the *Session* was started clean.

        A count of the harness's own markers is not that claim: this process is
        run from inside a Claude Code session and always carries them, so a line
        computed from `os.environ` reads identically whether the launch scrubbed
        anything or not.
        """
        journal = _journal(tmp_path)
        session = hand_started.Session(
            lane=CLAUDE_LANE,
            binary=Path(sys.executable),
            arguments=("-c", "pass"),
            workspace=tmp_path,
            environment=hand_started.terminal_environment(
                os.environ["PATH"], base={"HOME": "/Users/simon", "CLAUDECODE": "1"}
            ),
            journal=journal,
            transcript=tmp_path / "pty-claude.log",
        )
        session.start()
        session.stop()
        (line,) = [one for one in journal.read() if one["event"] == "session.hand_started"]
        assert line["markers"] == []
        assert "CLAUDECODE" in line["scrubbed"]

    def test_the_settle_between_text_and_submit_is_the_named_one(self) -> None:
        """§4.4: `\\r` in the same burst reads as a newline, not a submit."""
        source = inspect.getsource(hand_started.Session.submit)
        assert "deadlines.SUBMIT_SETTLE_SECONDS" in source
        assert "rollout" not in source.lower(), (
            "never wait on a Codex rollout file before typing — it is written when the first "
            "turn starts, so waiting first is a guaranteed deadlock (§4.4)"
        )


class TestTheModelPins:
    """§3 — the pins are the lane's, and one of them decides whether roster can pass."""

    def test_the_claude_lane_pins_the_model_the_effort_and_the_products_own_mode(self) -> None:
        assert journey.lane(CLAUDE_LANE).arguments == (
            "--model",
            "sonnet",
            "--effort",
            "medium",
            "--permission-mode",
            "default",
        )

    def test_the_codex_lane_pins_the_model_and_nothing_else(self) -> None:
        assert journey.lane(CODEX_LANE).arguments == ("-m", "gpt-5.6-luna")

    def test_no_lane_launches_with_a_config_override(self) -> None:
        """#232: `can_reuse_implicit_local_daemon` requires **empty** overrides.

        It does not test *which* key was overridden, so there is no such thing
        as a harmless `-c` here: a `-c` passed for cost made the Codex lane's own
        Session invisible to the product it was there to grade, `roster` red and
        every item behind it SKIPPED.
        """
        for one in journey.LANES:
            argv = hand_started.launch_arguments(one.arguments, one.boot_words)
            assert "-c" not in argv, argv
            assert not [flag for flag in argv if flag.startswith("-c")], argv

    def test_only_the_codex_lane_is_launched_with_a_boot_prompt(self) -> None:
        """§3: the boot turn is Codex's update gate, and `claude` boots into a composer."""
        assert journey.lane(CLAUDE_LANE).boot_words is None
        assert journey.lane(CODEX_LANE).boot_words == journey.BOOT_WORDS
        assert journey.BOOT_WORDS.strip()


class TestTheCodexBootTurn:
    """§3 — a boot prompt is a real turn, settled on Codex's own bracketing (#110)."""

    def _rollout(self, root: Path, *records: tuple[str, str]) -> Path:
        path = root / "rollout-20260910T000000-abc.jsonl"
        path.write_text(
            "".join(
                json.dumps({"type": kind, "payload": {"type": payload}}) + "\n"
                for kind, payload in records
            )
        )
        return path

    def test_a_turn_is_over_when_every_started_turn_has_completed(self, tmp_path: Path) -> None:
        rollout = self._rollout(
            tmp_path, ("event_msg", "task_started"), ("event_msg", "task_complete")
        )
        assert hand_started.codex_turn_over(rollout)

    def test_a_turn_still_running_is_not_over_however_quiet_it_is(self, tmp_path: Path) -> None:
        """Not "the record stopped growing": a turn waiting on the model appends nothing."""
        assert not hand_started.codex_turn_over(
            self._rollout(tmp_path, ("event_msg", "task_started"))
        )

    def test_no_record_at_all_is_not_a_turn_that_ended(self, tmp_path: Path) -> None:
        assert not hand_started.codex_turn_over(None)
        assert not hand_started.codex_turn_over(tmp_path / "nothing.jsonl")

    def test_a_half_written_line_is_skipped_rather_than_raised_on(self, tmp_path: Path) -> None:
        rollout = self._rollout(
            tmp_path, ("event_msg", "task_started"), ("event_msg", "task_complete")
        )
        rollout.write_text(rollout.read_text() + '{"type": "event_msg", "payl')
        assert hand_started.codex_turn_over(rollout)


class TestTheLanesEngine:
    """§4.2 — a fresh engine from the installed bundle, on this lane's own separations."""

    def _engine(self, tmp_path: Path, **extra: str) -> support.Engine:
        return support.Engine(
            config=_derived(tmp_path),
            bundle=_fake_bundle(tmp_path),
            journal=_journal(tmp_path),
            token="111:a-real-looking-token",  # noqa: S106 - a fake, and it must not land
            path_value="/opt/homebrew/bin:/usr/bin",
            base={"HOME": "/Users/simon", "PATH": "/usr/bin:/Users/simon/.local/bin"},
            extra=extra,
        )

    def test_the_token_reaches_the_engine_under_the_name_its_config_names(
        self, tmp_path: Path
    ) -> None:
        """§4.2 item 4, from the other end: the config names a variable, this sets it."""
        environment = self._engine(tmp_path).environment
        assert environment["GVC_TEST_BOT_TOKEN"] == "111:a-real-looking-token"

    def test_home_and_path_are_extended_and_never_replaced(self, tmp_path: Path) -> None:
        """The real HOME is what `claude` keeps its login in; the PATH is what finds it."""
        environment = self._engine(tmp_path).environment
        assert environment["HOME"] == "/Users/simon"
        entries = environment["PATH"].split(os.pathsep)
        assert entries[0] == "/opt/homebrew/bin"
        assert "/Users/simon/.local/bin" in entries

    def test_the_claude_lanes_config_directory_reaches_the_engine_too(self, tmp_path: Path) -> None:
        """§4.1: the engine's own `claude agents --json` inherits the engine's environment."""
        environment = self._engine(
            tmp_path, **{claude_hooks.CONFIG_DIRECTORY_VARIABLE: "/somewhere/claude-config"}
        ).environment
        assert environment[claude_hooks.CONFIG_DIRECTORY_VARIABLE] == "/somewhere/claude-config"

    def test_an_engine_that_dies_before_binding_says_so_and_names_its_log(
        self, tmp_path: Path
    ) -> None:
        """ADR 0004: the engine owns its log, so nothing here redirects its output.

        Output before the engine adopts that log is discarded, and this silence
        is how an engine that died that early surfaces — with the log named, so
        the reader is sent to the one file that could say more.
        """
        engine = self._engine(tmp_path)
        with pytest.raises(support.EngineRefused) as refused:
            engine.start()
        assert str(engine.log_path) in str(refused.value)
        engine.stop()


class TestTheEnginesOwnCodexHome:
    """§4.3 — the Claude lane's engine sees no Codex thread on this machine (#355).

    The Codex agent kind cannot be dropped on that lane: `call` is a required
    seam, the only Call adapter rides the app-server that kind owns, and engine
    assembly refuses without it. So the isolation is environmental — an empty
    `CODEX_HOME` of the engine's own, in the engine's environment alone — and
    what it has to produce is a derived control socket with nobody behind it.

    Without it that engine joined the operator's shared app-server, saw the
    *other* lane's thread, and sent a Stop Notice for the Codex lane's boot turn
    to the Claude lane's chat 0.16 s before the Claude lane's own turn was typed
    (run `20260910T191643Z`).
    """

    #: The Session's own variables, composed before the engine's (§4.1). Passed in
    #: rather than invented here, because the engine carries them too.
    SESSION = {claude_hooks.CONFIG_DIRECTORY_VARIABLE: "/the/lanes/claude-config"}

    def _variables(self, tmp_path: Path, lane: str) -> dict[str, str]:
        return support.derive_engine_variables(
            self.SESSION,
            engine_directory=tmp_path / f"engine-{lane}",
            own_codex_home=journey.lane(lane).empty_codex_home,
        )

    def test_only_the_claude_lanes_engine_is_given_one(self) -> None:
        """ADR 0022 derives the socket and the TUI's launch environment from one
        `CODEX_HOME`, so the Codex lane cannot have its own (§4.1, #232)."""
        assert journey.lane(CLAUDE_LANE).empty_codex_home
        assert not journey.lane(CODEX_LANE).empty_codex_home

    def test_the_claude_lanes_engine_carries_an_empty_one_under_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        variables = self._variables(tmp_path, CLAUDE_LANE)
        home = Path(variables[codex_runtime.CODEX_HOME_VARIABLE])
        assert home.is_relative_to(tmp_path)
        assert home.is_dir()
        assert list(home.iterdir()) == []

    def test_the_codex_lanes_engine_carries_none_and_keeps_the_operators_own(
        self, tmp_path: Path
    ) -> None:
        assert self._variables(tmp_path, CODEX_LANE) == self.SESSION

    def test_the_socket_derived_from_it_has_nobody_behind_it(self, tmp_path: Path) -> None:
        """The whole mechanism, read through the product's own lookup: the shared
        daemon is found by a socket under `CODEX_HOME`, computed when it is
        *called*, so an empty directory is an engine that joins nothing."""
        home = Path(self._variables(tmp_path, CLAUDE_LANE)[codex_runtime.CODEX_HOME_VARIABLE])
        found, why = codex_shared_daemon.locate(codex_runtime.control_socket(home))
        assert found is None
        assert str(home) in why

    def test_the_hand_started_session_keeps_its_real_environment(self, tmp_path: Path) -> None:
        """§4.3: the engine's alone. The Codex lane's TUI must join the operator's
        real daemon or it is not a Session the product can list (#232), and the
        Claude lane's Session has no business with a Codex home either."""
        self._variables(tmp_path, CLAUDE_LANE)
        session = hand_started.terminal_environment(
            "/usr/bin",
            base={"HOME": "/Users/simon", codex_runtime.CODEX_HOME_VARIABLE: "/Users/simon/.codex"},
            extra=self.SESSION,
        )
        assert session[codex_runtime.CODEX_HOME_VARIABLE] == "/Users/simon/.codex"

    def test_the_variable_reaches_the_engine_process(self, tmp_path: Path) -> None:
        """The engine's own environment is what the shared-daemon lookup reads."""
        variables = self._variables(tmp_path, CLAUDE_LANE)
        engine = support.Engine(
            config=_derived(tmp_path),
            bundle=_fake_bundle(tmp_path),
            journal=_journal(tmp_path),
            token="111:a-real-looking-token",  # noqa: S106 - a fake, and it must not land
            path_value="/opt/homebrew/bin:/usr/bin",
            base={"HOME": "/Users/simon", codex_runtime.CODEX_HOME_VARIABLE: "/Users/simon/.codex"},
            extra=variables,
        )
        assert (
            engine.environment[codex_runtime.CODEX_HOME_VARIABLE]
            == variables[codex_runtime.CODEX_HOME_VARIABLE]
        )

    def test_what_the_engine_was_given_is_written_down_beside_the_derived_config(
        self, tmp_path: Path
    ) -> None:
        """§4.3: the variable is journalled. A run that changed an engine's
        environment without saying so leaves a reader with nothing to read."""
        variables = self._variables(tmp_path, CLAUDE_LANE)
        written = json.loads(
            (tmp_path / f"engine-{CLAUDE_LANE}" / support.VARIABLES_NAME).read_text()
        )
        assert written == variables
        assert codex_runtime.CODEX_HOME_VARIABLE in written


class TestTheSurface:
    """§2 — every product action goes through the bundle's own `bridgectl`."""

    def test_every_call_names_this_lanes_socket_and_lands_in_the_journal(
        self, tmp_path: Path
    ) -> None:
        journal = _journal(tmp_path)
        surface = support.Bridgectl(
            bundle=_fake_bundle(tmp_path, bridgectl=True),
            socket_path=tmp_path / "control.sock",
            journal=journal,
        )
        answer = surface("switch", "voice", "off")
        assert answer.ok
        assert f"--socket {tmp_path / 'control.sock'}" in answer.stdout
        assert "switch voice off" in answer.stdout
        (line,) = [one for one in journal.read() if one["event"] == "bridgectl"]
        assert line["command"] == ["switch", "voice", "off"]
        assert line["returncode"] == 0

    def test_a_refusal_is_an_answer_and_not_an_exception(self, tmp_path: Path) -> None:
        """`bridgectl` § Three exits: the engine answered, refused, or was not there."""
        surface = support.Bridgectl(
            bundle=_fake_bundle(tmp_path, bridgectl=True, exit_code=1),
            socket_path=tmp_path / "control.sock",
            journal=_journal(tmp_path),
        )
        answer = surface("status")
        assert not answer.ok
        assert answer.returncode == 1


class TestTheRosterRow:
    """§2 item 1 — a row that is a real target, matched the way the product joins them."""

    def _row(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "target": {"agent": CODEX_LANE, "session_id": "thread-1", "pid": 4242},
            "workspace": "/runs/workspace-codex",
            "name": "工位 · READY",
            "state": "idle",
        }
        return {**row, **overrides}

    def test_a_row_is_matched_by_session_id_where_the_agent_has_one(self) -> None:
        truth = hand_started.GroundTruth(
            session_id="thread-1", pid=1, workspace=Path("/runs/workspace-codex")
        )
        rows = [
            self._row(target={"agent": CODEX_LANE, "session_id": "another", "pid": 1}),
            self._row(),
        ]
        assert journey.roster_row(rows, truth) is rows[1]

    def test_a_row_is_matched_by_pid_where_it_has_none(self) -> None:
        """Codex has no session_id before its first turn (§2 item 1)."""
        truth = hand_started.GroundTruth(
            session_id="", pid=4242, workspace=Path("/runs/workspace-codex")
        )
        rows = [self._row(target={"agent": CODEX_LANE, "session_id": "x", "pid": 9}), self._row()]
        assert journey.roster_row(rows, truth) is rows[1]

    def test_a_roster_holding_nobody_matches_nothing(self) -> None:
        truth = hand_started.GroundTruth(session_id="x", pid=1, workspace=Path("/runs/w"))
        assert journey.roster_row([], truth) is None
        assert journey.roster_row([{"target": None}], truth) is None

    def test_a_real_target_is_an_address_plus_the_lanes_own_workspace(self) -> None:
        assert journey.not_a_target(self._row(), Path("/runs/workspace-codex")) is None
        assert "workspace" in str(journey.not_a_target(self._row(), Path("/runs/workspace-claude")))
        assert "address" in str(
            journey.not_a_target(
                self._row(target={"agent": CODEX_LANE, "session_id": None, "pid": None}),
                Path("/runs/workspace-codex"),
            )
        )


class TestTheSharedCodexDaemon:
    """§4.3 and #232 — a TUI outside the daemon is refused, never graded."""

    def test_an_observed_absence_refuses_the_lane_and_quotes_what_it_was_launched_with(
        self,
    ) -> None:
        membership = support.DaemonMembership(
            thread_id="thread-1", held=False, held_threads=("other",), daemon="/tmp/x.sock"
        )
        refusal = membership.refusal(("-m", "gpt-5.6-luna"))
        assert refusal is not None
        assert "thread-1" in refusal and "ADR 0020" in refusal
        assert "-m" in refusal

    def test_a_daemon_that_could_not_be_read_is_not_evidence_of_an_absence(self) -> None:
        """#96's rule: never claim anything about the daemon this run did not observe."""
        assert (
            support.DaemonMembership(thread_id="t", held=None, reason="not dialled").refusal(())
            is None
        )

    def test_a_thread_the_daemon_holds_refuses_nothing(self) -> None:
        assert support.DaemonMembership(thread_id="t", held=True).refusal(()) is None


class _Clock:
    """A clock that jumps a whole deadline every reading, so a wait runs out at once.

    The seam `deadlines.wait` documents, used the way `Walk` passes it: the real
    budgets are minutes — the boot turn is two turns long — and what a fast test
    reads is what the harness *does* when one runs out.
    """

    def __init__(self, step: float = max(deadlines.DEADLINES.values())) -> None:
        self.reading = 0.0
        self.step = step

    def __call__(self) -> float:
        answer = self.reading
        self.reading += self.step
        return answer


class _Surface:
    """A `bridgectl` that answers what a test arranged, and remembers what it was asked."""

    def __init__(self, payload: dict[str, Any] | None = None, *, ok: bool = True) -> None:
        self.payload = payload or {"sessions": []}
        self.ok = ok
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str, deadline: str | None = None) -> support.Answer:
        self.calls.append(arguments)
        return support.Answer(arguments, 0 if self.ok else 1, "done", "refused")

    def status_payload(self, *, why: str) -> dict[str, Any]:
        return self.payload


class _UnreadableSurface(_Surface):
    """A `bridgectl` nobody is behind yet: `status` raises rather than answers.

    The state the boot drain can genuinely meet — it reads the roster before
    `roster` has waited for anything (#356).
    """

    def status_payload(self, *, why: str) -> dict[str, Any]:
        raise OSError("the engine's control socket is not there yet")


def _unreadable_truth() -> Any:
    """An agent record that could not be read — the other way to have no row yet."""
    raise OSError("the agent's own record could not be read")


class _Engine:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or []

    def log_lines(self) -> list[str]:
        return list(self.lines)


class _Session:
    """The pty, as the walk uses it — including the two facts a failure names."""

    def __init__(self) -> None:
        self.transcript = Path(f"/runs/pty-{CODEX_LANE}.log")
        self.environment: dict[str, str] = {}

    def submit(self, words: str) -> None:
        return None

    def screen_tail(self) -> str:
        return "<the screen, which nothing parses>"


def _walk(tmp_path: Path, lane: str = CODEX_LANE, **overrides: Any) -> journey.Walk:
    journal = _journal(tmp_path)
    verdict = support.Verdict(
        run_id="20260910T000000Z",
        selection=overrides.pop("selection", items.select()),
        lanes=items.LANES,
        journal=journal,
    )
    arranged = {
        "lane": journey.lane(lane),
        "now": _Clock(),
        "sleep": lambda _: None,
        "selection": verdict.selection,
        "journal": journal,
        "verdict": verdict,
        "bridgectl": _Surface(),
        "engine": _Engine(),
        "session": _Session(),
        "workspace": tmp_path / f"workspace-{lane}",
        "run_directory": tmp_path,
        "truth": lambda: None,
    }
    arranged.update(overrides)
    return journey.Walk(**arranged)


def _truth(workspace: Path) -> Any:
    """The agent's own account of the Session the harness started."""
    return lambda: hand_started.GroundTruth(session_id="thread-1", pid=7, workspace=workspace)


def _rows(walk: journey.Walk) -> list[dict[str, Any]]:
    return walk.verdict.document()["lanes"][walk.lane.name]


class TestWalkingALane:
    """§2 and §6 — the ground, then the items, and what a failure does to the rest."""

    def _payload(self, workspace: Path, **target: Any) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "target": {"agent": CODEX_LANE, "session_id": "thread-1", "pid": 7, **target},
                    "workspace": str(workspace),
                    "name": "工位",
                    "state": "idle",
                }
            ]
        }

    def test_a_roster_row_that_is_a_real_target_passes_and_rests_on_a_journal_line(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir()
        walk = _walk(
            tmp_path,
            bridgectl=_Surface(self._payload(workspace)),
            truth=lambda: hand_started.GroundTruth(
                session_id="thread-1", pid=7, workspace=workspace
            ),
        )
        walk.walk()
        (roster,) = [row for row in _rows(walk) if row["item"] == str(items.Item.ROSTER)]
        assert roster["verdict"] == "PASS"
        line = support.resolve(tmp_path, roster["evidence"])
        assert line["event"] == "roster.row"
        assert line["address"] == f"{CODEX_LANE}:thread-1:7"

    def test_the_switches_are_armed_before_any_item_is_read(self, tmp_path: Path) -> None:
        """§4.2: Voice off, Message on, Duty on — a fresh engine answers all three off."""
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir()
        surface = _Surface(self._payload(workspace))
        walk = _walk(tmp_path, bridgectl=surface, truth=_truth(workspace))
        walk.walk()
        assert surface.calls[:3] == [
            ("switch", "voice", "off"),
            ("switch", "message", "on"),
            ("switch", "duty", "on"),
        ]

    def test_a_row_against_another_workspace_is_not_this_session(self, tmp_path: Path) -> None:
        """The join that makes the row this Session rather than a coincidence."""
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir()
        walk = _walk(
            tmp_path,
            bridgectl=_Surface(self._payload(tmp_path / "somebody-elses-workspace")),
            truth=lambda: hand_started.GroundTruth(
                session_id="thread-1", pid=7, workspace=workspace
            ),
        )
        walk.walk()
        (roster,) = [row for row in _rows(walk) if row["item"] == str(items.Item.ROSTER)]
        assert roster["verdict"] == "FAIL"
        assert "workspace" in support.resolve(tmp_path, roster["evidence"])["why"]

    def test_a_lane_whose_session_never_reaches_the_roster_fails_and_names_what_never_ended(
        self, tmp_path: Path
    ) -> None:
        """Ruling of #352: after preflight passed, this is a `roster` FAIL, never REFUSED.

        §7: a deadline hit is FAIL with the verdict naming what never ended, and
        the items behind it are SKIPPED `blocked by` that row — never missing,
        and never a silently skipped lane.
        """
        # The claude lane, which has no boot turn: what is being read is the
        # deadline on the *roster*, and this lane's clock runs one out per ask.
        walk = _walk(tmp_path, lane=CLAUDE_LANE, selection=items.select(["relay"]))
        walk.walk()
        rows = {row["item"]: row for row in _rows(walk)}
        assert rows[str(items.Item.ROSTER)]["verdict"] == "FAIL"
        # `roster` is *setup* under `--step relay`, so its row carries no
        # evidence of its own (§6): what it was arranging is what gets graded.
        assert rows[str(items.Item.ROSTER)]["graded"] is False
        (expired,) = [one for one in walk.journal.read() if one["event"] == "deadline.expired"]
        assert expired["deadline"] == "ROSTER_SECONDS"
        assert "the harness started" in expired["what"]
        assert rows[str(items.Item.RELAY)]["verdict"] == "SKIPPED"
        assert (
            support.resolve(tmp_path, rows[str(items.Item.RELAY)]["evidence"])["why"]
            == f"blocked by {CLAUDE_LANE}/{items.Item.ROSTER}"
        )

    def test_a_session_the_shared_daemon_does_not_hold_blocks_the_lane_rather_than_grading_it(
        self, tmp_path: Path
    ) -> None:
        """#232: nothing the product does can give that TUI a roster row (ADR 0020)."""
        walk = _walk(
            tmp_path,
            membership=lambda: support.DaemonMembership(
                thread_id="thread-1", held=False, held_threads=(), daemon="/tmp/codex.sock"
            ),
        )
        walk.walk()
        rows = {row["item"]: row for row in _rows(walk)}
        assert rows[str(items.Item.ROSTER)]["verdict"] == "SKIPPED"
        why = support.resolve(tmp_path, rows[str(items.Item.ROSTER)]["evidence"])["why"]
        assert "ADR 0020" in why and "blocked by" in why

    def test_a_daemon_nobody_could_read_blocks_nothing(self, tmp_path: Path) -> None:
        workspace = tmp_path / f"workspace-{CODEX_LANE}"
        workspace.mkdir()
        walk = _walk(
            tmp_path,
            bridgectl=_Surface(self._payload(workspace)),
            truth=_truth(workspace),
            membership=lambda: support.DaemonMembership(
                thread_id="thread-1", held=None, reason="the daemon could not be dialled"
            ),
        )
        walk.walk()
        rows = {row["item"]: row for row in _rows(walk)}
        assert rows[str(items.Item.ROSTER)]["verdict"] == "PASS"
        assert [one for one in walk.journal.read() if one["event"] == "daemon.membership"]


class TestTheMarkEveryChatReadStartsFrom:
    """§2 and §9 — a mark is a position in this lane's engine log, never a clock."""

    SENT = (
        "2026-09-10 sent Companion Channel message request=abc outcome=Delivery.DELIVERED "
        "message_ids=41"
    )

    #: The address of the Session `_truth` and `_held` below agree on, spelled
    #: from the roster row's three fields — the join the harness makes.
    OWN_ADDRESS = f"{CODEX_LANE}:thread-1:7"

    #: The same send line about this lane's own Session and about one that is not
    #: — the pair the boot drain has to tell apart (#356). One engine bridges
    #: every Session on the machine, so both are ordinary in that window.
    OWN = (
        "2026-09-10 sent Companion Channel message request=own outcome=Delivery.DELIVERED "
        f"target={OWN_ADDRESS} message_ids=42"
    )
    STRANGER = (
        "2026-09-10 sent Companion Channel message request=other outcome=Delivery.DELIVERED "
        f"target={CODEX_LANE}:01a08cc0-f956-7c63-ae40-95e80334cb9b:68569 message_ids=1806"
    )

    def _held(self, root: Path, **overrides: Any) -> journey.Walk:
        """A walk whose engine already holds a row for the Session it started."""
        workspace = root / f"workspace-{CODEX_LANE}"
        payload = {
            "sessions": [
                {
                    "target": {"agent": CODEX_LANE, "session_id": "thread-1", "pid": 7},
                    "workspace": str(workspace),
                    "state": "idle",
                }
            ]
        }
        return _walk(root, bridgectl=_Surface(payload), truth=_truth(workspace), **overrides)

    def _boot_notice(self, walk: journey.Walk) -> dict[str, Any]:
        (line,) = [one for one in walk.journal.read() if one["event"] == "boot.notice"]
        return line

    def test_a_mark_is_where_the_engine_log_had_got_to(self, tmp_path: Path) -> None:
        engine = _Engine(["one", "two"])
        walk = _walk(tmp_path, engine=engine)
        mark = walk.mark()
        assert mark == 2
        engine.lines.append(self.SENT)
        assert walk.sent_since(mark) == [self.SENT]
        assert walk.sent_since(mark + 1) == []

    def test_a_bound_drain_reads_this_lanes_own_send_and_never_the_newest(
        self, tmp_path: Path
    ) -> None:
        """#356: with a row to bind to, the drain selects as `stop notice` selects.

        The stranger's line is the **newer** of the two on purpose: an unbound
        drain answers with it, and that is the reading this issue replaced.
        """
        walk = self._held(tmp_path, engine=_Engine([self.OWN, self.STRANGER]))
        walk.drain_boot_notice(0)
        line = self._boot_notice(walk)
        assert line["drained"] == self.OWN
        assert line["bound"] is True
        assert line["address"] == self.OWN_ADDRESS

    def test_a_strangers_send_does_not_settle_a_bound_drain(self, tmp_path: Path) -> None:
        """The hole #356 was filed for, from the other side.

        A send about somebody else settled the drain early, `stop notice` took
        its mark, and this lane's own boot notice landed *behind* it — where the
        address filter (#355) admits it and the count binding (#354) matches. A
        bound drain waits it out instead, and the wait running out is still not a
        failure: no notice at all remains a legitimate answer.
        """
        walk = self._held(tmp_path, engine=_Engine([self.STRANGER]))
        walk.drain_boot_notice(0)
        line = self._boot_notice(walk)
        assert line["drained"] is None
        assert line["bound"] is True
        assert line["address"] == self.OWN_ADDRESS

    def test_the_boot_notice_is_drained_on_a_mark_taken_behind_the_boot_turn(
        self, tmp_path: Path
    ) -> None:
        """§3: so it can never read as a later `stop notice` green (#109's shape).

        With no row there is no address to bind to and the first send after the
        mark is the answer — which is also the only sound reading: an engine
        holding no row by the time the boot turn is over never saw this Session
        active and will raise no Stop for it (#356). The line it drains here
        carries no `target=` field at all, which is what a caller with no address
        has to be able to read.
        """
        engine = _Engine(["Session stopped: 工位", self.SENT])
        walk = _walk(tmp_path, engine=engine)
        walk.drain_boot_notice(0)
        line = self._boot_notice(walk)
        assert line["drained"] == self.SENT
        assert line["bound"] is False
        assert line["address"] is None

    def test_no_notice_at_all_is_a_legitimate_answer(self, tmp_path: Path) -> None:
        """A Stop is raised on a transition out of `active`; an already-idle thread raises none."""
        walk = _walk(tmp_path)
        walk.drain_boot_notice(0)
        line = self._boot_notice(walk)
        assert line["drained"] is None
        assert line["bound"] is False

    def test_the_address_the_drain_reads_is_never_a_raise(self, tmp_path: Path) -> None:
        """It reads the roster before `roster` does, where every absence is an answer.

        `row()` is the raising read and is the wrong one here: no agent record
        yet, no row yet, a `status` payload of a shape nobody can read, an engine
        whose socket cannot be dialled at all, and a row that matches but carries
        too little to render an address from are all "no address to bind to". A
        drain that raised would fail a lane on ground `roster` has not graded yet
        (#356).
        """
        matched_but_unrenderable = {"sessions": [{"target": {"session_id": "thread-1"}}]}
        unreadable: list[tuple[Any, Any]] = [
            (_Surface({}), _truth(tmp_path)),
            (_Surface({"sessions": 7}), _truth(tmp_path)),
            (_Surface({"sessions": [{"target": None}]}), _truth(tmp_path)),
            (_Surface(matched_but_unrenderable), _truth(tmp_path)),
            (_Surface(), lambda: None),
            (_UnreadableSurface(), _truth(tmp_path)),
            (_Surface(), _unreadable_truth),
        ]
        for case_index, (surface, truth) in enumerate(unreadable):
            root = tmp_path / f"read-{case_index}"
            root.mkdir()
            walk = _walk(root, bridgectl=surface, truth=truth, engine=_Engine([self.SENT]))
            assert walk.address_if_held() == ""
            walk.drain_boot_notice(0)
            line = self._boot_notice(walk)
            assert line["bound"] is False
            assert line["drained"] == self.SENT

    def test_a_lane_with_no_boot_prompt_takes_no_boot_mark(self, tmp_path: Path) -> None:
        assert _walk(tmp_path, lane=CLAUDE_LANE).settle_boot_turn() is None

    def test_a_boot_turn_that_ended_is_journalled_with_its_seconds(self, tmp_path: Path) -> None:
        walk = _walk(tmp_path, boot_turn_over=lambda: True)
        assert walk.settle_boot_turn() == 0
        (line,) = [one for one in walk.journal.read() if one["event"] == "boot.turn"]
        assert line["seconds"] >= 0


class TestALaneThatCouldNotBeArranged:
    """The #352 ruling: past preflight, a lane that will not start is a `roster` FAIL.

    REFUSED is preflight's word and preflight's alone (§5) — it means no engine
    was ever started and nothing was observed. A lane whose engine or Session
    dies *after* preflight passed has observed something: the Session never
    reached the product's roster. So the row is FAIL, the rows behind it are
    SKIPPED naming it, and the run exits non-zero on them.
    """

    def _verdict(self, tmp_path: Path, selection: items.Selection) -> support.Verdict:
        return support.Verdict(
            run_id="20260910T000000Z",
            selection=selection,
            lanes=items.LANES,
            journal=_journal(tmp_path),
        )

    def test_the_lane_fails_roster_and_skips_what_stood_on_it(self, tmp_path: Path) -> None:
        verdict = self._verdict(tmp_path, items.select())
        journey.unarranged(
            journey.lane(CLAUDE_LANE),
            selection=verdict.selection,
            journal=verdict.journal,
            verdict=verdict,
            why="the lane ended in EngineRefused: the engine did not bind its socket",
        )
        rows = {row["item"]: row for row in verdict.document()["lanes"][CLAUDE_LANE]}
        assert rows[str(items.Item.ROSTER)]["verdict"] == "FAIL"
        assert (
            "EngineRefused"
            in support.resolve(tmp_path, rows[str(items.Item.ROSTER)]["evidence"])["why"]
        )
        assert [row["verdict"] for row in rows.values()] == ["FAIL"] + ["SKIPPED"] * 4
        assert verdict.result is support.FAIL
        assert not [owed for owed in verdict.missing if owed.startswith(CLAUDE_LANE)], (
            "a lane that could not be arranged still writes every row it promised — a lane "
            "with no rows at all is one the verdict cannot tell from a lane nobody asked for"
        )

    def test_it_never_writes_a_second_row_over_one_the_lane_had_already_read(
        self, tmp_path: Path
    ) -> None:
        """A lane can also die *after* `roster` passed, and that row stands."""
        verdict = self._verdict(tmp_path, items.select())
        reference = verdict.journal("roster.row", lane=CLAUDE_LANE)
        verdict.record(items.Item.ROSTER, support.PASS, reference, lane=CLAUDE_LANE)
        journey.unarranged(
            journey.lane(CLAUDE_LANE),
            selection=verdict.selection,
            journal=verdict.journal,
            verdict=verdict,
            why="the lane ended in SessionRefused: the pty went away",
        )
        rows = {row["item"]: row for row in verdict.document()["lanes"][CLAUDE_LANE]}
        assert rows[str(items.Item.ROSTER)]["verdict"] == "PASS"
        assert rows[str(items.Item.RELAY)]["verdict"] == "SKIPPED"


class TestWhatTheCodexLaneMayNotDo:
    """§4.1 and §4.3 — the lane borrows the operator's Codex, and changes nothing of it."""

    def test_the_harness_sets_no_codex_home(self) -> None:
        """ADR 0022 derives the shared app-server's socket from one `CODEX_HOME`.

        A lane with its own would find no server at its socket and its TUI would
        not join the shared daemon — the exact `roster` failure of #232. So the
        Codex lane adds nothing to the environment, and the operator's own value
        (if they have one) travels unchanged.
        """
        assert journey.lane(CODEX_LANE).own_config_directory is False
        bare = hand_started.terminal_environment("/usr/bin", base={"HOME": "/Users/simon"})
        assert codex_runtime.CODEX_HOME_VARIABLE not in bare
        theirs = hand_started.terminal_environment(
            "/usr/bin",
            base={"HOME": "/Users/simon", codex_runtime.CODEX_HOME_VARIABLE: "/elsewhere/.codex"},
        )
        assert theirs[codex_runtime.CODEX_HOME_VARIABLE] == "/elsewhere/.codex"

    def test_no_lane_installs_the_shared_app_servers_job_or_reconciles_it(self) -> None:
        """§4.3: one launchd job per user, and it is the operator's — not a run's."""
        named = [
            f"{source.name}:{number}"
            for source in sorted(ACCEPTANCE.glob("*.py"))
            for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
            if "codex_launch_agent" in line
        ]
        assert not named, (
            "the lanes join the operator's shared app-server and never install or reconcile "
            "one: " + "; ".join(named)
        )


def test_a_lane_name_is_the_agent_kind_the_engine_keys_its_tables_by() -> None:
    """The one thing that makes `[adapters.agents] <lane>` a table the engine reads.

    The harness derives `[adapters.agents]`, `[adapters.settings."agent.<kind>"]`
    and every roster address from the lane's name, so a lane named anything the
    product does not call an agent kind is a config the engine refuses outright
    and a row nothing matches.
    """
    assert set(items.LANES) == {str(kind) for kind in AgentKind}
    assert {one.agent for one in journey.LANES} == set(items.LANES)


class TestWhereALanesSocketsLive:
    """§4.2 items 1 and 6 — one directory per lane per run, and nothing shared.

    Not under the run directory, and that is the platform rather than a
    preference: Darwin caps an `AF_UNIX` path at 103 bytes
    (`src/gpt_voicecoding/config.py:37`) and the control socket would be 111
    there, the engine's own app-server socket 140. The product keeps
    `RUNTIME_ROOT` at `/tmp` for the same reason, and §5's writable-root refusal
    is about the run root — the workspace an approval is raised over — not about
    a socket. What the separation asks for holds either way: a directory per
    lane per run, made 0700 and removed when the lane is done with it.
    """

    def test_two_lanes_of_two_runs_are_four_directories(self) -> None:
        composed = {
            support.socket_directory(run, lane, uid=501)
            for run in ("20260910T000000Z", "20260910T010000Z")
            for lane in items.LANES
        }
        assert len(composed) == 4
        assert support.socket_directory("20260910T000000Z", CODEX_LANE, uid=501) == Path(
            f"/tmp/gvc-acceptance-501-20260910T000000Z-{CODEX_LANE}"
        )

    def test_it_is_the_owners_alone_and_goes_when_the_lane_is_done(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        with support.lane_sockets("20260910T000000Z", CODEX_LANE, journal) as sockets:
            assert sockets.is_dir()
            assert stat.S_IMODE(sockets.stat().st_mode) == 0o700
            (sockets / "control.sock").touch()
            held = sockets
        assert not held.exists()
        made, removed = (one for one in journal.read() if one["event"].startswith("sockets."))
        assert made["directory"] == str(held)
        assert removed["directory"] == str(held)

    def test_the_run_directory_says_where_they_went(self, tmp_path: Path) -> None:
        """The one thing the run directory cannot show a reader: a path outside it."""
        journal = _journal(tmp_path)
        with support.lane_sockets("20260910T000000Z", CLAUDE_LANE, journal):
            pass
        (made,) = [one for one in journal.read() if one["event"] == "sockets.made"]
        assert made["lane"] == CLAUDE_LANE
        assert "the run directory cannot hold" in made["why"]

    def test_a_lane_that_left_nothing_behind_still_leaves_no_directory(
        self, tmp_path: Path
    ) -> None:
        with support.lane_sockets("20260910T000000Z", CLAUDE_LANE, _journal(tmp_path)) as sockets:
            held = sockets
        assert not held.exists()


class TestAReconcileThatCouldNotRemoveTheRow:
    """§4.1 — the removal is verified, because a false "reconciled" is worse than a red.

    Rows are *found* by parsing and *removed* by matching header lines, and TOML
    has more than one legal spelling for a header. A row this harness cannot
    match must not be reported as removed: a run that walked past it would grade
    a Session in a workspace somebody else's row had already trusted.
    """

    def _codex_home(self, tmp_path: Path, body: str) -> Path:
        codex_home = tmp_path / "dot-codex"
        codex_home.mkdir()
        (codex_home / support.CONFIG_NAME).write_text(body)
        return codex_home

    def _stale(self, tmp_path: Path) -> Path:
        stale = tmp_path / "acceptance" / "20260901T000000Z" / "workspace-codex"
        stale.mkdir(parents=True)
        return stale

    def test_a_row_written_with_the_other_quotes_is_still_removed(self, tmp_path: Path) -> None:
        stale = self._stale(tmp_path)
        codex_home = self._codex_home(
            tmp_path, f"[projects.'{stale}']  # left by a killed run\ntrust_level = 'trusted'\n"
        )
        removed = support.reconcile_codex_trust(
            tmp_path / "acceptance",
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)},
        )
        assert removed == (str(stale),)
        assert "projects" not in tomllib.loads((codex_home / support.CONFIG_NAME).read_text())

    def test_a_row_it_cannot_remove_is_said_out_loud_rather_than_reported_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure this verification exists for, forced by taking the matcher away."""
        stale = self._stale(tmp_path)
        codex_home = self._codex_home(tmp_path, f'[projects."{stale}"]\ntrust_level = "trusted"\n')
        monkeypatch.setattr(support, "_project_of", lambda _: None)
        with pytest.raises(support.CouldNotReconcile) as unremoved:
            support.reconcile_codex_trust(
                tmp_path / "acceptance",
                environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)},
            )
        assert str(stale) in str(unremoved.value)
        assert support.BACKUP_SUFFIX in str(unremoved.value)

    def test_a_verified_rewrite_leaves_nothing_of_its_own_behind(self, tmp_path: Path) -> None:
        """§4: nothing a lane writes lands in the operator's own agent configuration.

        The backup is taken because the rewrite is of *their* file; once the
        rewrite is verified it is the run's own litter, and a run that
        reconciles leaves one per run forever. What was removed is in the
        journal, which is where a run's record belongs.
        """
        stale = self._stale(tmp_path)
        codex_home = self._codex_home(
            tmp_path, f'model = "gpt-5.6-luna"\n\n[projects."{stale}"]\ntrust_level = "trusted"\n'
        )
        support.reconcile_codex_trust(
            tmp_path / "acceptance",
            environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)},
            taken_by="20260911T000000Z",
        )
        assert sorted(one.name for one in codex_home.iterdir()) == [support.CONFIG_NAME]

    def test_the_backup_a_failed_rewrite_leaves_is_named_for_the_run_that_took_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = self._stale(tmp_path)
        codex_home = self._codex_home(tmp_path, f'[projects."{stale}"]\ntrust_level = "trusted"\n')
        monkeypatch.setattr(support, "_project_of", lambda _: None)
        with pytest.raises(support.CouldNotReconcile):
            support.reconcile_codex_trust(
                tmp_path / "acceptance",
                environment={codex_runtime.CODEX_HOME_VARIABLE: str(codex_home)},
                taken_by="20260911T000000Z",
            )
        (backup,) = sorted(codex_home.glob(f"{support.CONFIG_NAME}.*"))
        assert "20260911T000000Z" in backup.name
