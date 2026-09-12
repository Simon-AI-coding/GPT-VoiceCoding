"""A read-only configuration projection for the desktop, without a running engine.

The existing loaders own parsing, validation and defaults. This composition only
selects display fields; it constructs no adapter and reads no credential value.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.call.realtime.settings import (
    DEFAULT_REALTIME_MODEL,
    RealtimeCallSettings,
    SettingsError,
)
from gpt_voicecoding.adapters.companion_channel.null import TELEGRAM_REFERENCE
from gpt_voicecoding.config import ConfigError, of, read_document

# The product's fixed picker, not an additional adapter validation rule (#361).
VOICES = ("juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove")


def read(path: Path) -> dict[str, Any]:
    """Only the facts the shell displays, from the same file the engine loads."""
    document = read_document(path)
    config = of(document, source=path, allow_unchosen_model=True)
    call = RealtimeCallSettings.of(config.adapters.settings_for("call"))
    channel = config.adapters.settings_for("companion_channel") or {}
    return {
        "model": config.delegated_turn_model or None,
        "effort": config.delegated_turn_effort,
        "voice": call.voice,
        "voices": list(VOICES),
        "realtime_models": [DEFAULT_REALTIME_MODEL],
        "silence_end_seconds": config.policy.silence_end_seconds,
        "cool_down_seconds": config.policy.cool_down_seconds,
        "speech_settle_seconds": config.policy.speech_settle_seconds,
        "realtime_model": call.realtime_model,
        "log_path": str(config.log.path),
        "telegram_name": document.get("shell", {}).get("telegram", {}).get("bot_name"),
        "token_env": channel.get("token_env"),
        "chat_id": str(channel["chat_id"]) if "chat_id" in channel else None,
        "telegram_bound": config.adapters.companion_channel == TELEGRAM_REFERENCE
        and bool(str(channel.get("chat_id", "")).strip()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        document = read(args.config)
    except (ConfigError, SettingsError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
