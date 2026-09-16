#!/usr/bin/env python3
"""Cut the GitHub Release for the version a commit on `main` writes, once.

Run by `.github/workflows/release.yml` after CI has passed on a push to `main`,
from a checkout of that commit:

    python3 scripts/cut_release.py <commit-sha>

`pyproject.toml` is the one place the version is written. Releasing is a PR that
changes it; when that PR lands and CI passes, this cuts `v<version>` at that
commit with GitHub's generated notes. Every later push that leaves the version
alone finds the tag and does nothing.

A tag without a Release — someone deleted the Release by hand — is reported and
left alone: recreating it would publish notes nobody asked for, and moving a tag
users may already have installed is worse.
"""

from __future__ import annotations

import enum
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: Plain `MAJOR.MINOR.PATCH`. The installer takes the latest Release, and a
#: pre-release spelling would need rules this workflow does not have yet.
RELEASABLE = re.compile(r"\d+\.\d+\.\d+")

#: What `gh release view` prints when the tag has no Release.
NOT_FOUND = "release not found"

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ReleaseError(Exception):
    """The run cannot decide, or cannot do what it decided. The workflow goes red."""


class Decision(enum.Enum):
    CREATE = "create"
    ALREADY_RELEASED = "already released"
    TAG_WITHOUT_RELEASE = "tag without release"


def tag_for(version: str) -> str:
    return f"v{version}"


def written_version(pyproject: Path = PYPROJECT) -> str:
    with pyproject.open("rb") as handle:
        return tomllib.load(handle).get("project", {}).get("version", "")


def check_releasable(version: str) -> None:
    if not RELEASABLE.fullmatch(version):
        raise ReleaseError(f"pyproject.toml's version {version!r} is not MAJOR.MINOR.PATCH")


def decide(version: str, *, tag_exists: bool, release_exists: bool) -> Decision:
    check_releasable(version)
    if not tag_exists:
        return Decision.CREATE
    if release_exists:
        return Decision.ALREADY_RELEASED
    return Decision.TAG_WITHOUT_RELEASE


def _shell(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _checked(run: Run, argv: list[str]) -> subprocess.CompletedProcess[str]:
    done = run(argv)
    if done.returncode != 0:
        raise ReleaseError(f"`{' '.join(argv)}` failed: {done.stderr.strip()}")
    return done


def _tag_exists(run: Run, tag: str) -> bool:
    listed = _checked(run, ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    return bool(listed.stdout.strip())


def _release_exists(run: Run, tag: str) -> bool:
    argv = ["gh", "release", "view", tag]
    done = run(argv)
    if done.returncode == 0:
        return True
    if NOT_FOUND in done.stderr:
        return False
    raise ReleaseError(f"`{' '.join(argv)}` failed: {done.stderr.strip()}")


def main(*, sha: str, pyproject: Path = PYPROJECT, run: Run = _shell) -> int:
    version = written_version(pyproject)
    check_releasable(version)
    tag = tag_for(version)
    tag_exists = _tag_exists(run, tag)
    release_exists = tag_exists and _release_exists(run, tag)
    decision = decide(version, tag_exists=tag_exists, release_exists=release_exists)
    if decision is Decision.CREATE:
        _checked(
            run,
            ["gh", "release", "create", tag, "--target", sha, "--title", tag, "--generate-notes"],
        )
        print(f"Released {tag} at {sha}.")
    elif decision is Decision.ALREADY_RELEASED:
        print(f"{tag} is already released; nothing to do.")
    else:
        print(f"::warning::Tag {tag} exists but has no Release. Left alone; see this script.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: cut_release.py <commit-sha>")
    try:
        sys.exit(main(sha=sys.argv[1]))
    except ReleaseError as error:
        sys.exit(f"::error::{error}")
