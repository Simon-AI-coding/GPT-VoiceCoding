"""Where the engine learns its own version.

`pyproject.toml` is the one place the version is written. A source checkout
reads that file directly, because an editable install's metadata keeps whatever
version was current at its last `pip install -e` and would go stale after a
bump. An installed tree — the app bundle — has no `pyproject.toml` beside the
package, and reads the metadata pip wrote from that same file at build time.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

#: The distribution name in `pyproject.toml`, and the one the metadata is under.
DISTRIBUTION = "gpt-voicecoding"

#: Where `pyproject.toml` sits when this package runs from a checkout:
#: `src/gpt_voicecoding/` is two levels below the repository root.
CHECKOUT = Path(__file__).resolve().parents[2]


def read(
    *,
    checkout: Path = CHECKOUT,
    installed: Callable[[str], str] = metadata.version,
) -> str:
    """This product's version: the checkout's `pyproject.toml`, else the install's metadata."""
    candidate = checkout / "pyproject.toml"
    if candidate.is_file():
        with candidate.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
        if project.get("name") == DISTRIBUTION:
            return project["version"]
    return installed(DISTRIBUTION)
