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

from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings, SettingsError
from gpt_voicecoding.adapters.companion_channel.null import TELEGRAM_REFERENCE
from gpt_voicecoding.config import ConfigError, load


def read(path: Path) -> dict[str, Any]:
    """Only the facts the shell displays, from the same file the engine loads."""
    config = load(path)
    call = RealtimeCallSettings.of(config.adapters.settings_for("call"))
    channel = config.adapters.settings_for("companion_channel") or {}
    return {
        "model": config.delegated_turn_model,
        "effort": config.delegated_turn_effort,
        "realtime_model": call.realtime_model,
        "log_path": str(config.log.path),
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
