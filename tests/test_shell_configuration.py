"""The desktop reads configuration through its existing Python owner."""

from pathlib import Path

import pytest
from tests.test_config import COMPLETE, written

from gpt_voicecoding import shell_configuration
from gpt_voicecoding.adapters.call.realtime.settings import DEFAULT_REALTIME_MODEL
from gpt_voicecoding.adapters.companion_channel.null import TELEGRAM_REFERENCE
from gpt_voicecoding.config import ConfigError, default_log_path, load


def test_voice_settings_use_the_adapter_values_and_its_realtime_model_list(tmp_path: Path) -> None:
    path = written(tmp_path, COMPLETE + '\n[adapters.settings.call]\nvoice = "maple"\n')
    reading = shell_configuration.read(path)
    assert reading["voice"] == "maple"
    assert reading["voices"] == [
        "juniper",
        "maple",
        "spruce",
        "ember",
        "vale",
        "breeze",
        "arbor",
        "sol",
        "cove",
    ]
    assert reading["realtime_models"] == [DEFAULT_REALTIME_MODEL]


def test_call_durations_are_the_same_values_the_engine_reads(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        COMPLETE
        + "\n[policy]\nsilence_end_seconds = 12\ncool_down_seconds = 34\n"
        + "speech_settle_seconds = 5.5\n",
    )
    reading = shell_configuration.read(path)
    assert [
        reading[key]
        for key in ("silence_end_seconds", "cool_down_seconds", "speech_settle_seconds")
    ] == [12, 34, 5.5]


def test_telegram_name_is_shell_metadata_not_an_adapter_setting_or_a_secret(tmp_path: Path) -> None:
    content = COMPLETE.replace("tests.fakes:FakeCompanionChannel", TELEGRAM_REFERENCE)
    path = written(
        tmp_path,
        content
        + '\n[adapters.settings.companion_channel]\ntoken_env = "MY_TOKEN"\nchat_id = "42"\n'
        + '[shell.telegram]\nbot_name = "My bot"\n',
    )
    reading = shell_configuration.read(path)
    assert reading["telegram_name"] == "My bot"
    assert reading["token_env"] == "MY_TOKEN"
    assert reading["chat_id"] == "42"
    assert "token" not in reading


def test_an_unchosen_model_is_readable_but_cannot_start_the_engine(tmp_path: Path) -> None:
    path = written(tmp_path, COMPLETE.replace('model = "a-model-the-user-chose"', ""))
    before = path.read_bytes()
    assert shell_configuration.read(path)["model"] is None
    with pytest.raises(ConfigError, match="no delegated-turn model"):
        load(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "invalid",
    [
        'model = ""',
        "model = 12",
        'effort = ""',
        "[policy]\nsilence_end_seconds = -1",
    ],
)
def test_allowing_an_unchosen_model_does_not_hide_other_invalid_settings(
    tmp_path: Path, invalid: str
) -> None:
    content = COMPLETE.replace('model = "a-model-the-user-chose"', invalid)
    path = written(tmp_path, content)
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        shell_configuration.read(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("chat", ['"42"', "42", '""'])
def test_only_a_selected_telegram_with_a_chat_is_bound(tmp_path: Path, chat: str) -> None:
    content = COMPLETE.replace("tests.fakes:FakeCompanionChannel", TELEGRAM_REFERENCE)
    path = written(
        tmp_path, content + f"\n[adapters.settings.companion_channel]\nchat_id = {chat}\n"
    )
    before = path.read_bytes()
    result = shell_configuration.read(path)
    assert result["telegram_bound"] is (chat != '""')
    assert result["model"] == "a-model-the-user-chose"
    assert result["effort"] is None
    assert result["realtime_model"] == DEFAULT_REALTIME_MODEL
    assert result["log_path"] == str(default_log_path())
    assert path.read_bytes() == before


def test_unselected_telegram_does_not_become_bound_and_configured_values_are_carried(
    tmp_path: Path,
) -> None:
    content = COMPLETE.replace("[delegate]", '[delegate]\neffort = "high"')
    content = content.replace("[log]", '[log]\npath = "/tmp/chosen-engine.log"')
    path = written(
        tmp_path,
        content
        + '\n[adapters.settings.call]\nrealtime_model = "chosen-realtime"\n'
        + '[adapters.settings.companion_channel]\nchat_id = "42"\n',
    )
    result = shell_configuration.read(path)
    assert not result["telegram_bound"]
    assert result["effort"] == "high"
    assert result["realtime_model"] == "chosen-realtime"
    assert result["log_path"] == "/tmp/chosen-engine.log"


def test_broken_toml_is_refused_without_touching_the_file(tmp_path: Path) -> None:
    path = written(tmp_path, COMPLETE + "\n[unfinished")
    before = path.read_bytes()
    with pytest.raises(ConfigError, match="not readable as TOML"):
        shell_configuration.read(path)
    assert path.read_bytes() == before
