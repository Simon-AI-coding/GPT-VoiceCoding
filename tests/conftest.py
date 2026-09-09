"""What no test in this suite is allowed to reach.

**The real `launchctl`.** The Codex installation item loads a login job into
launchd, and a job loaded from a test is loaded into the launchd of the person
running the test — a real login session, a real `~/Library/LaunchAgents`, and a
real Codex daemon started under it. That is not a thought experiment: two drafts
of `installation/codex_launch_agent.py` did exactly that while it was being
written, once naming a plist pytest deleted a second later.

Passing a fake in is the design (`Launchd` has no default and every entry point
demands one), but a design only holds while everyone remembers it, and a test
that forgets does not fail — it silently changes the machine and passes. So the
real runner is taken away from the whole suite here, and a test that wants a
subprocess has to say so by supplying its own.

**The real shared Codex app-server.** `shared_daemon.locate` finds the socket
the machine's server is listening on, and #77 put that lookup on the path every
Relay and every Approval now takes. A test that reached it would not merely
read: it would attach to the server holding the Sessions the person running the
tests has open, and a Relay is a `turn/start`. Injecting a `locate`, or naming a
`control_socket` of one's own, is the design; refusing the machine's real one is
what makes forgetting it fail loudly instead of quietly starting a turn in
somebody's work.

**The hazard got sharper with #272, which is why this guard changed shape.**
Until then the lookup was a subprocess — `codex app-server daemon version` — and
taking the runner away could not be forgotten around. Now it is a `stat` of
`$CODEX_HOME/app-server-control/app-server-control.sock`, which no fixture can
intercept by refusing a subprocess.

So what is refused is the **one combination that can reach the machine**: a
`SharedDaemon` built with neither a `control_socket` of its own nor a `locate`
of its own. Either alone is enough to reach nobody — a path under `tmp_path` is
a path under `tmp_path`, and an injected `locate` never looks at the one it was
handed — and refusing more than that would have the suite stubbing the very
thing under test. Refusing less is what let a forgotten construction dial the
developer's own live server, which is the accident this exists for.

**The real Claude Session registry.** #77 put this engine on Claude Code's own
cross-session wire, and being answerable there means publishing a peer key into
`~/.claude/sessions` — the registry holding the live Sessions of whoever is
running the tests. A test that bound a reply inbox with default settings would
publish that key, and a key in that directory is this process announcing itself
as a peer of their open work. Passing a `registry_directory` is the design;
refusing the real one is what makes forgetting it fail loudly.

**The machine's published engine address.** One file per user per machine says
where this machine's engine parks permission dialogs (ADR 0019), and both Claude
hook routes read it with no configuration to go on: with neither a variable nor
a `base_dir`, `bootstrap.approval_socket_path_in` falls through to it. So a test
that arranged nothing was asserting against whichever engine the developer had
up — #229 caught two, which went red and sent a fake Session's registration to a
real engine whenever an acceptance run was publishing. Passing a `base_dir` is
the design, and a test that names one still gets the directory it named; what is
taken away is the machine-wide answer, so forgetting reaches a `tmp_path`
instead of the developer's own route.

**This file holds fixtures and nothing else.** The fake and the helpers it needs
live in `launchd_fake.py`, because a test module that imports a `conftest` by
name is importing whichever `conftest` reached `sys.path` first — and with the
`[acceptance]` extra installed that is `tests/acceptance/conftest.py`, not this
one ([#93](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/93)).
`tests/test_layout.py` holds the rule so it cannot come back.
"""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from gpt_voicecoding import locations
from gpt_voicecoding.adapters.agent.claude import bootstrap
from gpt_voicecoding.adapters.agent.claude.inbox import ReplyInbox
from gpt_voicecoding.adapters.agent.claude.registry import default_registry_directory
from gpt_voicecoding.adapters.agent.codex import shared_daemon
from gpt_voicecoding.adapters.agent.codex.shared_daemon import SharedDaemon
from gpt_voicecoding.installation import claude_hooks, codex_launch_agent
from launchd_fake import FakeLaunchd


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every subprocess this module would run, refused in the loudest way there is."""

    def refuse(arguments: Sequence[str]) -> tuple[int, str]:
        raise AssertionError(
            "a test reached the real machine through "
            f"gpt_voicecoding.installation.codex_launch_agent: {list(arguments)}. "
            "Pass a Launchd with a `run` of its own."
        )

    monkeypatch.setattr(codex_launch_agent, "_run", refuse)


@pytest.fixture(autouse=True)
def _no_real_codex_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one lookup that leads to the machine's own Codex Sessions, refused.

    **Scoped to that path rather than to `locate` outright**, and the
    distinction is the hazard rather than a convenience. `locate` is a pure
    question about a path a test may perfectly well name in its own `tmp_path`,
    and refusing it altogether would make every test inject a stub for something
    that reaches nobody. What must never happen is the *machine's* socket: the
    server on the other end of it is holding the Sessions the person running the
    tests has open, and from there a Relay is a `turn/start` in their work.
    """
    forbidden = shared_daemon.default_control_socket()
    real_init = SharedDaemon.__init__

    def guarded(
        self: SharedDaemon,
        *,
        control_socket: Path | None = None,
        locate: object = shared_daemon.locate,
        **rest: object,
    ) -> None:
        # The *resolved* socket, not the absence of one: a caller may legitimately
        # want the default — the composition root does — and a test that moved
        # `CODEX_HOME` under `tmp_path` has already put that default somewhere
        # harmless. What is refused is landing on this machine's own.
        reaches = Path(control_socket or shared_daemon.default_control_socket())
        if reaches == forbidden and locate is shared_daemon.locate:
            raise AssertionError(
                "a test built a SharedDaemon that would dial the machine's own Codex "
                f"app-server at {forbidden}, holding the Sessions whoever is running "
                "these tests has open. Pass a `control_socket` under `tmp_path`, move "
                "`CODEX_HOME` there, or pass a `locate` of its own."
            )
        real_init(self, control_socket=control_socket, locate=locate, **rest)  # type: ignore[arg-type]

    monkeypatch.setattr(SharedDaemon, "__init__", guarded)


@pytest.fixture(autouse=True)
def _codex_absence_is_never_the_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether *this* machine has a codex is not allowed to decide a test.

    `SharedDaemon` adds "and there is no codex on this machine" to its note when
    the socket is missing *and* nothing resolves on the `PATH` (#272). That
    resolution is a `shutil.which` and reaches nobody, so it is not the hazard
    the fixture above guards — but it does read the developer's environment, and
    a note that gains a clause on a machine without codex and loses it on one
    with codex is a test whose output depends on who ran it. So the default
    answers a fixed path, and a test about the absence injects
    `resolve_executable=lambda: None` and says so.
    """
    monkeypatch.setattr(
        shared_daemon, "default_resolve_executable", lambda: Path("/somewhere/bin/codex")
    )


@pytest.fixture(autouse=True)
def _no_real_claude_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test publishes this process as a peer of the real machine's Sessions.

    "Real" is every registry this machine actually keeps Sessions in, and since
    #303 that is two directories rather than one: the registry follows the Claude
    config directory, so a suite run under `CLAUDE_CONFIG_DIR` has its live
    Sessions there, while the home default stays real whether or not the variable
    is set. Both are derived from the product's own resolver, so this guard cannot
    drift from what the adapter would choose.
    """
    real = ReplyInbox._publish_key  # noqa: SLF001
    machine_registries = {
        default_registry_directory(claude_hooks.default_config_directory(environ))
        for environ in (os.environ, {})
    }

    def refuse(self: ReplyInbox) -> None:
        directory = self._key_path.parent  # noqa: SLF001
        if directory in machine_registries:
            raise AssertionError(
                "a test published a peer key into the machine's own Claude registry at "
                f"{directory}. Pass a `registry_directory` in ClaudeSettings."
            )
        real(self)

    monkeypatch.setattr(ReplyInbox, "_publish_key", refuse)


@pytest.fixture(autouse=True)
def published_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the engine says it is, moved off this machine for every test.

    Returned as well as installed, because the tests that pin what a refused or
    unwritable publish leaves behind have to read the file they moved
    (`test_claude_address_claim.py`, `test_claude_unpublished_address.py`), and
    a fixture each of them defined for itself is how #229's two unpinned tests
    went unnoticed beside them.

    **A `base_dir` a test named is still that directory.** Only the machine-wide
    answer moves, so `publish_address(..., base_dir=tmp_path)` and every test
    that checks a path against `locations.address_path(base_dir)` reads exactly
    what it did before; what changes is the answer to the question no test asked.

    Both spellings are taken, and the second is what keeps this from going
    quietly inert: `bootstrap` bound `address_path` at import, so patching the
    module global is what the hook's own lookup goes through today — and
    patching `locations` is what covers a caller that reaches the source
    function instead, including one written after this fixture.
    """
    path = tmp_path / "engine" / "address.json"
    real = locations.address_path

    def moved(base_dir: Path | None = None) -> Path:
        return path if base_dir is None else real(base_dir)

    monkeypatch.setattr(locations, "address_path", moved)
    monkeypatch.setattr(bootstrap, "address_path", moved)
    return path


@pytest.fixture
def mode_at_bind(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Every `AF_UNIX` socket's permission bits in the instant `bind` returned.

    The property `start_private_unix_server` exists for is not "ends up 0600" —
    binding wide and narrowing with a chmod ends up 0600 too, and that is the
    defect, not the fix (#116). The property is "was never anything else", so the
    reading has to happen in the one instant nothing can have followed the bind:
    `bind` is what creates the file, and the mode it is created at is the whole
    question.

    So the seam is `bind` itself, not whatever calls it. Reading at
    `asyncio.start_unix_server` instead would be reading after the helper had
    already bound *and* listened, which is late enough that a helper narrowing
    its own socket in between would go unnoticed — the test would pass and the
    window would be open. Patching here also covers the callers that hand
    `asyncio` a `path` and let it do the binding, with no second branch.

    The umask is opened all the way for the duration, so a socket that took its
    mode from the umask is unmistakable rather than coincidentally right.
    """
    recorded: dict[str, int] = {}
    bind = socket.socket.bind

    def recording(self: socket.socket, address: object) -> None:
        bind(self, address)
        if self.family == socket.AF_UNIX and isinstance(address, str):
            recorded[address] = stat.S_IMODE(os.stat(address).st_mode)

    monkeypatch.setattr(socket.socket, "bind", recording)
    wide_open = os.umask(0o000)
    try:
        yield recorded
    finally:
        os.umask(wide_open)


@pytest.fixture
def launchd() -> FakeLaunchd:
    return FakeLaunchd()


_socket_roots = itertools.count()


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A short private root for sockets that are really bound and really dialled.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so these cannot live under
    pytest's ``tmp_path``. Held here rather than in each module because the limit
    is a fact about the platform, and two copies of it drift.
    """
    root = Path("/tmp") / f"vc-sockets-{os.getpid()}-{next(_socket_roots)}"
    root.mkdir(mode=0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)
