"""Every bundled Python console script, made relative to its interpreter.

`pip` writes console scripts whose launcher contains the **absolute** path of the
interpreter that installed them. It is a one-line shebang when the path is short
and has no spaces, or a three-line shell trampoline otherwise. The interpreter
itself is relocatable, but its scripts are not, so every script installed into
the bundle has to move with it.

The rewrite is deliberately selected by the interpreter path rather than by a
list of script names. Shell scripts already supplied by python-build-standalone
are left alone, as are binaries and symlinks.
"""

from __future__ import annotations

from pathlib import Path

#: A shell executes the second line; Python reads lines two and three as a
#: string literal. In both languages the remaining file is still the original
#: console script, while `realpath` finds the interpreter beside the target even
#: when the script was invoked through a symlink elsewhere.
PREAMBLE = """\
#!/bin/sh
'''exec' "$(dirname -- "$(realpath -- "$0")")/python3" "$0" "$@"
' '''
"""


def _installed_preambles(directory: Path) -> tuple[bytes, bytes]:
    """The two launchers pip can write for this interpreter on POSIX."""
    interpreter = str(directory / "python3").encode()
    executable = b'"' + interpreter + b'"' if b" " in interpreter else interpreter
    direct = b"#!" + interpreter + b"\n"
    safe = b"#!/bin/sh\n" + b"'''exec' " + executable + b' "$0" "$@"\n' + b"' '''\n"
    return direct, safe


def relocate_all(directory: Path) -> None:
    """Make every pip-installed Python script in ``directory`` relocatable."""
    installed_preambles = _installed_preambles(directory)
    replacement = PREAMBLE.encode()
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode & 0o111:
            continue
        contents = path.read_bytes()
        for installed in installed_preambles:
            if contents.startswith(installed):
                path.write_bytes(replacement + contents.removeprefix(installed))
                break
