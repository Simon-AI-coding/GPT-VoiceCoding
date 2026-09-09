"""Rebuilding a registration this engine never received, by reading what the machine shows.

`SessionStart` fires **once per Session** (`registration.py`), so a Session that
started before this engine did never registers with it — and a restart is
exactly that for every Session on the machine. The roster keeps listing them,
because `claude agents --json` lists a Session for existing rather than for
having announced itself; what is lost is the two things that command does not
carry, the transcript path and the inbox socket. Without them a Session is listed
and unreadable: `history` refuses, the brief says nothing was read, and the Reply
Window is CLOSED for want of an address (#278).

**Recovery is by recognition, never by construction.** Every field this module
returns was read out of a file some other program wrote:

| Field | Where the machine already shows it |
| --- | --- |
| inbox socket | the Session's own registry record, keyed by pid — `messagingSocketPath` |
| messaging token | the sibling key file, `<pid>.<sha256 of the socket path>.key` |
| transcript path | the transcript under the projects directory **named for the session id** |

**The transcript is found by its own name.** It is never assembled from the
encoded workspace: that flattening replaces `/`, `.` *and* `_` with `-`, a rule
#73 rediscovered the hard way and `transcript.py` records as the reason the
`SessionStart` hook exists at all. Searching for a name the writer chose is a
reading; rebuilding one from a cwd is a guess that looks like a reading.

**Nothing here is persisted** (#74). This module holds no state and writes no
file: it reads three files and returns what they said. A persisted socket path
would be a route that may no longer listen, which is how a `live` row outlived
its process by days (#26).

**Where a reading does not narrow to exactly one answer, it decides nothing and
says why** (ADR 0020). A pid whose record names another session id is another
process; a session id matching two transcripts picks neither. The reason is
returned as a sentence rather than a code, because its two destinations — the
engine log and the brief the user hears — want the same words (#278).

**A refusal is scoped to what it actually costs.** The registry record is the
whole recovery: without it there is no socket, and no statement from the machine
that this pid is this session id, so nothing is composed. A missing *transcript*
costs the transcript alone — the Session keeps the route the record gave it, and
a Session too new to have written a transcript is not thereby declared dead. A
missing *token* costs the token alone: the token is not what earns a receipt
(#71, `inbox.py`), and losing the route over one would trade a whole Session for
a field that changes nothing on the accepted path.

**The key file convention, and the build it was probed against.** Named
`<pid>.<sha256 of the socket path>.key` beside the Session records, holding
`peerToken` — the exact convention `ReplyInbox` writes for this engine's own
reply socket (`inbox.py`, `KEY_SUFFIX`). Written down here rather than assumed,
the way `registry.PROVEN_AGAINST_VERSION` is: measured on 2026-09-09 against
Claude Code `KEY_PROVEN_AGAINST_VERSION` on Simon's machine, where the live
record `95756.json` named `/tmp/cc-socks/95756.sock` and the file
`95756.ade28dd6….key` beside it held `peerToken`, `procStart` and `pidDomain`.
Only `peerToken` is read; the other two are the receiver's business.

**`SessionReport` lives here** rather than beside the hook that first composed
one, because it now has two composition paths and the recovering one cannot
import the adapter that holds the other without a cycle. The type is the shape
both agree on; `recovered` is the field that keeps them tellable apart.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.inbox import KEY_SUFFIX
from gpt_voicecoding.adapters.agent.claude.registry import (
    RegistryError,
    read_record,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

_log = logging.getLogger(__name__)

#: What the transcripts are called inside a Claude config directory. This module
#: owns the word, as `registry.py` owns `sessions`: the config directory it sits
#: in belongs to `installation.claude_hooks`, and no third module spells either
#: (#303).
PROJECTS_DIRECTORY_NAME: Final = "projects"

#: The Claude Code build the key-file convention above was probed against.
#: Documentation, not a gate — there is nothing to gate, because an unreadable
#: key costs the token alone. Written down so the next probe knows its baseline.
KEY_PROVEN_AGAINST_VERSION: Final = "2.1.266"

#: The field the key file carries the sender's token under.
PEER_TOKEN_FIELD: Final = "peerToken"

#: What a transcript file is called.
TRANSCRIPT_SUFFIX: Final = ".jsonl"


def default_projects_directory(config_directory: Path) -> Path:
    """Where the transcripts of one Claude installation are, given its config directory.

    Follows the config directory for `default_registry_directory`'s reason: under
    `CLAUDE_CONFIG_DIR` the transcripts are written beside that directory's
    settings, and a reader still looking under `~/.claude` would find every
    Session's transcript missing (#303).
    """
    return config_directory / PROJECTS_DIRECTORY_NAME


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Where one Session can be reached, as its own hook said or as disk was read.

    Every field but the id is optional, because every one of them can honestly
    be absent: a build that does not export the messaging variables, a Session
    whose first turn has not created a transcript, a payload without a cwd. A
    partial report is worth keeping — the fields that did arrive are still the
    ones nothing else carries.
    """

    session_id: str
    #: The `claude` process this Session runs as. Not optional in practice and
    #: optional in the type: it comes from `CLAUDE_PID`, which every build
    #: measured so far exports, and a report without it cannot be turned into a
    #: `SessionTarget` at all (`seams/identity.py`).
    pid: int | None = None
    workspace: Path | None = None
    transcript_path: Path | None = None
    messaging_socket: Path | None = None
    messaging_token: str | None = None
    #: Whether this report was composed from disk rather than reported by the
    #: Session's own `SessionStart` hook. It matters because a recovered inbox
    #: address is a route no handshake has been had on: the Session never told
    #: this engine anything, so what is held is a reading of somebody else's file.
    #:
    #: **Precedence is one rule, and it is stated here alone.** A hook report
    #: always overwrites — the Session speaking for itself is better than any
    #: reading of it. A recovery never overwrites a report that is already held,
    #: whichever kind it is: it exists to fill a gap, not to compete.
    recovered: bool = False

    @property
    def target(self) -> SessionTarget | None:
        """The exact Session this report is about, when it said enough to say."""
        if self.pid is None:
            return None
        return SessionTarget(agent=AgentKind.CLAUDE, session_id=self.session_id, pid=self.pid)


@dataclass(frozen=True, slots=True)
class Recovery:
    """What one Session's own on-disk records composed into, and what they could not.

    The two fields are one outcome seen from the two sides that read it. `report`
    is what can be held and routed on; `unread_reason` is the sentence the log
    and the brief both say when there is no transcript to read — either because
    the whole recovery was refused, or because the record was fine and the
    transcript was not there.

    `unread_reason` is absent exactly when `report` carries a transcript path,
    which is the only shape in which nothing is missing.
    """

    report: SessionReport | None = None
    unread_reason: str | None = None

    def __post_init__(self) -> None:
        readable = self.report is not None and self.report.transcript_path is not None
        if readable and self.unread_reason is not None:
            raise ValueError("a recovery that found a transcript has no reason to be unread")
        if not readable and not (self.unread_reason or "").strip():
            raise ValueError("a recovery with no transcript must say why it has none")


def recover(
    target: SessionTarget,
    *,
    registry_directory: Path,
    projects_directory: Path,
) -> Recovery:
    """Compose the registration this engine was never told, or say why it cannot.

    The registry record is asked for first because it is the whole recovery: it
    is the machine's own statement that this pid is this session id, and the
    only place the inbox socket appears. Everything after it is scoped to
    itself.
    """
    try:
        record = read_record(registry_directory, target.pid)
    except RegistryError as refused:
        return Recovery(
            unread_reason=(
                f"registered before this engine started, and no registry record for pid "
                f"{target.pid} could be read: {refused}"
            )
        )
    if record.session_id != target.session_id:
        return Recovery(
            unread_reason=(
                f"the registry record for pid {target.pid} names session "
                f"{record.session_id}, not {target.session_id}; that pid is another process now"
            )
        )
    located = locate_transcript(projects_directory, target.session_id)
    return Recovery(
        report=SessionReport(
            session_id=target.session_id,
            pid=target.pid,
            workspace=record.cwd,
            transcript_path=located.path,
            messaging_socket=record.messaging_socket,
            messaging_token=read_peer_token(
                registry_directory, record.pid, record.messaging_socket
            ),
            recovered=True,
        ),
        unread_reason=located.reason,
    )


@dataclass(frozen=True, slots=True)
class LocatedTranscript:
    """The one transcript a session id names, or the reason there is not exactly one."""

    path: Path | None = None
    reason: str | None = None


def locate_transcript(projects_directory: Path, session_id: str) -> LocatedTranscript:
    """The transcript whose **filename** is this session id, if there is exactly one.

    Zero and more than one are both "no path, and here is why". More than one has
    never been observed — 2138 transcripts on the machine of record on 2026-09-09
    yielded no duplicated basename — but a locator that picked one anyway would
    be choosing which conversation the user is shown, which is not a choice a
    reading gets to make.
    """
    try:
        found = sorted(projects_directory.glob(f"*/{session_id}{TRANSCRIPT_SUFFIX}"))
    except OSError as unreadable:
        return LocatedTranscript(
            reason=f"its transcript could not be found by session id: {unreadable}"
        )
    if len(found) == 1:
        return LocatedTranscript(path=found[0])
    if not found:
        return LocatedTranscript(
            reason=(
                f"its transcript could not be found by session id under {projects_directory}; "
                "the Session may not have written one yet"
            )
        )
    return LocatedTranscript(
        reason=(
            f"its transcript could not be found by session id: {len(found)} files under "
            f"{projects_directory} carry that name, and this engine will not pick one"
        )
    )


def peer_key_path(registry_directory: Path, pid: int, socket_path: Path) -> Path:
    """Where the token for one socket is published, under the convention above.

    The name hashes the socket path exactly as the sender wrote it, so a key for
    a *different* socket of the same process is a different file — which is the
    property that makes reading one by name a reading rather than a guess.
    """
    digest = hashlib.sha256(str(socket_path).encode()).hexdigest()
    return registry_directory / f"{pid}.{digest}{KEY_SUFFIX}"


def read_peer_token(registry_directory: Path, pid: int, socket_path: Path | None) -> str | None:
    """This Session's messaging token, or `None` for every way there is not one.

    Absent, unreadable and unparseable are one answer here, and it is not a
    failure: the token is not what earns a receipt (#71 tried it and this
    engine's own, and neither changed anything on the accepted path), so a
    missing one must not cost the whole route.
    """
    if socket_path is None:
        return None
    path = peer_key_path(registry_directory, pid, socket_path)
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as unreadable:
        _log.info("no messaging token for pid %s at %s: %s", pid, path, unreadable)
        return None
    if not isinstance(document, dict):
        return None
    token = document.get(PEER_TOKEN_FIELD)
    return token if isinstance(token, str) and token.strip() else None
