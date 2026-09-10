"""The five lane items, written once and walked by both lanes (#350, #352, #353).

**Responsibilities held here** (§9's `journey.py`): the lane as a value, the
engine-log lines and receipts the items read, §3's three turns, and the walk of
§2's five per-lane items. #352 laid the lane value, the arrangement every item
stands on — the boot turn, the daemon membership, the switches, the drained boot
notice — and the first item, `roster`; #353 adds the turns and the four items
read from them, so a no-option run is now the whole of §2 in code.

The lane is a value rather than a module per lane. The two used to be separate
files whose bodies were the same forty lines with one name changed, and the same
forty lines is where a fix applied to one lane and not the other comes from. So
the difference between the lanes lives entirely in `Lane`, and pytest's own
parametrisation names the lane in the test id (`test_lanes.py`).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import deadlines
import hand_started
import items
import support

from gpt_voicecoding.control_plane.commands import format_address
from gpt_voicecoding.installation import claude_hooks
from gpt_voicecoding.seams.agent import ApprovalVerdict, WaitingKind
from gpt_voicecoding.seams.delivery import Delivery

#: The engine's own line for a Stop it announced (`core/bridge.py`), and the
#: only engine-log line this harness matches at all (§2 item 2 reads the send
#: record beside it; §2 item 5 grades no log line whatsoever).
#:
#: **The record is several lines** (#189): a Stop Notice is a Session Brief and
#: the log carries `Briefing.text` whole, so `Session stopped:` opens the first
#: line and the brief's labelled lines follow. Matched line by line, so this
#: matches once per Stop — the header.
#:
#: `tests/test_bridge.py` asserts the product still writes a first line this
#: matches. That check is the reason this constant is public, and it is why it
#: stayed in this module through #350's rewrite.
ENGINE_STOP_LINE = r"(?i)Session stopped:"

#: The words the Codex lane's TUI is **launched** with (§3's boot turn, #110).
#:
#: The same sentence §3 gives the acknowledge turn, for two reasons: it is the
#: cheapest turn this harness knows — one word out, no tool use, so it can raise
#: no permission with nothing yet allowed to answer one — and a boot prompt is a
#: real turn, so it is billed like one. Its Stop Notice is drained on a
#: pre-`roster` mark, so nothing it produces can be read as a later `stop
#: notice` (#109's shape, read by mark rather than by name).
BOOT_WORDS = "reply with the single word READY, and use no tools"


#: The engine's line for a Companion Channel message it sent: who it was about,
#: and the ids it landed under (`core/bridge.py`). Every chat read in this
#: harness is **bounded** by one of these lines — the chat is never searched and
#: a message is never matched by the Session's name (§9, #109) — and two things
#: decide which line is this turn's: a **mark**, a position in the lane's engine
#: log taken before the turn, and the **address** on the line itself (#355).
#: Nothing is read *by* the ids: a private chat has two id spaces and this
#: harness reads the account's, so what the ids give is a count (#354).
#:
#: `_SENT` is the words the line opens with, spelled **once** for the two fields
#: read off it below: a send line matched by one spelling and parsed by another
#: is two readings of one contract, and only one of them gets fixed.
_SENT = r"sent Companion Channel message "

ENGINE_SENT_LINE = _SENT + r".*message_ids="

#: The ids on that line, as the engine joined them (`core/bridge.py`).
_MESSAGE_IDS = re.compile(ENGINE_SENT_LINE + r"(?P<ids>\S*)")

#: Who that send was **about**, as the engine wrote it on the same line: the
#: address of the Anchor's target, spelled the way the roster row spells it, and
#: empty for a send that named nobody (`core/bridge.py`, #355). One engine
#: bridges every Session on the machine, so this field is the only thing that
#: tells a lane's own send from a send about somebody else's Session.
#:
#: Its own pattern rather than one expression with both groups: `message_ids=`
#: is read off lines this harness does not require a `target=` on at all — the
#: boot notice's drain among them — and one pattern demanding both fields would
#: make an id unreadable for want of an address.
_SEND_TARGET = re.compile(_SENT + r".*target=(?P<target>\S*)")

#: §3's three turns, by name. The name is what the journal's `turn` line carries
#: and what a reader looks for when a run drifts past five minutes (§7).
ACKNOWLEDGE_TURN = "acknowledge"
RELAY_TURN = "relay"
INBOUND_TURN = "inbound"

#: The acknowledge turn, word for word (§3). No tool use **on purpose**: a turn
#: that raised a permission would sit in `waiting` with nothing allowed to
#: answer it yet. It ends on its own, and the Stop it ends with is what `stop
#: notice` reads.
ACKNOWLEDGE_WORDS = "reply with the single word READY, no tools"

#: The two turns that leave a file, and the file and word each leaves (§3).
#: **Distinct on purpose**: one filename and one word per turn, so no effect can
#: be mistaken for another and one item's failure cannot look like another's.
RELAY_FILE = "relay.txt"
RELAY_WORD = "BRAVO"
INBOUND_FILE = "inbound.txt"
INBOUND_WORD = "CHARLIE"

#: Where the Codex lane's relay write lands: a directory of its own under the
#: run directory, which is ground no Codex sandbox may write (preflight refuses
#: a run root inside one) and which is not the list §7 draws at the run
#: directory's root.
OUTSIDE_DIRECTORY = "outside"

#: What the user's say-so is spelled as on the `approve` verb — the product's
#: own word rather than a second spelling of it.
ALLOW = str(ApprovalVerdict.ALLOW)

#: The one grade that means the words reached the model, read from the product's
#: own classification (`seams/delivery.py`). Everything else — including a
#: `retained` receipt — is not this.
DELIVERED = str(Delivery.DELIVERED)

#: The field a receipt is graded on, and the shape every field on that line has
#: (`core/relays.py`'s `receipt_line`: `state=… grade=… reason=…`).
GRADE_FIELD = "grade"
_RECEIPT_FIELD = re.compile(r"(?P<name>[a-z_]+)=(?P<value>\S+)")


def message_ids(line: str) -> tuple[str, ...]:
    """Every id the engine reported sending one Companion Channel message under.

    A line that is not a send carries none, and a send that reached nobody
    carries an empty field — both are answers rather than errors, because the
    item that reads them grades the absence itself.
    """
    found = _MESSAGE_IDS.search(line)
    if found is None:
        return ()
    return tuple(one for one in found["ids"].split(",") if one)


def send_target(line: str) -> str:
    """The Session a send line says it was about, or an empty string for nobody.

    A line that is not a send, and a send that named no target, both answer the
    same way — and neither is this lane's Stop: an address is what makes a send
    one's own, so "no address" is never a match.
    """
    found = _SEND_TARGET.search(line)
    return "" if found is None else found["target"]


def addressed_elsewhere(lines: Sequence[str], address: str) -> str:
    """What the engine sent in that time about somebody else, as a clause or nothing.

    Read only on the way out of a failure (§7: a deadline hit names what never
    ended). A run whose notice never came has one question worth answering
    first — did the engine send nothing at all, or something about a Session
    that is not this lane's? — and the answer is on the lines it already has.
    """
    strangers = sorted({send_target(line) for line in lines} - {address})
    if not strangers:
        return ""
    named = ", ".join(one or "nobody" for one in strangers)
    return f"; the engine did send in that time, about {named} and not {address}"


def sent_for(lines: Sequence[str], address: str) -> list[str]:
    """Of those send lines, the ones addressed to this Session (§2 item 2, #355).

    **Never by recency.** The newest send in the engine's log is not this turn's
    Stop: one engine bridges every Session on the machine, and taking the newest
    line took a Stop Notice for the *other* lane's boot turn — sent 0.16 s before
    this lane's own turn was even typed (run `20260910T191643Z`). A mark keeps
    out what is older than the turn; only the address keeps out what belongs to
    somebody else.

    **An empty address matches nothing**, and that is the rule rather than an
    edge case: `send_target` answers the empty string for a send that named
    nobody — a `/status` answer, a refusal — and for any line that carries no
    such field at all, so an equality test against an empty address would take
    every one of them. A caller with no address for its own Session has nothing
    to match on, and saying so here keeps that true of every caller.
    """
    if not address:
        return []
    return [line for line in lines if send_target(line) == address]


def not_the_stop_notice(
    issued: Sequence[str], arrived: Sequence[Any], *, sent: Sequence[str]
) -> str | None:
    """Why what reached the chat is not this turn's Stop Notice, or `None` when it is.

    §2 item 2's binary check as amended (#354), in one place so every way of
    failing it reads the same: the engine said it sent messages under some ids,
    and **exactly that many** arrived in the chat after the mark, none of them
    this account's own.

    * no ids at all — the engine's own record of a send that reached nobody;
    * a different number — nothing arrived, or something else did too; a split
      send (#189) expects as many messages as it has ids;
    * an `outgoing` message among them — the account's own words, which is the
      harness reading itself rather than the bot.

    Counted rather than keyed, because the ids cannot be read back: a private
    chat gives each account its own sequence, so the Bot API's ids are the bot's
    and this account cannot address them.

    **Every branch names both sides** — the lines the engine wrote *and* the
    messages the chat holds — because a reader of a red row cannot tell which of
    the two went wrong from one of them, and because a branch that saw only one
    side would be tempted to claim the other: "no ids issued" is a fact about the
    engine's record and says nothing whatever about what is in the chat (§1 rule
    1: nothing is guessed).
    """
    evidence = (
        f"the engine's own send lines after the mark were {list(sent)!r}, and the messages that "
        f"arrived after the chat mark were {[(one.id, one.text) for one in arrived]!r}"
    )
    if not issued:
        return (
            f"the engine recorded sending a Companion Channel message under no id at all, so by "
            f"its own record this turn's Stop reached nobody: {evidence}"
        )
    if len(arrived) != len(issued):
        return (
            f"the engine issued {len(issued)} id(s) for this turn's Stop ({list(issued)!r}) and "
            f"{len(arrived)} message(s) arrived in the chat after the mark: {evidence}"
        )
    own = [(one.id, one.text) for one in arrived if one.outgoing]
    if own:
        return (
            f"what arrived after the mark includes this account's own message(s) {own!r} rather "
            f"than the bot's alone: {evidence}"
        )
    return None


def receipt_fields(text: str) -> dict[str, str]:
    """A receipt as the fields it is, never as a string to search (§2 item 3).

    `bridgectl` prints one format for every receipt — `state=… grade=… reason=…`,
    with `verdict=…` in front of an `approve` reply — and this reads it back as
    what it is. A substring on `delivered` false-passes a **retained** relay
    whose reason merely mentions the word (#71), which is the defect this
    function exists to make impossible.
    """
    return {found["name"]: found["value"] for found in _RECEIPT_FIELD.finditer(text)}


def undelivered(text: str) -> str | None:
    """Why this receipt is not a delivery, or `None` when it is.

    One field decides: `grade`, compared **literally** to the product's own
    `delivered`. `state` is not consulted — a Relay's lifecycle is a fact about
    the queue, and the grade is the fact about the attempt.
    """
    grade = receipt_fields(text).get(GRADE_FIELD)
    if grade == DELIVERED:
        return None
    return (
        f"the receipt's {GRADE_FIELD} field is {grade!r} and not {DELIVERED!r}, so nothing "
        f"proves the words reached the model: {text!r}"
    )


def approval_id(row: Mapping[str, Any]) -> str | None:
    """The permission on this roster row, read from `waiting_for` (§2 item 4).

    **Never from `state`** (#191): a Codex thread stays `running` with its
    permission dialog on screen, so a reader that waited for a state to change
    would wait for something that never happens.
    """
    waiting = row.get("waiting_for")
    if not isinstance(waiting, Mapping):
        return None
    identifier = waiting.get("approval_id")
    return str(identifier) if identifier else None


def stopped_on(row: Mapping[str, Any]) -> str | None:
    """What the Session stopped on, or `None` when the engine cannot yet say.

    §2 item 2 grades no wording: the check is that `waiting_for` carries a kind
    from the product's own closed set. `unknown` is not one — it is "something is
    being waited on and we cannot yet say what" (`seams/agent.py`), which is the
    absence of the answer rather than an answer.
    """
    waiting = row.get("waiting_for")
    if not isinstance(waiting, Mapping):
        return None
    kind = waiting.get("kind")
    if not kind or str(kind) == str(WaitingKind.UNKNOWN):
        return None
    return str(kind)


def write_words(path: Path, word: str) -> str:
    """One turn's whole instruction: one sentence, one path, one word (§3, §1 rule 3).

    Nothing asks the model to talk, count, recite or explain, and the path is
    absolute so the effect is where the harness looks rather than where the
    agent's cwd happened to be.
    """
    return f"write {path} containing {word}"


def relay_target_file(lane: Lane, *, workspace: Path, run_directory: Path) -> Path:
    """Where the relay turn's file goes — the one variable between the lanes (§3).

    Inside the workspace where nothing confines the writes: Claude asks for the
    permission because the run keeps the product's own permission mode. Outside
    every writable root where a sandbox does: Codex asks only for a write its
    sandbox refuses, so the **path** is what raises the permission `approval`
    grades (#105). One file, one word, both lanes.
    """
    if lane.sandboxed:
        return run_directory / f"{OUTSIDE_DIRECTORY}-{lane.name}" / RELAY_FILE
    return workspace / RELAY_FILE


def inbound_target_file(workspace: Path) -> Path:
    """Where the inbound turn's file goes — inside the workspace on either lane.

    §2 item 5 is graded by the file alone and raises no permission: the transport
    is what it proves, and a second sandbox refusal on the way would prove the
    sandbox instead.
    """
    return workspace / INBOUND_FILE


def wrote(path: Path, word: str) -> bool:
    """Whether the turn's file is there carrying its word — the one binary signal."""
    try:
        return word in path.read_text(errors="replace")
    except OSError:
        return False


@dataclass(frozen=True)
class Chat:
    """One lane's private chat with its bot — and the only three things done to it.

    `mark`, `arrived` and `reply`, and no fourth. There is **no send**: §2 item 5
    uses the product's primary inbound form (ADR 0021 §2–§3), a reply anchored to
    the Stop Notice's own id, so an inbound with no anchor is impossible by
    construction rather than by care. There is **no read-by-id** either: a private
    chat gives each account its own id sequence, so the ids the product issued are
    the bot's and are not addressable from here (#354). And there is no search: a
    mark taken before the turn is what separates this turn's messages from the
    chat's history (§9, #109).
    """

    #: The entity the lane's bot username stands for, resolved once per lane.
    peer: Any
    #: The user-account client (`telegram_person.PersonConnection`), which the
    #: whole run shares: one SQLite session backs one client, and the two lanes
    #: differ by peer rather than by account.
    connection: Any

    def mark(self) -> int:
        """Where this chat has got to, as the account sees it. 0 for an empty chat."""
        return self.connection.mark(self.peer)

    def arrived(self, since: int) -> tuple[Any, ...]:
        """Everything newer than a mark, oldest first, read once and never waited on."""
        return tuple(self.connection.arrived(self.peer, int(since)))

    def reply(self, anchor: str | int, words: str) -> Any:
        """Plain words, into the chat, anchored to a message the Session's own."""
        return self.connection.reply(self.peer, int(anchor), words)


@dataclass(frozen=True)
class Lane:
    """One lane: one engine, one hand-started Session, one bot, one chat (§2).

    Everything that differs between the lanes lives here, so no other module
    asks which lane it is holding: the launch argv, the boot turn, the config
    directory each lane may or may not have (§4.1), and the two ways §4.3 keeps
    one lane's engine out of the other's view — the agent kind it drops, and
    whether its engine runs on an empty `CODEX_HOME` of its own.

    The model pins are the lane's, not a step's (§3). The Codex lane carries
    **no** other launch argument: any `-c` override makes codex-tui run its own
    core instead of joining the shared daemon, and the lane's Session vanishes
    from `roster` (#232).
    """

    name: str
    #: The binary a Session is started from, resolved on the login shell's PATH
    #: and never through a shell function (§4.4).
    binary: str
    #: The agent kind this lane's Sessions are, as the engine spells it
    #: (`seams.identity.AgentKind`) — the name in `[adapters.agents]` and the
    #: first field of every address on the roster.
    agent: str
    #: How this lane's binary spells "use this model" — `claude` takes
    #: `--model`, `codex` takes `-m`. A field rather than a rule inferred from
    #: the other pins: the flags are the lane's, and inferring one is how a lane
    #: comes to be launched with a flag nobody chose.
    model_flag: str
    #: What the lane's turns are billed to (§3).
    model: str
    #: Claude's alone; Codex takes no second pin, for the reason above.
    effort: str | None = None
    #: The product's **own** permission mode, passed rather than overridden:
    #: overriding it would pre-approve the very thing `approval` observes (#60).
    #: Claude's alone; Codex has no such flag and pins nothing here.
    permission_mode: str | None = None
    #: The words this lane's TUI is *launched* with (§3's boot turn), or nothing.
    #: Non-empty is the whole mechanism on the Codex lane: an empty string is not
    #: a skipped update gate (#110).
    boot_words: str | None = None
    #: Whether this lane owns a config directory (§4.1). The Claude lane does;
    #: the Codex lane cannot, under ADR 0022.
    own_config_directory: bool = False
    #: Whether this lane's Session may only write inside its workspace (§3).
    #: A fact about the **agent's sandbox** and not about its configuration, so
    #: it is its own field: it is what decides where the relay turn's file has
    #: to go for the write to raise a permission at all (#105).
    sandboxed: bool = False

    @property
    def arguments(self) -> tuple[str, ...]:
        """This lane's launch flags, in one place because they are the lane's (§3).

        Built rather than written out so the Codex lane cannot grow a second
        flag by someone adding a field: it has a model and nothing else, and
        `tests/test_harness_lanes.py` reads this tuple for a `-c` on every run
        of the fast suite.
        """
        flags: list[str] = [self.model_flag, self.model]
        if self.effort is not None:
            flags += ["--effort", self.effort]
        if self.permission_mode is not None:
            flags += ["--permission-mode", self.permission_mode]
        return tuple(flags)

    @property
    def dropped_agents(self) -> tuple[str, ...]:
        """The agent kinds this lane's engine must not load (§4.3).

        Only the Claude kind is ever dropped, and only on the lane that will
        never walk a Claude route: the machine's one Claude approval address has
        no environment to read (ADR 0019), so exclusion is what leaves exactly
        one claimant (#202).

        **The Codex kind is never dropped, and cannot be**: `call` is a required
        seam, the only Call adapter rides the app-server the Codex agent adapter
        owns, and engine assembly refuses without it. What keeps that kind from
        seeing the *other* lane's Session is `empty_codex_home` below, not a
        drop.
        """
        return () if self.agent == items.LANES[0] else (items.LANES[0],)

    @property
    def empty_codex_home(self) -> bool:
        """Whether this lane's **engine** runs on a `CODEX_HOME` of its own (§4.3).

        The Claude lane's does, and the directory is empty: the shared-daemon
        lookup derives its control socket from `CODEX_HOME` when it is *called*
        (`adapters/agent/codex/shared_daemon.py`), so an empty one has nobody
        behind it — that engine's Codex lane reports itself `degraded` and empty
        and it sees no Codex thread on the machine, the other lane's or the
        operator's. Without it that engine joined the operator's shared
        app-server and announced the Codex lane's boot turn to the Claude lane's
        chat (#355, run `20260910T191643Z`).

        The Codex lane's engine never gets one: ADR 0022 derives the socket and
        the TUI's launch environment from one `CODEX_HOME`, so a lane with its
        own finds no server and its Session vanishes from `roster` (§4.1, #232).
        A property beside `dropped_agents` because it answers the same question
        that one does — what this lane's engine must not see — for the kind a
        drop cannot reach.
        """
        return self.agent == items.LANES[0]


LANES: tuple[Lane, ...] = (
    Lane(
        name=items.LANES[0],
        binary="claude",
        agent=items.LANES[0],
        model_flag="--model",
        model="sonnet",
        effort="medium",
        permission_mode="default",
        # Launched silent: `claude` boots into an empty composer, no update gate
        # of the Codex kind has been measured here, and a Session nobody has
        # typed into is what `roster` wants to find.
        boot_words=None,
        own_config_directory=True,
    ),
    Lane(
        name=items.LANES[1],
        binary="codex",
        agent=items.LANES[1],
        model_flag="-m",
        model="gpt-5.6-luna",
        boot_words=BOOT_WORDS,
        sandboxed=True,
    ),
)

#: What `[delegate] model` is pinned to in **both** lanes' derived configs (§3):
#: the Delegated Turn runs on codex whichever lane raised it, and a run that
#: copied the operator's own value would bill whatever they last used. Read off
#: the lane rather than spelled again, because they are one bill.
DELEGATED_TURN_MODEL = LANES[1].model


def lane(name: str) -> Lane:
    """The lane a `--lane` name stands for."""
    return next(one for one in LANES if one.name == name)


# --- reading the roster (§2 item 1) -----------------------------------------


def roster_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every row the engine holds, with every field on it.

    Read from `status`'s **payload** rather than from the lines it renders into:
    the rendering carries a name, an address, a workspace, a state and a window
    (`control_plane/commands.py:341`), and the join below needs the `session_id`
    and the `pid` that no rendered line has.
    """
    return [row for row in payload.get("sessions", []) if isinstance(row, dict)]


def roster_row(
    rows: Sequence[Mapping[str, Any]], truth: hand_started.GroundTruth
) -> Mapping[str, Any] | None:
    """The row for the Session the harness started — by id, else by pid (§2 item 1).

    The session id is the exact key and is used whenever the agent has one.
    There is not always one: `codex` writes the rollout that names it when the
    first *turn* starts (measured 2026-08-26), so before that the only thing the
    two sides can agree on is the process. `SessionTarget` carries the pid and
    the product's own Codex discovery finds rows by pid and cwd, so matching on
    it here is the same join the product has to make — not a weaker one the
    harness invented for itself.
    """
    for row in rows:
        target = row.get("target")
        if not isinstance(target, Mapping):
            continue
        if truth.session_id and str(target.get("session_id")) == truth.session_id:
            return row
        if not truth.session_id and target.get("pid") == truth.pid:
            return row
    return None


def not_a_target(row: Mapping[str, Any], workspace: Path) -> str | None:
    """Why this row is not a real target, or `None` when it is (§2 item 1).

    Two facts and no third: an **address** a surface could name it by, and the
    **workspace** the harness made — which is the join that makes this row this
    Session rather than a coincidence. `roster` grades no reach and no
    provenance: every listed Session is one the bridge talks to, and a route
    that fails surfaces as a delivery failure in `relay` (#68, #73).
    """
    target = row.get("target")
    address = format_address(dict(target)) if isinstance(target, Mapping) else ""
    if not isinstance(target, Mapping) or not (target.get("session_id") or target.get("pid")):
        return f"the roster row carries no address a surface could name it by: target is {target!r}"
    listed = row.get("workspace")
    if not listed or os.path.realpath(str(listed)) != os.path.realpath(workspace):
        return (
            f"{address} is listed against workspace {listed!r}, not the one the harness "
            f"started it in ({workspace})"
        )
    return None


def address_of(row: Mapping[str, Any]) -> str:
    target = row.get("target")
    return format_address(dict(target)) if isinstance(target, Mapping) else ""


# --- walking one lane (§2, §6) ----------------------------------------------


class LaneBlocked(RuntimeError):
    """The lane cannot be walked, and no item of it can be read on this ground."""


class ItemFailed(RuntimeError):
    """One item read the product and the product did not do what §2 says it does."""


#: Voice **off**, Message **on**, Duty **on** for the whole run (§4.2). Armed
#: rather than configured, because there is no configuration key for a switch:
#: a fresh engine answers `switches: duty off, message off, voice off`, so an
#: unarmed run would see no push anywhere and read one cause as four failures.
SWITCH_POSITIONS: tuple[tuple[str, str], ...] = (
    ("voice", "off"),
    ("message", "on"),
    ("duty", "on"),
)


def unarranged(
    lane: Lane,
    *,
    selection: items.Selection,
    journal: support.Journal,
    verdict: support.Verdict,
    why: str,
) -> None:
    """A lane that could not be arranged, said in rows rather than in a traceback.

    Preflight is the only thing that REFUSES (§5): it runs before any engine and
    a refusal means nothing was observed. Past it, a lane whose engine or Session
    will not start has **failed `roster`** — the Session never reached the
    product's roster, whatever stopped it — and everything behind it is SKIPPED
    naming that row. The run then exits non-zero on those rows, which is what
    keeps a lane that quietly did nothing from reading as a lane that passed.
    """
    reference = journal("lane.unarranged", lane=lane.name, why=why)
    if items.Item.ROSTER not in verdict.written(lane.name):
        verdict.record(items.Item.ROSTER, support.FAIL, reference, lane=lane.name)
    skip_the_rest(verdict, lane.name, selection, f"blocked by {lane.name}/{items.Item.ROSTER}")


def skip_the_rest(
    verdict: support.Verdict,
    lane: str,
    selection: items.Selection,
    why: str,
    *,
    after: items.Item | None = None,
) -> None:
    """Every item of this lane with no row yet, SKIPPED and saying why (§6, §7).

    `after` is the item whose own row was just written — the one being blamed —
    so a failure does not skip itself.
    """
    written = verdict.written(lane)
    for item in selection.items:
        if item in items.LANE_ITEMS and item not in written and item is not after:
            verdict.skip(item, why, lane=lane)


@dataclass
class Walk:
    """One lane's walk: the ground it stands on, then the items it was asked for.

    Everything the walk reads is passed in, and the two per-lane *readings* — who
    the agent says this Session is, and whether the shared daemon holds it — are
    callables rather than branches on the lane's name. A `Walk` never asks which
    lane it is walking.
    """

    lane: Lane
    selection: items.Selection
    journal: support.Journal
    verdict: support.Verdict
    bridgectl: support.Bridgectl
    engine: support.Engine
    session: hand_started.Session
    workspace: Path
    #: The agent's own account of the Session this harness started, or `None`
    #: while there is not one yet.
    truth: Callable[[], hand_started.GroundTruth | None]
    #: The run's own directory — where the Codex lane's relay write has to land,
    #: because it is ground that lane's sandbox refuses (§3, preflight's writable
    #: run root refusal). Every walk has one, so it is required rather than
    #: defaulted into a branch nothing reaches.
    run_directory: Path
    #: This lane's private chat with its bot, or `None` when the run arranged
    #: none: an item that needs it is **blocked**, never graded, because a chat
    #: the run could not open is the harness's own ground missing (§6).
    chat: Chat | None = None
    #: Whether the turn the *launch* started has ended, on the agent's own
    #: bracketing. Always over on a lane with no boot prompt.
    boot_turn_over: Callable[[], bool] = lambda: True
    #: Whether the shared Codex daemon holds this Session's thread, or `None` on
    #: a lane where the question does not arise.
    membership: Callable[[], support.DaemonMembership | None] = lambda: None
    #: Filled as the walk goes, so a failure message can say where it got to.
    boot_seconds: float | None = None
    #: The **account-side** id of the first message `stop notice` read, kept
    #: because `companion inbound` replies to it (§6: `stop notice` is that
    #: item's reply anchor). Never a product-issued id: Telethon's `reply_to`
    #: wants the id in *this account's* dialog, and a private chat's two id
    #: spaces are why the product's own is unusable here (#354).
    notice_anchor: int | None = None
    #: Every permission this walk has already answered as **arrangement**, so a
    #: dialog that lingers for a poll or two is answered once rather than once
    #: per reading.
    arranged: set[str] = field(default_factory=set)
    #: The clock and the sleep every wait on this lane is made through, as
    #: `deadlines.wait` takes them. Injectable for the reason that function
    #: gives: the rules above are minutes long — the boot turn is two turns —
    #: and what a fast test reads is what the harness *does* when one runs out,
    #: not that it can count. **Nothing in the run passes them.**
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def items_walked(self) -> dict[items.Item, Callable[[], str]]:
        """Every item of §2's table, bound to the method that reads it.

        Total over `items.LANE_ITEMS` (`tests/test_harness_turns.py`): a row
        nobody wrote would be named by `Verdict.missing` as something the run
        promised and did not write, and with #353 there is nothing left to
        promise and not write.
        """
        return {
            items.Item.ROSTER: self.roster,
            items.Item.STOP_NOTICE: self.stop_notice,
            items.Item.RELAY: self.relay,
            items.Item.APPROVAL: self.approval,
            items.Item.COMPANION_INBOUND: self.companion_inbound,
        }

    # -- the ground every item stands on -------------------------------------

    def walk(self) -> None:
        """Arrange this lane, then read the items this run asked for (§2, §6)."""
        try:
            boot_mark = self.settle_boot_turn()
            self.settle_daemon_membership()
            self.arm_switches()
            self.drain_boot_notice(boot_mark)
        except LaneBlocked as blocked:
            self.block(f"blocked by the {self.lane.name} lane's arrangement: {blocked}")
            return
        walked = self.items_walked()
        for item in self.selection.items:
            if item not in items.LANE_ITEMS:
                continue
            if not self.read(item, walked[item]):
                return

    def read(self, item: items.Item, reading: Callable[[], str]) -> bool:
        """One item, its row, and — on a failure — the rows it was holding up.

        A failed item **blocks** what stands on it (§6): the rest of this lane's
        selected items become SKIPPED naming this row, rather than being read on
        ground the run could not arrange.
        """
        started = time.monotonic()
        try:
            evidence = reading()
        except LaneBlocked as unarranged:
            # **Not a claim about the product** (§6, §1 rule 1): ground the run
            # could not arrange is a row that never ran, so it is SKIPPED saying
            # why — a red here would say the main flow broke when what broke was
            # this harness's own arrangement.
            why = f"blocked by the {self.lane.name} lane's arrangement: {unarranged}"
            self.journal(
                "item.unarranged",
                lane=self.lane.name,
                item=str(item),
                why=str(unarranged),
                **self.for_a_human(),
            )
            self.verdict.skip(item, why, lane=self.lane.name)
            self.block(why, after=item)
            return False
        except (ItemFailed, deadlines.DeadlineExpired) as failed:
            witness = self.for_a_human()
            reference = (
                self.journal.expired(failed, lane=self.lane.name, item=str(item), **witness)
                if isinstance(failed, deadlines.DeadlineExpired)
                else self.journal(
                    "item.failed",
                    lane=self.lane.name,
                    item=str(item),
                    why=str(failed),
                    **witness,
                )
            )
            self.verdict.record(
                item,
                support.FAIL,
                reference,
                lane=self.lane.name,
                seconds=time.monotonic() - started,
            )
            self.block(f"blocked by {self.lane.name}/{item}", after=item)
            return False
        self.verdict.record(
            item,
            support.PASS,
            evidence,
            lane=self.lane.name,
            seconds=time.monotonic() - started,
        )
        return True

    def for_a_human(self) -> dict[str, Any]:
        """The three places a red is looked at, named on the line that reports it (§3).

        "A real agent that does not perform the action is a FAIL, with the
        workspace, the pty log and the agent's own transcript as evidence." So a
        failing row's journal line carries all three rather than leaving a reader
        to reconstruct where this lane's run happened.

        The agent's own record is the agent's to give: `codex` writes a rollout
        and `GroundTruth` carries its path; `claude agents --json` names no
        transcript, so what is carried there is the **session id**, which is what
        locates the transcript inside the lane's own config directory (§4.1).
        Asked here rather than kept from `roster`, and never allowed to raise:
        this line is written on the way out of a failure.
        """
        try:
            record = self.truth()
        except Exception:  # noqa: BLE001 - an oracle that failed is not this row's cause
            record = None
        return {
            "workspace": str(self.workspace),
            "pty_log": str(self.session.transcript),
            # A **path** where the agent writes one, so a reader opens it rather
            # than hunts for it.
            "agent_transcript": str(record.record) if record and record.record else None,
            # Where the Claude lane's transcript is, for the lane whose roster
            # names none: `claude agents --json` carries a session id and no
            # transcript path, and the file is that id's inside the lane's own
            # config directory (§4.1).
            "agent_config_directory": self.session.environment.get(
                claude_hooks.CONFIG_DIRECTORY_VARIABLE
            ),
            "agent_record": record.describe() if record is not None else None,
        }

    def block(self, why: str, after: items.Item | None = None) -> None:
        """Every item of this lane the run has not written a row for, SKIPPED with why."""
        skip_the_rest(self.verdict, self.lane.name, self.selection, why, after=after)

    def settle_boot_turn(self) -> int | None:
        """Wait out the turn the *launch* started, and mark the engine log behind it.

        A lane with a boot prompt is running a turn from the moment it starts
        (#110 — a non-empty prompt is what carries it past the update gate).
        Nothing may be typed into a Session that is mid-turn, and no mark may be
        taken while one is in flight: a boot turn that ends *after* a later
        mark puts its own Stop Notice on the far side of it, and `stop notice`
        would pass on a notice for a turn nobody drove.

        This waits on the agent's **own** turn boundary rather than on the record
        going quiet, because a turn waiting on the model appends nothing either.
        The mark it answers is spent by `drain_boot_notice`.
        """
        if self.lane.boot_words is None:
            return None
        started = time.monotonic()
        try:
            self.waiting(
                "BOOT_TURN_SECONDS",
                self.boot_turn_over,
                what=f"the turn the {self.lane.name} lane's Session was launched with",
            )
        except deadlines.DeadlineExpired as unsettled:
            self.journal.expired(unsettled, lane=self.lane.name)
            raise LaneBlocked(
                f"the turn this lane was launched with had not ended, so the walk cannot type "
                f"into this Session without racing it. Screen tail: "
                f"{self.session.screen_tail()[-600:]!r}"
            ) from None
        self.boot_seconds = time.monotonic() - started
        self.journal("boot.turn", lane=self.lane.name, seconds=self.boot_seconds)
        return self.mark()

    def settle_daemon_membership(self) -> None:
        """Record whether the product can see this Session at all, and refuse if it cannot.

        **Not an item, and it must not become one** (#232). ADR 0020 defines a
        Codex Session as a daemon thread a terminal vouches for, so a
        hand-started TUI the shared daemon does not hold is a Session the product
        is *right* not to list — grading that is grading the harness's own ground
        as a product defect.

        **Here, because here is where the fact first exists and still costs
        nothing**: the thread id is written into the rollout when the first turn
        starts, and the turn just waited out is that turn. Nothing has been typed
        yet, no switch is armed, no mark has been spent, so a lane blocked at
        this line has cost one boot turn and nothing else.

        It records on **every** path and refuses on one: a daemon that is down,
        moved or unreadable is not evidence that a thread is absent from it.
        """
        membership = self.membership()
        if membership is None:
            return
        self.journal(
            "daemon.membership",
            lane=self.lane.name,
            flags=list(self.lane.arguments),
            thread_id=membership.thread_id,
            held=membership.held,
            held_threads=list(membership.held_threads),
            daemon=membership.daemon,
            reason=membership.reason,
        )
        refusal = membership.refusal(self.lane.arguments)
        if refusal is not None:
            raise LaneBlocked(refusal)

    def arm_switches(self) -> None:
        """Voice off, Message on, Duty on — the text-only mode this run exercises (§4.2)."""
        for name, position in SWITCH_POSITIONS:
            answer = self.bridgectl("switch", name, position)
            self.journal(
                "switch.armed", lane=self.lane.name, switch=name, to=position, reply=answer.text
            )
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")

    def drain_boot_notice(self, mark: int | None) -> None:
        """Let the boot turn's Stop Notice land before any later mark is taken (§3).

        **It records and never asserts**, because *no notice* is a legitimate
        answer: a Stop is only raised on a transition out of `active`, and an
        engine that first saw this thread already idle raised nothing for the
        boot turn. That case is also the one that pays the whole wait.

        Turning an outlet on asks the next discovery pass to reconcile current
        state, so the notice can arrive either side of `arm_switches`; the mark
        is taken **before** that, which is what makes this a deadline on the
        notice landing rather than a window it is watched in.

        **Bound to this lane's own address wherever there is one to bind to**
        (#356). One engine bridges every Session on the machine, so a send about
        somebody else's landing in this window settled the unfiltered wait early
        — and then this lane's own boot notice landed *behind* the acknowledge
        mark, where #355's address filter admits it and #354's count binding
        matches: `stop notice` green on a turn nobody drove, which is the very
        false green this drain exists to prevent. The selection is `sent_for`,
        the one `stop notice` reads its own Stop with, rather than a second way
        of asking the same question.

        The fallback is not a weaker reading of the same case but a different
        case: an engine holding **no row** by the time the boot turn is over
        never saw this Session active and will raise no Stop for the boot turn,
        so there is nothing for a later mark to admit. The row is read without
        raising for the same reason the drain does not assert — `roster` has not
        run yet, and an absence here is an answer.
        """
        if mark is None:
            return
        address = self.address_if_held()
        what = "the boot turn's Stop Notice reaching the chat"

        def notice() -> list[str]:
            return self.sent_since_for(mark, address) if address else self.sent_since(mark)

        try:
            sent = self.waiting(
                "TELEGRAM_ROUND_TRIP_SECONDS",
                notice,
                what=f"{what}, about {address}" if address else what,
            )
        except deadlines.DeadlineExpired:
            # Not a failure: an engine that first saw this thread already idle
            # raised nothing to drain, and there is no way to tell that apart
            # from a notice that never came — nor any need to.
            drained = None
        else:
            drained = sent[-1]
        self.journal(
            "boot.notice",
            lane=self.lane.name,
            drained=drained,
            mark=mark,
            # Which of the two readings above this run got, so a journal says
            # whether its drain could exclude a stranger's send or not.
            bound=bool(address),
            address=address or None,
        )

    # -- the engine log, and the marks every chat read starts from -----------

    def waiting(self, deadline: str, question: Callable[[], Any], *, what: str) -> Any:
        """One wait, on this lane's clock. Every wait in the walk goes through here."""
        return deadlines.wait(deadline, question, what=what, now=self.now, sleep=self.sleep)

    def mark(self) -> int:
        """Where this lane's engine log has got to (§2, §9).

        A mark is a position in the engine's own log, never a clock: what it has
        to say is "everything after here belongs to the turn I am about to
        drive". The chat has a mark of its own — `Chat.mark`, the newest message
        id as the user account sees it — and the two are taken together, before
        the turn is typed (§2 item 2 as amended, #354).
        """
        return len(self.engine.log_lines())

    def sent_since(self, mark: int) -> list[str]:
        """Every `sent Companion Channel message …` line the engine wrote after a mark."""
        pattern = re.compile(ENGINE_SENT_LINE)
        return [line for line in self.engine.log_lines()[mark:] if pattern.search(line)]

    def sent_since_for(self, mark: int, address: str) -> list[str]:
        """Those of them this lane's own Session is the subject of (§2 item 2, #355)."""
        return sent_for(self.sent_since(mark), address)

    # -- the items -----------------------------------------------------------

    def roster(self) -> str:
        """§2 item 1 — the Session has a row, and the row is a real target.

        Two claims and no third:

        1. the Session the harness started by hand — identified from the
           **agent's own** record, never from the engine — has a row;
        2. that row is a *target*: an address a surface could name it by, plus
           the workspace this harness made.

        Unreachability is not this item's business and has no row of its own: a
        Session the daemon cannot reach is refused above (§4.3), and a route that
        fails surfaces in `relay` as a delivery failure with a reason (#73).
        """
        truth = self.waiting(
            "ROSTER_SECONDS",
            self.truth,
            what=(
                f"the {self.lane.binary} Session the harness started appearing in the agent's "
                f"own record"
            ),
        )
        row = self.waiting(
            "ROSTER_SECONDS",
            lambda: roster_row(self.rows(), truth),
            what=(
                f"the Session the harness started appearing in the {self.lane.name} engine's roster"
            ),
        )
        unsound = not_a_target(row, self.workspace)
        if unsound is not None:
            raise ItemFailed(unsound)
        return self.journal(
            "roster.row",
            lane=self.lane.name,
            address=address_of(row),
            workspace=str(row.get("workspace")),
            agent_record=truth.describe(),
        )

    def rows(self) -> list[dict[str, Any]]:
        return roster_rows(
            self.bridgectl.status_payload(
                why="a roster row is matched by session_id else pid, and no rendered line "
                "carries either"
            )
        )

    def row(self) -> Mapping[str, Any]:
        """This lane's own roster row, read again — the payload, never a rendering.

        Read afresh at every use rather than kept from `roster`: the fields the
        items after it read — what the Session stopped on, the permission it
        raised — are the ones that change, and a row held from earlier would
        answer with what was true before the turn.
        """
        truth = self.truth()
        if truth is None:
            raise ItemFailed(
                f"the {self.lane.binary} Session the harness started is no longer in the agent's "
                f"own record, so no roster row can be matched to it"
            )
        row = roster_row(self.rows(), truth)
        if row is None:
            raise ItemFailed(
                f"the Session the harness started ({truth.describe()}) has no row in the "
                f"{self.lane.name} engine's roster"
            )
        return row

    def address_if_held(self) -> str:
        """This lane's own address where the engine already holds a row, and **never a raise**.

        `row()`'s question and `row()`'s own matching, asked where an absence is
        an answer rather than a failure: before `roster` there may be no agent
        record yet, no row yet, no engine answering `status` at all, or a row too
        malformed to render an address from — and none of those is a claim about
        the product. Every one of them answers the empty string, which is the
        answer `sent_for` already gives a caller with no address: nothing matches
        it (#356).

        **The address rather than the row**, so the one thing that can raise
        between a row and an address is inside the guard rather than after it.
        """
        try:
            return address_of(self.row())
        except Exception:  # noqa: BLE001 - every way of having no address yet is one answer
            return ""

    def the_chat(self) -> Chat:
        """This lane's chat, or a lane blocked for want of one."""
        if self.chat is None:
            raise LaneBlocked(
                f"the {self.lane.name} lane has no chat with its bot, and the items that read "
                f"one cannot be graded on ground the run did not arrange"
            )
        return self.chat

    def relay_file(self) -> Path:
        """Where this lane's relay turn writes, made ready for it (§3).

        The **directory** is the harness's own to arrange — on the Codex lane it
        is a fresh one outside every writable root — so a permission is raised by
        the write the item is about and not by a missing parent.
        """
        written = relay_target_file(
            self.lane, workspace=self.workspace, run_directory=self.run_directory
        )
        written.parent.mkdir(parents=True, exist_ok=True)
        return written

    def proven(self, answer: support.Answer, what: str) -> None:
        """A verb answered, and its receipt is a proven delivery — or the item failed.

        Both verbs this harness sends are graded the same way and are graded here
        once: the surface answered at all, and the receipt's `grade` field is
        literally `delivered` (§2 items 3 and 4). Two copies of this is how one
        verb comes to be graded more leniently than the other.
        """
        if not answer.ok:
            raise ItemFailed(f"{what} was refused: {answer.text}")
        unproven = undelivered(answer.text)
        if unproven is not None:
            raise ItemFailed(f"{what}: {unproven}")

    def stop_notice(self) -> str:
        """§2 item 2 — the acknowledge turn's end reaches the lane's chat.

        Three claims, in the order the run can make them:

        1. the engine **says** it sent a Companion Channel message for the Stop
           the turn ended with — addressed to *this* Session — and says under
           which ids;
        2. **as many messages as it issued ids for** arrived in the chat after a
           mark taken before the turn, and none of them is this account's own:
           read once through the user account, never searched for, never matched
           by name and never waited on (§9, #109). Not read *by* those ids — a
           private chat has two id spaces and the account cannot address the
           bot's (§2 item 2 as amended, #354);
        3. `status` says what the Session stopped on.

        **Two marks, both taken before the turn is typed** (§3): a position in
        this lane's engine log, and the chat's newest message id as the account
        sees it. Everything after either belongs to this turn, which is what
        makes this a deadline on the notice landing rather than a window a
        message is watched in.

        **Two things bound claim 1, and neither is recency.** The engine-log mark
        keeps out what is older than the turn; the address keeps out what is
        newer but somebody else's. One engine bridges every Session on the
        machine, and the newest send line was another lane's boot-turn Stop —
        written 0.16 s before this lane's own turn was typed (#355, run
        `20260910T191643Z`).

        The address is read **once**, before the turn, and it is the engine's own
        row that gives it — the same renderer the engine writes the send line
        with (`identity.address_of`), so the two cannot disagree about the
        *format*. They can still disagree about the value in one case: a Codex
        row matched by pid alone (§2 item 1) gains its `session_id` later, and a
        row read before that names the Session with the id half empty. Re-reading
        the row every half-second would be a `bridgectl` call per poll, so this
        reads once and the failure clause below names every address the engine
        did send about — the newly named one among them.
        """
        chat = self.the_chat()
        address = address_of(self.row())
        if not address:
            # Unreachable through `roster`, which fails the lane for a row that
            # is no target at all (§2 item 1) — and refused here anyway, because
            # an empty address is the one value that would match every send the
            # engine made about nobody.
            raise ItemFailed(
                f"the {self.lane.name} engine's row for this Session carries no address, so no "
                f"send line can be matched to it"
            )
        mark = self.mark()
        chat_mark = chat.mark()
        with self.journal.turn(ACKNOWLEDGE_TURN, lane=self.lane.name):
            self.session.submit(ACKNOWLEDGE_WORDS)
            try:
                own = self.waiting(
                    "TURN_SECONDS",
                    lambda: self.sent_since_for(mark, address),
                    what=(
                        f"the acknowledge turn's Stop Notice for {address} reaching the "
                        f"{self.lane.name} chat"
                    ),
                )
            except deadlines.DeadlineExpired as unsent:
                # The sends that *did* happen in that time, named on the way out:
                # a notice addressed elsewhere is the one failure whose cause is
                # invisible from everything else the row carries.
                raise deadlines.DeadlineExpired(
                    f"{unsent.what}{addressed_elsewhere(self.sent_since(mark), address)}",
                    deadline=unsent.deadline,
                    seconds=unsent.seconds,
                ) from None
        # The **first** own-addressed send after the mark, ids and all — taken
        # even when it carries none, because a send that reached nobody is the
        # engine's own record of a failure with a reason, where skipping it would
        # spend the whole deadline and report a silence.
        issued = message_ids(own[0])
        # Read **once**, with no deadline of its own: the engine writes that line
        # *after* the Bot API returned, so the message is already on the server
        # and a window watched for it would be the waiting ADR 0021 §10 forbids.
        arrived = chat.arrived(chat_mark)
        unsound = not_the_stop_notice(issued, arrived, sent=self.sent_since(mark))
        if unsound is not None:
            raise ItemFailed(unsound)
        # The **first** message, which is the oldest: the client hands the
        # arrivals over in the order they were sent (`in_arrival_order`), and the
        # product registers the Anchor against every id of a split send
        # (`core/bridge.py`'s `anchors.register`), so the first resolves as any
        # other would.
        self.notice_anchor = arrived[0].id
        kind = self.waiting(
            "ROSTER_SECONDS",
            lambda: stopped_on(self.row()),
            what=f"the {self.lane.name} engine saying what the Session stopped on",
        )
        return self.journal(
            "stop.notice",
            lane=self.lane.name,
            # The address the send line had to carry for this to be this turn's
            # Stop at all (#355) — evidence, so a green row says whose notice it
            # read and not only which message.
            target=address,
            # The ids the engine issued, carried because they are what the chat
            # read was bounded by — and never read back by (#354).
            message_ids=list(issued),
            chat_message_ids=[message.id for message in arrived],
            waiting_for=kind,
            chat=[message.as_journal_fields() for message in arrived],
        )

    def relay(self) -> str:
        """§2 item 3 — the relay turn's receipt is a proven delivery.

        The instruction is sent to the address on this lane's own roster row, and
        the receipt is graded as **fields**: `grade` literally `delivered`, never
        a substring (#71). What that instruction then does — the permission it
        raises and the file it leaves — is `approval`'s to read: the two items
        are one turn from two ends (§2).
        """
        target = address_of(self.row())
        written = self.relay_file()
        # The relay turn's **first half**: the instruction and the receipt for
        # it. Its second half — the permission and the file — is `approval`'s,
        # and is journalled under the same turn name, because §2 reads one turn
        # from two ends and a reader adding the two halves gets the turn.
        with self.journal.turn(
            RELAY_TURN, lane=self.lane.name, half="instruction", path=str(written)
        ):
            answer = self.bridgectl(
                "relay",
                target,
                write_words(written, RELAY_WORD),
                deadline="RELAY_RECEIPT_SECONDS",
            )
        self.proven(answer, f"`relay {target} …`")
        return self.journal(
            "relay.receipt",
            lane=self.lane.name,
            target=target,
            receipt=answer.text,
            path=str(written),
        )

    def approval(self) -> str:
        """§2 item 4 — the relay turn's permission, answered, and the file it leaves.

        The permission is read from `waiting_for.approval_id` on the **payload's**
        roster row and never from `state` (#191), answered with the product's own
        verb, and the answer's receipt is graded as fields exactly as the relay's
        was: the Approval Relay's contract is that the outcome is the receipt the
        verb returns, and the reply belongs to the call that sent the id — so
        there is no id to correlate afterwards.

        The push of that permission to the chat is **not** read (§2): Telegram is
        proved by one message out and one in, and nothing more.
        """
        written = self.relay_file()
        with self.journal.turn(
            RELAY_TURN, lane=self.lane.name, half="permission", path=str(written)
        ):
            identifier = self.waiting(
                "TURN_SECONDS",
                lambda: approval_id(self.row()),
                what=(
                    f"the permission the relay turn raises reaching the {self.lane.name} roster row"
                ),
            )
            # Remembered before it is sent, so the arrangement inside a later
            # turn cannot answer this same permission a second time if the row
            # still carries it for a poll or two.
            self.arranged.add(identifier)
            answer = self.bridgectl("approve", identifier, ALLOW, deadline="RELAY_RECEIPT_SECONDS")
            self.proven(answer, f"`approve {identifier} {ALLOW}`")
            self.waiting(
                "TURN_SECONDS",
                lambda: wrote(written, RELAY_WORD),
                what=f"the relay turn's write of {written} carrying {RELAY_WORD}",
            )
        return self.journal(
            "approval.answered",
            lane=self.lane.name,
            approval_id=identifier,
            verdict=ALLOW,
            receipt=answer.text,
            path=str(written),
        )

    def answer_any_permission(self) -> None:
        """Answer a permission standing in front of the inbound turn's file — as arrangement.

        **The one place this harness answers a permission it does not grade**
        (ruling on #353). §3's inbound turn is a write like the relay turn's, and
        the Claude lane keeps the product's own permission mode (#60), so the
        same mode that makes `approval` observable here makes item 5's file wait
        behind a dialog nobody answers. The spec grades item 5 by the file alone
        and names no permission (§2 item 5), so the permission is arranged away
        rather than graded: read exactly as `approval` reads it — the payload's
        `waiting_for.approval_id`, never `state` — answered with the product's
        own verb, and journalled as setup for the item.

        Nothing here is a claim: no permission is the ordinary answer on the
        Codex lane, whose sandbox allows a write inside the workspace and asks
        nothing, and a roster this walk cannot read at this instant is not this
        item's cause.
        """
        try:
            identifier = approval_id(self.row())
        except ItemFailed:
            return
        if identifier is None or identifier in self.arranged:
            return
        # Retired whatever the engine answers, so a permission this run cannot
        # answer is asked about once rather than every half-second — and the
        # refusal is on the line below, so the deadline this item then hits has
        # the reason beside it rather than a silence.
        self.arranged.add(identifier)
        answer = self.bridgectl("approve", identifier, ALLOW, deadline="RELAY_RECEIPT_SECONDS")
        self.journal(
            "approval.arranged",
            lane=self.lane.name,
            approval_id=identifier,
            verdict=ALLOW,
            accepted=answer.ok,
            reply=answer.text,
            setup_for=str(items.Item.COMPANION_INBOUND),
        )

    def companion_inbound(self) -> str:
        """§2 item 5 — plain words from the chat become a delivered relay.

        Sent as a **reply** anchored to this Session's own Stop Notice (ADR 0021
        §2–§3), which is why `stop notice` is this item's ground: the anchor is
        that item's reading — the **account-side** id of the first message it
        read, because that is the id Telethon's `reply_to` takes (#354). The
        product registers the Anchor against every id of a split send
        (`core/bridge.py`'s `anchors.register`), so the first resolves exactly as
        any other would. `@<name>:` addressing is retired (ADR 0024) and this
        harness has no way to send it — `Chat` has no `send`.

        Graded by the file alone. No engine-log line is read: the file is the one
        binary signal, and the old inbound log-line check is dropped as
        superfluous (#48).
        """
        chat = self.the_chat()
        if self.notice_anchor is None:
            raise LaneBlocked(
                "this lane has no Stop Notice message to anchor an inbound reply to, and the "
                "reply anchor is what `stop notice` arranges for this item (§6)"
            )
        written = inbound_target_file(self.workspace)

        def settled() -> bool:
            """The file, and — on the way — any permission standing in front of it."""
            if wrote(written, INBOUND_WORD):
                return True
            self.answer_any_permission()
            return wrote(written, INBOUND_WORD)

        with self.journal.turn(INBOUND_TURN, lane=self.lane.name, path=str(written)):
            sent = chat.reply(self.notice_anchor, write_words(written, INBOUND_WORD))
            self.waiting(
                "REPLY_SECONDS",
                settled,
                what=f"the inbound turn's write of {written} carrying {INBOUND_WORD}",
            )
        return self.journal(
            "companion.inbound",
            lane=self.lane.name,
            anchor=self.notice_anchor,
            path=str(written),
            chat=sent.as_journal_fields(),
        )
