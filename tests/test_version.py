"""The product has one version, and `pyproject.toml` is where it is written.

The engine reports it on the control plane and to codex app-server, the app
build stamps it into `Info.plist`, and the release workflow tags it. None of
them carries a copy.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import gpt_voicecoding
from gpt_voicecoding import _version

REPO_ROOT = Path(__file__).resolve().parents[1]


def written_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def pyproject(root: Path, *, name: str, version: str) -> None:
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')


def test_the_engine_reports_the_version_pyproject_writes():
    assert gpt_voicecoding.__version__ == written_version()


def test_a_source_checkout_reads_its_own_pyproject_not_stale_install_metadata(tmp_path):
    pyproject(tmp_path, name=_version.DISTRIBUTION, version="7.8.9")

    def stale(_name):
        return "0.0.0"

    assert _version.read(checkout=tmp_path, installed=stale) == "7.8.9"


def test_an_installed_tree_without_a_checkout_reads_the_package_metadata(tmp_path):
    def installed(name):
        assert name == _version.DISTRIBUTION
        return "7.8.9"

    assert _version.read(checkout=tmp_path, installed=installed) == "7.8.9"


def test_another_projects_pyproject_is_not_mistaken_for_this_one(tmp_path):
    pyproject(tmp_path, name="something-else", version="4.4.4")

    assert _version.read(checkout=tmp_path, installed=lambda _name: "7.8.9") == "7.8.9"
