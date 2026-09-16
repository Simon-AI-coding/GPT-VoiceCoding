"""The release workflow's one decision: whether a commit on `main` gets a Release.

`.github/workflows/release.yml` runs `scripts/cut_release.py` after CI passes on
a push to `main`. The version is whatever `pyproject.toml` writes at that commit;
a Release is cut the first time a version is seen and never again.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cut_release.py"
_spec = importlib.util.spec_from_file_location("cut_release", SCRIPT)
cut_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut_release)


def test_a_new_version_is_released():
    assert cut_release.decide("1.0.0", tag_exists=False, release_exists=False) is (
        cut_release.Decision.CREATE
    )


def test_a_version_already_released_is_left_alone():
    assert cut_release.decide("1.0.0", tag_exists=True, release_exists=True) is (
        cut_release.Decision.ALREADY_RELEASED
    )


def test_a_tag_whose_release_was_deleted_is_reported_not_recreated():
    assert cut_release.decide("1.0.0", tag_exists=True, release_exists=False) is (
        cut_release.Decision.TAG_WITHOUT_RELEASE
    )


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0rc1", "1.0.0.dev3", "", "1.0.0 "])
def test_a_version_that_is_not_plain_major_minor_patch_stops_the_run(version):
    with pytest.raises(cut_release.ReleaseError, match="version"):
        cut_release.decide(version, tag_exists=False, release_exists=False)


def test_an_unreleasable_version_stops_before_asking_git_or_github(tmp_path):
    shell = Recorder(tag_exists=False, release_exists=False)

    with pytest.raises(cut_release.ReleaseError, match="version"):
        cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0"), run=shell)
    assert shell.calls == []


def test_the_tag_is_the_version_with_a_v():
    assert cut_release.tag_for("1.2.3") == "v1.2.3"


class Recorder:
    """Stands in for `git` and `gh`, answering from a fixed state."""

    def __init__(self, *, tag_exists: bool, release_exists: bool):
        self.tag_exists = tag_exists
        self.release_exists = release_exists
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            out = "abc\trefs/tags/v1.0.0\n" if self.tag_exists else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[:3] == ["gh", "release", "view"]:
            if self.release_exists:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="release not found")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def pyproject(root: Path, version: str) -> Path:
    written = root / "pyproject.toml"
    written.write_text(f'[project]\nname = "gpt-voicecoding"\nversion = "{version}"\n')
    return written


def test_a_run_for_a_new_version_creates_the_release_at_that_commit(tmp_path, capsys):
    shell = Recorder(tag_exists=False, release_exists=False)

    code = cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=shell)

    assert code == 0
    assert shell.calls[-1] == [
        "gh",
        "release",
        "create",
        "v1.0.0",
        "--target",
        "deadbeef",
        "--title",
        "v1.0.0",
        "--generate-notes",
    ]
    assert "v1.0.0" in capsys.readouterr().out


def test_a_run_for_a_released_version_creates_nothing(tmp_path):
    shell = Recorder(tag_exists=True, release_exists=True)

    code = cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=shell)

    assert code == 0
    assert not any(call[:3] == ["gh", "release", "create"] for call in shell.calls)


def test_a_run_that_finds_an_orphan_tag_warns_and_creates_nothing(tmp_path, capsys):
    shell = Recorder(tag_exists=True, release_exists=False)

    code = cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=shell)

    assert code == 0
    assert not any(call[:3] == ["gh", "release", "create"] for call in shell.calls)
    assert "::warning::" in capsys.readouterr().out


def test_a_failed_create_fails_the_run(tmp_path):
    def failing(argv):
        if argv[:3] == ["gh", "release", "create"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 403")
        return Recorder(tag_exists=False, release_exists=False)(argv)

    with pytest.raises(cut_release.ReleaseError, match="HTTP 403"):
        cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=failing)


def test_a_release_lookup_that_fails_for_another_reason_fails_the_run(tmp_path):
    def offline(argv):
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="connection refused")
        return Recorder(tag_exists=True, release_exists=False)(argv)

    with pytest.raises(cut_release.ReleaseError, match="connection refused"):
        cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=offline)


def test_a_tag_lookup_that_fails_fails_the_run(tmp_path):
    def offline(argv):
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="could not read")

    with pytest.raises(cut_release.ReleaseError, match="could not read"):
        cut_release.main(sha="deadbeef", pyproject=pyproject(tmp_path, "1.0.0"), run=offline)


def test_the_repository_version_is_releasable():
    written = Path(__file__).resolve().parents[1] / "pyproject.toml"

    cut_release.decide(cut_release.written_version(written), tag_exists=False, release_exists=False)
