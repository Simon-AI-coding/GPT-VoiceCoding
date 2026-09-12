"""The desktop reads configuration through its existing Python owner."""

from pathlib import Path

import pytest
from tests.test_config import COMPLETE, written

from gpt_voicecoding import shell_configuration
from gpt_voicecoding.adapters.call.realtime.settings import DEFAULT_REALTIME_MODEL
from gpt_voicecoding.adapters.companion_channel.null import TELEGRAM_REFERENCE
from gpt_voicecoding.config import ConfigError, default_log_path


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
