"""Exercise the shipping upgrade check without building or installing an app."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


def upgrade_check():
    source = Path("scripts/install.sh").read_text()
    check = source.split('installed_app="/Applications/$bundle_name.app"\n', 1)[1]
    check = check.split("# Move the old bundle aside", 1)[0]
    return 'set -eu\nfail() { printf "%s\\n" "$1" >&2; exit 1; }\nbundle_id="$1"\n' + check


@pytest.mark.skipif(sys.platform != "darwin", reason="The installer uses macOS AppKit")
def test_first_install_has_no_old_application_error():
    result = subprocess.run(
        ["/bin/sh", "-c", upgrade_check(), "check", f"test.missing.{uuid.uuid4().hex}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("queries", "quit_status", "expected_status", "expected_quits", "message"),
    [
        (["false"], 0, 0, 0, ""),
        (["true", "true", "false"], 0, 0, 1, ""),
        (["error"], 0, 1, 0, "Could not check"),
        (["true", "error"], 0, 1, 1, "Could not check"),
        (["true"], 1, 1, 1, "Quit GPT-VoiceCoding"),
        (["true"] * 31, 0, 1, 1, "Quit GPT-VoiceCoding"),
    ],
)
def test_upgrade_waits_for_quit_and_keeps_real_failures_visible(
    tmp_path, queries, quit_status, expected_status, expected_quits, message
):
    calls = tmp_path / "calls"
    interpreter = tmp_path / "osascript"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        f"calls = Path({str(calls)!r})\n"
        "previous = calls.read_text().splitlines() if calls.exists() else []\n"
        "quitting = 'to quit' in ' '.join(sys.argv)\n"
        "with calls.open('a') as stream:\n"
        "    stream.write('quit\\n' if quitting else 'query\\n')\n"
        f"if quitting: sys.exit({quit_status})\n"
        f"answer = {queries!r}[previous.count('query')]\n"
        "if answer == 'error': sys.exit(1)\n"
        "print(answer)\n"
    )
    interpreter.chmod(0o755)
    sleeper = tmp_path / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n")
    sleeper.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", upgrade_check(), "check", "test.fixture.app"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected_status, result.stderr
    assert calls.read_text().splitlines().count("quit") == expected_quits
    assert message in result.stderr
    if expected_status == 0:
        assert result.stderr == ""
