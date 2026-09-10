#!/usr/bin/env python3
"""The Telegram **user**-account client (#351).

**Responsibilities held here** (§9's `telegram_person.py`): one Telethon client
for both lanes, taking a **mark** on a chat, reading what **arrived** since one,
replying to an id, the cross-process session lock of §5, and the one-time `login`
a person runs.

The account is played because a **bot cannot message a bot**: the inbound half of
the Companion Channel can only be produced by a user account, so the harness
drives one over MTProto. The same client is the run's eyes on outbound — what the
bot sent is read back out of the chat by a real client rather than trusted from
the Bot API's own `sendMessage` reply, which proves the API accepted the call and
not that the message reached the far side.

Three rules this module exists to hold:

* **`telethon` is imported here and nowhere else.** It is the `acceptance` extra,
  it is a forbidden import for Bridge Core and the seams
  (`tests/test_architecture.py`), and it must not reach the bundle. Imported
  *inside* the functions that need it, so this module — and the suite that
  collects it — loads on a machine that never installed the extra.
* **Every chat read is after a mark this harness took** (§9). The harness never
  searches the chat and never matches a message by the Session's name: one engine
  bridges every Session on the machine, so a search would grade a stranger. A
  mark plus the count of ids the engine logged removes the search rather than
  filtering it.
* **Nothing is read by the id the product issued**, and that is not a preference:
  in a Telegram **private chat** each account has its own message-id sequence, so
  the Bot API's `message_id` is the id in the *bot's* dialog and this account
  cannot address a message by it (#354, run `20260910T191643Z`: ids 1806 and 306
  sent, the same two messages are 5939 and 5940 here).

**Three chat operations and no more** (§9): `mark`, `arrived` and `reply`. There
is no read-by-id, no send, no search and no waiting — `peer` resolves a username
to the entity the three take, which touches the account's contacts and not the
chat.

Run the one-time authorisation with:

    .venv/bin/python tests/acceptance/telegram_person.py login

and check it later with `… telegram_person.py status`.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import deadlines

# --- where the account's credentials and its authorised session live ---------

#: A *location* rather than a decision, so it has a default and an override — the
#: same shape the engine's own `state_path` uses. Beside the run directories
#: rather than among them: a run directory is named for a UTC timestamp, and this
#: is not one of those.
PERSON_DIRECTORY_VARIABLE = "GPTVOICECODING_ACCEPTANCE_PERSON_DIR"
DEFAULT_PERSON_DIRECTORY = (
    Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance" / "person"
)

#: Telethon appends `.session` to the name it is given, so the name is stated
#: without one and the file on disk carries it.
SESSION_STEM = "person"
CREDENTIALS_FILE = "credentials.json"

#: The one-run-per-machine lock (§5), a sibling of the session rather than a
#: suffix on it: SQLite keeps its own `-journal` and `-wal` beside the database,
#: and a `person.session.lock` would read as one more of those.
LOCK_FILE = f"{SESSION_STEM}.lock"

#: `my.telegram.org` issues these to a Telegram account, once. They are needed at
#: every connect and not only at login, so the login writes them down beside the
#: session; **the environment wins over the file** (§8), for a machine that would
#: rather keep them somewhere else entirely.
API_ID_VARIABLE = "GPTVOICECODING_ACCEPTANCE_TG_API_ID"
API_HASH_VARIABLE = "GPTVOICECODING_ACCEPTANCE_TG_API_HASH"

#: Owner-only, on the directory and on both files it holds. The session file is a
#: bearer credential for a whole Telegram account.
PRIVATE_DIRECTORY = stat.S_IRWXU
PRIVATE_FILE = stat.S_IRUSR | stat.S_IWUSR

#: What a refusal calls a holder whose record it could not read. Not "another
#: acceptance run": an unreadable record is not evidence of what wrote it.
UNKNOWN_HOLDER = "something on this machine"

#: How the two holders name themselves in the lock file. A run and a one-shot
#: `status` take the same lock, and only the writer knows which it is (§8).
ACCEPTANCE_RUN_HOLDER = "another acceptance run"
STATUS_CHECK_HOLDER = "a `telegram_person.py status` check"

#: `flock` says "somebody else has it" with `EWOULDBLOCK`, and `EACCES` is the
#: same answer on the platforms that use it. Every other errno is a different
#: problem and is raised as itself.
CONTENDED_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


def person_directory(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get(PERSON_DIRECTORY_VARIABLE)
    return Path(override).expanduser() if override else DEFAULT_PERSON_DIRECTORY


def session_path(directory: Path | None = None) -> Path:
    return (directory or person_directory()) / f"{SESSION_STEM}.session"


def credentials_path(directory: Path | None = None) -> Path:
    return (directory or person_directory()) / CREDENTIALS_FILE


def session_lock_path(directory: Path | None = None) -> Path:
    """The one-run-per-machine lock, beside the session it guards.

    Beside it rather than in a directory of its own, so
    `GPTVOICECODING_ACCEPTANCE_PERSON_DIR` moves the lock and the session
    together — a lock that stayed behind would refuse a run that was not sharing
    anything, which is the false refusal mirroring the false verdict preflight
    exists to prevent.
    """
    return (directory or person_directory()) / LOCK_FILE


class PersonError(RuntimeError):
    """The person cannot act — no credentials, no session, or no such peer."""


# --- the cross-process session lock (§5) -------------------------------------


@dataclass(frozen=True)
class LockHolder:
    """Whoever holds the session lock, as the refusal is allowed to name them.

    Every field is optional, and `held_by` says what kind of process wrote the
    record rather than letting the reader assume: `status` takes the same lock
    for the length of one question and has no run directory at all, so a refusal
    that called every holder "another acceptance run" would name a run that does
    not exist. §5 requires the refusal to name the holder's **pid and run
    directory**, and a refusal never assumes.
    """

    pid: int | None
    run_directory: str | None
    held_by: str | None = None

    def __str__(self) -> str:
        return (
            f"{self.held_by or UNKNOWN_HOLDER} holds the user-account session: "
            f"pid {self.pid if self.pid is not None else 'unknown'}, "
            f"run directory {self.run_directory or 'unknown'}"
        )


class SessionInUse(PersonError):
    """Something on this machine already holds the user account. Named, never guessed."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__(str(holder))
        self.holder = holder


def _is_alive(pid: int) -> bool:
    """Signal 0: asks the kernel about the process without touching it.

    `PermissionError` is *alive* — a process this user may not signal is still a
    process — and **only `ProcessLookupError` means gone**, which is the whole of
    "a dead holder is a ghost, not a holder" (§5).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _recorded_holder(directory: Path | None) -> LockHolder | None:
    """The record if it names a live process, `None` while it names nobody yet."""
    try:
        recorded = json.loads(session_lock_path(directory).read_text())
        pid = int(recorded["pid"])
        run_directory = recorded["run_directory"]
        held_by = recorded.get("held_by")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _is_alive(pid):
        return None
    return LockHolder(
        pid,
        None if run_directory is None else str(run_directory),
        None if held_by is None else str(held_by),
    )


def read_lock_holder(
    directory: Path | None = None,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LockHolder:
    """Who the lock file says is holding it — or `unknown`, never a guess.

    **The record is written after the lock is taken**, because the lock belongs to
    the file and writing an identity before holding it would clobber the record of
    whoever currently does. Two consequences, both waited out rather than quoted:
    a holder that has the lock but has not written yet leaves an empty file, and a
    holder that has *just* taken it leaves the previous holder's record in place
    for the syscall between `flock` and the truncate. Both windows end the same
    way — a record naming a process that is no longer alive is a ghost.
    """
    try:
        return deadlines.wait(
            "HOLDER_RECORD_SECONDS",
            lambda: _recorded_holder(directory),
            what="the holder's identity record",
            now=now,
            sleep=sleep,
        )
    except deadlines.DeadlineExpired:
        # The one wait in this harness whose expiry is an **answer**: nobody
        # named themselves, so nobody is named. A refusal never assumes.
        return LockHolder(None, None, None)


class PersonSessionLock:
    """One acceptance run per machine, enforced across processes rather than by a person.

    Two runs share what no `--lane` separates: this session file, which is SQLite
    backing exactly one client, and the trust row a run writes into the operator's
    own `config.toml`, guarded by a *thread* lock that means nothing to a second
    pytest process. So the run takes this lock before it opens the session, and
    holds it until the session-scoped fixtures tear down.

    **Advisory, non-blocking, and released by the kernel.** `flock` is held by the
    **open file description**, so a run killed with `SIGKILL` leaves no stale lock
    to clean up by hand — which is the whole reason the holder is a lock file
    rather than a pid file somebody has to sweep. The holder writes its pid and
    run directory into the file *after* acquiring, so the refusal can name what is
    in the way instead of saying only that something is.
    """

    def __init__(
        self,
        *,
        held_by: str,
        directory: Path | None = None,
        run_directory: Path | str | None = None,
    ) -> None:
        self._directory = directory or person_directory()
        self._run_directory = run_directory
        # Stated by the caller rather than inferred here: this class cannot know
        # whether it is being taken for a whole run or for one `status` answer,
        # and the refusal on the other side quotes whichever it is.
        self._held_by = held_by
        self._handle: IO[str] | None = None

    def __enter__(self) -> PersonSessionLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()

    @property
    def path(self) -> Path:
        return session_lock_path(self._directory)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> PersonSessionLock:
        """Take the lock, or raise `SessionInUse` naming who has it. Never waits."""
        self._directory.mkdir(parents=True, exist_ok=True)
        # `a+` rather than `w`: opening for write truncates the holder's record
        # before this process has learned it cannot have the lock.
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            # Only contention becomes a refusal. A permission error, a full disk
            # or a filesystem with no `flock` proves nothing about another run,
            # and reporting one as "another acceptance run holds the session"
            # would be exactly the assumption preflight exists to refuse.
            if error.errno in CONTENDED_ERRNOS:
                raise SessionInUse(read_lock_holder(self._directory)) from None
            raise
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "run_directory": None
                        if self._run_directory is None
                        else str(self._run_directory),
                        "held_by": self._held_by,
                    }
                )
            )
            handle.flush()
            self.path.chmod(PRIVATE_FILE)
        except OSError:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        """Clear the record, then close the handle — closing is what releases it.

        The record goes first and inside the lock, so a clean handover leaves no
        identity behind for the next contender to quote. A run that dies instead
        of releasing leaves its record, and `read_lock_holder` is what disregards
        it. Safe to repeat.
        """
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
        except OSError:
            # An unwritable record is not worth failing a teardown over: the
            # reader already refuses to quote a holder that is no longer alive.
            pass
        self._handle.close()
        self._handle = None


# --- the credentials (§8) ----------------------------------------------------


@dataclass(frozen=True)
class ApiCredentials:
    """The pair `my.telegram.org` issues once, per account. Journalled nowhere."""

    api_id: int
    api_hash: str


def load_credentials(
    directory: Path | None = None, environ: Mapping[str, str] | None = None
) -> ApiCredentials:
    """The account's `api_id`/`api_hash`: **environment first**, then the login's file.

    Nothing here is hard-coded and the environment always wins (§8). The fallback
    file exists because Telethon needs the pair on *every* client construction
    while the pair is issued once, by a human — so the alternative is not "no
    file" but "two variables exported before every run, forever". 0600, in the
    user's own application-support directory, written only by `login`, and never
    in the repository or the journal.

    A partial environment is **not** half an answer: one variable set and the
    other missing falls through to the file rather than pairing a stated `api_id`
    with a stored `api_hash`, which is a combination nobody chose.
    """
    directory = directory or person_directory()
    values = os.environ if environ is None else environ
    stated = (values.get(API_ID_VARIABLE), values.get(API_HASH_VARIABLE))
    if all(stated):
        api_id, api_hash = stated
        return ApiCredentials(int(str(api_id)), str(api_hash))

    path = credentials_path(directory)
    if not path.exists():
        raise PersonError(
            f"no Telegram API credentials: neither {API_ID_VARIABLE}/{API_HASH_VARIABLE} in the "
            f"environment nor {path} on disk. Run `python tests/acceptance/telegram_person.py "
            f"login` once."
        )
    stored = json.loads(path.read_text())
    return ApiCredentials(int(stored["api_id"]), str(stored["api_hash"]))


def store_credentials(credentials: ApiCredentials, directory: Path | None = None) -> Path:
    directory = directory or person_directory()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(PRIVATE_DIRECTORY)
    path = credentials_path(directory)
    path.write_text(json.dumps({"api_id": credentials.api_id, "api_hash": credentials.api_hash}))
    path.chmod(PRIVATE_FILE)
    return path


# --- the client --------------------------------------------------------------


@dataclass(frozen=True)
class PersonMessage:
    """One message in the chat, as a real Telegram client sees it."""

    id: int
    text: str
    outgoing: bool
    date: datetime | None = None

    def as_journal_fields(self) -> dict[str, object]:
        return {
            "message_id": self.id,
            "direction": "sent" if self.outgoing else "received",
            "text": self.text,
            "date": None if self.date is None else self.date.isoformat(),
        }


def as_person_message(message: Any) -> PersonMessage:
    return PersonMessage(
        id=int(message.id),
        text=str(message.message or ""),
        outgoing=bool(message.out),
        date=getattr(message, "date", None),
    )


def newest_id(messages: Iterable[Any]) -> int:
    """A chat's mark: the newest id it holds, and **0 for a chat holding nothing**.

    0 is a real answer rather than a missing one — everything in an empty chat
    arrived after the mark, and a run's first turn against a fresh bot is exactly
    that. Ordinary code, so `tests/test_harness_person.py` pins it without an
    account (§9).
    """
    return max((int(one.id) for one in messages), default=0)


def in_arrival_order(messages: Iterable[Any]) -> tuple[PersonMessage, ...]:
    """What arrived, **oldest first** — the order a split send was sent in (#189).

    Telethon hands back newest-first, and the order matters twice: the count is
    graded against the ids the engine issued, and the *first* of these is the id
    item 5 anchors its reply to (§2 item 5).
    """
    return tuple(sorted((as_person_message(one) for one in messages), key=lambda one: one.id))


def _no_journal(event: str, **fields: object) -> str:  # noqa: ARG001
    """The default sink: a client driven outside a run journals nowhere."""
    return ""


class PersonConnection:
    """One SQLite session, one client, two peers (§8) — and three chat operations.

    **One session file backs one client.** That is Telethon's own rule and it is
    not advisory: the session is an SQLite file holding a bearer auth key, and two
    clients opened on it race each other's writes and present the same key on two
    connections. The two lanes talk to **two bots**, so the harness needs two
    peers and not two accounts — which is why the peer is an argument of every
    operation rather than a property of the client.

    The harness is a pytest suite and pytest is synchronous, so the event loop is
    owned here — created, handed to Telethon, and closed with the client. Every
    call goes through `run`, under a lock, because `run_until_complete` is not
    re-entrant and the two lanes call it from two threads.

    The chat surface is **`mark`, `arrived` and `reply`, and nothing else**. No
    read-by-id, no send, no search, no matching by name and no polling loop: a
    mark is taken before the turn that should produce a message, and what the
    turn produced is everything that arrived after it — bounded by the count of
    ids the engine logged, which is what makes a green row a row about this run's
    own turn rather than about a stranger's (§9, #109, #354).
    """

    def __init__(
        self,
        *,
        directory: Path | None = None,
        journal: Any = _no_journal,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        import asyncio

        from telethon import TelegramClient

        self._directory = directory or person_directory(environ)
        self._credentials = load_credentials(self._directory, environ)
        self._journal = journal
        self._loop = asyncio.new_event_loop()
        self._client = TelegramClient(
            str(session_path(self._directory).with_suffix("")),
            self._credentials.api_id,
            self._credentials.api_hash,
            loop=self._loop,
        )
        self._lock = threading.Lock()
        self.account: Any | None = None

    def __enter__(self) -> PersonConnection:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- lifetime ------------------------------------------------------------

    def open(self) -> PersonConnection:
        """Connect, and refuse if this session is not an authorised account."""
        self.run(self._client.connect())
        if not self.run(self._client.is_user_authorized()):
            raise PersonError(
                f"the Telegram user-account session at {session_path(self._directory)} is not "
                f"authorised. Run `python tests/acceptance/telegram_person.py login` once."
            )
        self.account = self.run(self._client.get_me())
        # The account is who, not what it can prove: no `api_id`, no `api_hash`
        # and no session path reaches this line (§8).
        self._journal(
            "telegram.person.opened",
            account_id=getattr(self.account, "id", None),
            account_username=getattr(self.account, "username", None),
        )
        return self

    def close(self) -> None:
        shut_down(self._client, self._loop)

    def run(self, coroutine: Any) -> Any:
        with self._lock:
            return self._loop.run_until_complete(coroutine)

    # -- the peers -----------------------------------------------------------

    def peer(self, username: str) -> Any:
        """The entity a bot's username stands for. Not a chat operation.

        The username is handed down from the Bot API's own `getMe`, so no bot is
        named anywhere in this suite. Resolving it touches the account's contacts
        and never the chat — which is why "three chat operations" is `mark`,
        `arrived` and `reply` and this is not one of them.
        """
        return self.run(self._client.get_entity(username))

    # -- the three chat operations -------------------------------------------

    def mark(self, peer: Any) -> int:
        """Where this chat has got to, as **this account** sees it (§2 item 2, §9).

        The newest message's id, and **0 for a chat holding nothing** — which is a
        real answer rather than a missing one: everything in an empty chat arrived
        after the mark, and the first run against a fresh bot is exactly that.

        Taken before the turn whose message is about to be read, so what arrived
        after it belongs to that turn. The ids are the account's own sequence and
        have nothing to do with the ids the product issued (#354).
        """
        found = newest_id(self.run(self._collect(peer, limit=1)))
        self._journal("telegram.person.mark", mark=found)
        return found

    def arrived(self, peer: Any, since: int) -> tuple[PersonMessage, ...]:
        """Everything newer than a mark, **oldest first, read once** (§2 item 2, §9).

        One call and no polling: the caller reads this only once the engine has
        logged the send, and the engine logs it after the Bot API returned — so
        the message is already on the server and a window watched for it would be
        the waiting ADR 0021 §10 forbids.

        **`limit` is stated, and that is load-bearing.** Telethon's
        `get_messages` quietly defaults it to 1 unless *both* `min_id` and
        `max_id` are named (`telethon/client/messages.py`), so the bare
        `get_messages(peer, min_id=…)` §9 describes would read one message however
        many arrived — and the count a split send is graded against (#189) would
        false-fail on a read this function capped itself.
        """
        messages = in_arrival_order(self.run(self._collect(peer, limit=None, min_id=int(since))))
        self._journal(
            "telegram.person.arrived",
            since=int(since),
            messages=[message.as_journal_fields() for message in messages],
        )
        return messages

    def _collect(self, peer: Any, **query: Any) -> Any:
        """The one place the chat is read at all, so there is one query to audit."""
        return self._client.get_messages(peer, **query)

    def reply(self, peer: Any, reply_to_message_id: int, text: str) -> PersonMessage:
        """A reply **anchored to an id** (§2 item 5, ADR 0021 §2–§3).

        The anchor is set explicitly rather than left to the product's "reply to
        nothing → newest Anchor" fallback, because the fallback is a second path
        and the item is about the primary one.
        """
        sent = self.run(self._client.send_message(peer, text, reply_to=int(reply_to_message_id)))
        message = as_person_message(sent)
        self._journal(
            "telegram.person.replied",
            reply_to_message_id=int(reply_to_message_id),
            **message.as_journal_fields(),
        )
        return message


def shut_down(client: Any, loop: Any) -> None:
    """Disconnect a client whose loop this code owns, then close the loop.

    `TelegramClient.disconnect` is a **dual-form** API: with the loop running it
    returns an awaitable, and with the loop stopped it runs the loop itself and
    returns `None`. Every call here is from outside the loop, so it takes the
    second path — and wrapping `None` in `run_until_complete` is a `TypeError`
    raised out of a `finally`, which is how a *successful* login came to end in a
    traceback with its session file left at 0644.
    """
    import asyncio
    import inspect

    closing = client.disconnect()
    if inspect.isawaitable(closing):
        loop.run_until_complete(closing)
    if loop.is_closed():
        return
    # `disconnect` *requests* cancellation of Telethon's background loops; a loop
    # closed in the same breath never gives them the turn they need to finish,
    # and asyncio prints "Task was destroyed but it is pending!" once per task.
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if pending:
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


# --- the one-time login (§8) -------------------------------------------------


def _prompt(question: str) -> str:
    answer = input(question).strip()
    if not answer:
        raise PersonError("nothing entered")
    return answer


def _client_on(credentials: ApiCredentials, session: Path) -> tuple[Any, Any]:
    """A client on a session **nobody has signed into yet** — `login`'s alone.

    `PersonConnection` refuses an unauthorised session, which is right for a run
    and for `status` and is exactly what `login` is there to change. So this is
    the one place that builds a client outside it, and it has one caller.
    """
    import asyncio

    from telethon import TelegramClient

    loop = asyncio.new_event_loop()
    return (
        TelegramClient(
            str(session.with_suffix("")), credentials.api_id, credentials.api_hash, loop=loop
        ),
        loop,
    )


def login(directory: Path | None = None) -> int:
    """Authorise the account once, interactively — the one human step of §8."""
    from telethon.errors import SessionPasswordNeededError

    directory = directory or person_directory()
    try:
        credentials = load_credentials(directory)
        print(f"Using the api_id already stored at {credentials_path(directory)}.")
    except PersonError:
        print(
            "This account needs an api_id and api_hash from https://my.telegram.org "
            "→ API development tools. They are issued once, per account."
        )
        credentials = ApiCredentials(int(_prompt("api_id: ")), _prompt("api_hash: "))
        print(f"Stored 0600 at {store_credentials(credentials, directory)}.")

    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(PRIVATE_DIRECTORY)
    client, loop = _client_on(credentials, session_path(directory))
    try:
        loop.run_until_complete(client.connect())
        if loop.run_until_complete(client.is_user_authorized()):
            me = loop.run_until_complete(client.get_me())
            print(f"Already authorised as {me.first_name} (@{me.username}, id {me.id}).")
            return 0
        phone = _prompt("phone number, with country code (e.g. +64…): ")
        loop.run_until_complete(client.send_code_request(phone))
        code = _prompt("the code Telegram just sent: ")
        try:
            loop.run_until_complete(client.sign_in(phone, code))
        except SessionPasswordNeededError:
            loop.run_until_complete(
                client.sign_in(password=_prompt("two-step verification password: "))
            )
        me = loop.run_until_complete(client.get_me())
        print(f"Authorised as {me.first_name} (@{me.username}, id {me.id}).")
    finally:
        # Before the disconnect, not after: the session file is a bearer
        # credential for a whole account, and a shutdown that raises must not be
        # what decides whether it is readable by everyone on this machine.
        written = session_path(directory)
        if written.exists():
            written.chmod(PRIVATE_FILE)
            print(f"Session written 0600 at {written}.")
        shut_down(client, loop)
    return 0


def status(directory: Path | None = None) -> int:
    """Report whether a run could use this account. Preflight asks the same question."""
    directory = directory or person_directory()
    session = session_path(directory)
    if not session.exists():
        print(f"NOT AUTHORISED: no session file at {session}")
        return 1
    try:
        # Asked before the lock is taken, and its answer thrown away: a machine
        # with no credentials has nothing to hold the session for, and the
        # refusal reads better than "IN USE" would.
        load_credentials(directory)
    except PersonError as refusal:
        print(f"NOT AUTHORISED: {refusal}")
        return 1
    # The lock is taken before the client is built, not after: connecting to a
    # session another run holds is where `database is locked` comes from, and
    # that message names SQLite rather than the run in the way. Held for the
    # length of this answer only — `status` is a question, not a run — and so it
    # records itself as a `status` check rather than as a run that never existed.
    try:
        with PersonSessionLock(directory=directory, held_by=STATUS_CHECK_HOLDER):
            return _report_authorisation(directory, session)
    except SessionInUse as in_use:
        print(f"IN USE: {in_use}")
        return 1


def _report_authorisation(directory: Path, session: Path) -> int:
    """The account question itself, asked the way a **run** asks it.

    Through `PersonConnection` rather than a second client of its own, so
    `status` answering and a run starting cannot come to disagree: connect,
    authorised, `get_me` is one sequence and it lives in one place.
    """
    connection = PersonConnection(directory=directory)
    try:
        connection.open()
    except PersonError as unauthorised:
        print(f"NOT AUTHORISED: {unauthorised}")
        return 1
    finally:
        connection.close()
    me = connection.account
    assert me is not None  # `open` set it, or it raised
    print(f"AUTHORISED as {me.first_name} (@{me.username}, id {me.id}); session {session}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("login", "status"))
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return login() if arguments.command == "login" else status()
    except PersonError as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
