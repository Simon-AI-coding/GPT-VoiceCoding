#!/usr/bin/env python3
"""Where does one spoken instruction get reworked between the microphone and a Relay? (#288)

The route #283 walks needs one thing it does not have: a picture of what the
user's sentence actually looks like at each hand of the chain. The engine log
shows the ends and none of the middle, and the middle is where the question
lives — the Voice authors `<input>` and may rewrite it (#284), the backend alone
decides when a turn ended and may delegate the same words twice (#285), and what
`bridgectl relay` is finally asked to carry is a third wording again.

This script dials a real Live Call with the **shipping** instruction catalogue,
lets a person talk into their own microphone for as long as they like, and then
writes the chain out turn by turn:

    1. what the ASR heard        — `thread/realtime/transcript/done`, role user
    2. what the Voice said       — the same notification, role assistant
    3. what the Call Agent got   — `<input>` and `<transcript_delta>`, read out
                                   of the matching rollout under `~/.codex/sessions/`
    4. what a Relay was asked to carry — the stand-in `bridgectl`'s own log

The timeline exists so a person can see how the instruction travelled and was
reworked at each step. That is its whole purpose, and it is why the layout is
one block per step stacked down the page rather than a wide table: the text
under comparison is whole sentences, often Chinese, and a table folds them into
noise at exactly the moment they need reading side by side. The index table at
the top is for finding a turn, not for reading one.

**This is a probe, not a test.** It asserts nothing, spends real API, and opens
a real microphone. A person reads the record.

**Run it from your own terminal.** The macOS microphone grant attaches to the
process that asks, so a call started from an agent or an IDE is silently muted
and every ASR line comes back empty.

    .venv/bin/python scripts/realtime_instruction_tracer.py

Speak. Press Return when you are done, and the call hangs up and the timeline is
written beside the JSONL.

**The instruction set is a switch**, so a later variant is measured on this same
script and against this same record:

    --instructions shipping        the catalogue this installation ships (default)
    --voice-prompt FILE            replace the Voice half with a file's text
    --agent-instructions FILE      replace the Call Agent half with a file's text

Whatever is sent is written into the record whole, so two runs can be compared
without trusting anyone's memory of what was in them.

**`bridgectl` is replaced by a stand-in** (the #179 device), and the stand-in is
a forwarder rather than a wall: every read — `brief`, `history`, `live` — is
passed to the real control plane, so the Call Agent chooses among the Sessions
that are really on this machine and nothing about the roster is invented or
frozen into this file. Only `relay` and `approve` are stopped: those are the two
that would put words into somebody's Session, which a probe has no business
doing. They are logged, answered with the shipping receipt's own success line so
the Call Agent's next move is unchanged, and go no further. **Every line the
timeline shows under "Relay asked for" was logged and not delivered.**

Requires the voice extra: `.venv/bin/pip install -e '.[voice]'`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The Recorder, the Call and the transport plumbing are #286's, imported rather
# than copied. That probe copied *its* base because the base did not import at
# all; this one does, and a third hand-rolled Recorder would be one more clock
# to keep in step with the other two.
from realtime_barge_in_probe import Call, Recorder, _console  # noqa: E402

from gpt_voicecoding import __version__  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.adapter import (  # noqa: E402
    APPROVAL_POLICY,
    CODEX_RESPONSE_ITEM_PREFIX,
    CODEX_RESPONSES_AS_ITEMS,
    DELEGATION_ACK_FILLER,
    DEVELOPER_ROLE,
    INCLUDE_STARTUP_CONTEXT,
    OUTPUT_MODALITY,
    REALTIME_VERSION,
    SANDBOX,
)
from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.webrtc import webrtc_transport  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.process import OwnedAppServer  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings  # noqa: E402
from gpt_voicecoding.config import default_socket_path  # noqa: E402
from gpt_voicecoding.control_plane.commands import (  # noqa: E402
    CommandError,
    build_request,
    format_address,
)
from gpt_voicecoding.core.instructions import generate  # noqa: E402
from gpt_voicecoding.core.instructions.context import (  # noqa: E402
    ControlPlaneCli,
    InstructionContext,
)
from gpt_voicecoding.core.lifecycle import Lifecycle, RelayReason  # noqa: E402
from gpt_voicecoding.core.relays import receipt_line  # noqa: E402
from gpt_voicecoding.seams.call import SpokenRosterBrief  # noqa: E402
from gpt_voicecoding.seams.delivery import Delivery  # noqa: E402

#: Where a run's working files go — the stand-in and its log. Outside the repo:
#: they are regenerated every run. The JSONL and the timeline are the evidence
#: and they go under `--out`.
PROBE_DIR = Path("/tmp/gptvc-probe288")

#: Where codex writes the Call Agent's own record. The `<input>` and the
#: `<transcript_delta>` exist nowhere else: they are what the backend composed
#: and handed over, and no notification on our side carries them.
SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

#: Audio deltas arrive per 20 ms and would bury the console. The file keeps them.
QUIET_ON_CONSOLE = ("thread/realtime/outputAudioDelta",)

#: The two verbs that would reach somebody's real Session. Stopped at the
#: stand-in; everything else is forwarded to the real control plane.
STOPPED_ACTIONS = ("relay", "approve")

#: How far either side of the call's own span a rollout may start and still be
#: this call's. The thread is started seconds before the dial, and a rollout's
#: first write can trail its session's start.
ROLLOUT_WINDOW = timedelta(minutes=5)

#: The delegation envelope, as it appears in the rollout's user messages.
DELEGATION = re.compile(
    r"<realtime_delegation>\s*"
    r"(?:<input>(?P<input>.*?)</input>\s*)?"
    r"(?:<transcript_delta>(?P<delta>.*?)</transcript_delta>\s*)?"
    r"</realtime_delegation>",
    re.DOTALL,
)


# ----------------------------------------------------------------------
# The record. One clock for three streams.
# ----------------------------------------------------------------------


class TracingRecorder(Recorder):
    """#286's Recorder with a wall clock beside its monotonic one.

    The three streams this script has to merge — our notifications, codex's
    rollout and the stand-in's log — are written by three processes, and only
    two of them can be told about a dial. UTC is the axis they already share, so
    every record carries it and the merge is a sort rather than a guess.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.dialled_at = datetime.now(UTC)

    def heard(self, message: dict[str, Any]) -> None:
        """Every notification, not only the realtime ones.

        #286's Recorder drops anything outside `thread/realtime/`, which is
        right for a probe asking one question about one subtree and wrong here:
        this script is the record of what the whole chain did, and the Call
        Agent's own half of it — `item/completed`, `turn/completed` — is
        outside that prefix. A tracer that cannot say what it did not see is
        worse than a verbose one.
        """
        method = message.get("method")
        if not isinstance(method, str):
            return
        record = {"at": self._offset(), "method": method, "params": message.get("params")}
        self.events.append(record)
        self._write(record)
        if method not in QUIET_ON_CONSOLE:
            print(f"  {record['at']:7.3f}  {_console(method, message.get('params'))}", flush=True)
        self._settle(method, message.get("params"))

    def _write(self, record: dict[str, Any]) -> None:
        # The same dict object is in `events` or `notes`, so stamping it here
        # stamps it in memory too, and the timeline reads one thing.
        record["utc"] = (self.dialled_at + timedelta(seconds=float(record["at"]))).isoformat()
        super()._write(record)

    def at_utc(self, offset: float) -> datetime:
        return self.dialled_at + timedelta(seconds=offset)


# ----------------------------------------------------------------------
# What the call is opened on.
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prose:
    """The two instruction sets one run sends, and where each came from.

    The provenance travels with the text because the switch is the point: a
    record that says only what was sent cannot be read against a record from a
    different variant without somebody remembering which was which.
    """

    voice: str
    agent: str
    voice_from: str
    agent_from: str


def prose_for(arguments: argparse.Namespace, cli: ControlPlaneCli) -> Prose:
    """The shipping catalogue, with either half replaced from a file on request.

    The catalogue is generated rather than pasted, so the Voice prompt and the
    Call Agent rules this sends are the ones this installation would send on a
    real dial — including the CLI path and socket the Call Agent is told to run,
    which is how the stand-in gets used without touching PATH.
    """
    instructions = generate(InstructionContext(cli=cli))
    voice, voice_from = instructions.voice.text, f"shipping catalogue {__version__}"
    agent, agent_from = instructions.agent.text, f"shipping catalogue {__version__}"

    if arguments.voice_prompt:
        path = Path(arguments.voice_prompt).expanduser().resolve()
        voice, voice_from = path.read_text(encoding="utf-8"), str(path)
    if arguments.agent_instructions:
        path = Path(arguments.agent_instructions).expanduser().resolve()
        agent, agent_from = path.read_text(encoding="utf-8"), str(path)

    if not voice.strip() or not agent.strip():
        raise SystemExit("both halves have to carry prose; one of them is empty")
    return Prose(voice=voice, agent=agent, voice_from=voice_from, agent_from=agent_from)


def hand_over(
    bridgectl: Path, socket_path: Path, recorder: TracingRecorder
) -> tuple[list[dict[str, Any]], str]:
    """The Roster Brief the engine would open a call on, read from the live engine.

    A real dial carries the roster in `initialItems`, and the Voice's own rules
    lean on it: *"you were handed the sessions and what each one is waiting on
    when this call opened, so answer from that"*. A call opened without it has a
    Voice that answers the commonest question of all out of nothing, and a trace
    of that is a trace of the wrong thing.

    The words are the engine's own — `brief` with no address is the Roster
    Brief, its first line the counts and the rest one header row per Session —
    so nothing here is composed and nothing is frozen into this file. No engine
    running means no hand-over, said out loud rather than invented.
    """
    read = subprocess.run(  # noqa: S603
        [str(bridgectl), "--socket", str(socket_path), "brief"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in read.stdout.splitlines() if line.strip()]
    if read.returncode != 0 or not lines:
        recorder.note(
            "no hand-over: the engine did not answer `brief`",
            {"returncode": read.returncode, "stderr": read.stderr.strip()[:400]},
        )
        return [], f"none — `brief` failed (rc={read.returncode})"

    brief = SpokenRosterBrief(counts=lines[0], rows=tuple(line.strip() for line in lines[1:]))
    recorder.note(
        "hand-over read from the live engine", {"counts": brief.counts, "rows": brief.rows}
    )
    return (
        [
            {
                "role": DEVELOPER_ROLE,
                "text": "\n".join([brief.counts, *(f"  {row}" for row in brief.rows)]),
            }
        ],
        f"{len(brief.rows)} Sessions, read from the live engine",
    )


class TracingCall(Call):
    """#286's Call, dialled with every field the engine's own dial carries.

    #286's `dial` sends five fields and the adapter sends twelve. #287 measured
    that gap and found it matters — `codexResponseHandoffMode` aside, the two
    that shape a hand-off are `codexResponsesAsItems` and its prefix — and a
    probe that dials differently from the engine is measuring a call nobody
    makes. So the parameters are built here from the adapter's own constants,
    imported rather than retyped, and the only things this script chooses are
    the two instruction sets and the hand-over.
    """

    async def dial(self, *, prose: Prose, hand_over: list[dict[str, Any]]) -> None:  # type: ignore[override]
        started = await self._request(
            "thread/start",
            {"cwd": str(self._settings.cwd), "approvalPolicy": APPROVAL_POLICY, "sandbox": SANDBOX},
        )
        thread = started.get("thread")
        self.thread_id = (thread or {}).get("id") if isinstance(thread, dict) else None
        if not isinstance(self.thread_id, str):
            raise RuntimeError(f"thread/start named no thread: {started}")
        self._recorder.note("thread started", self.thread_id)

        offer = await self._transport.offer()
        parameters: dict[str, Any] = {
            "threadId": self.thread_id,
            "version": REALTIME_VERSION,
            "model": self._settings.realtime_model,
            "outputModality": OUTPUT_MODALITY,
            "transport": {"type": "webrtc", "sdp": offer},
            "prompt": prose.voice,
            "realtimeStartInstructions": prose.agent,
            "initialItems": hand_over,
            "delegationAckFiller": DELEGATION_ACK_FILLER,
            "codexResponsesAsItems": CODEX_RESPONSES_AS_ITEMS,
            "codexResponseItemPrefix": CODEX_RESPONSE_ITEM_PREFIX,
            "includeStartupContext": INCLUDE_STARTUP_CONTEXT,
        }
        # The prose is in the record already, whole and once. Repeating it here
        # would put five kilobytes into every dial line and hide the fields.
        self._recorder.note(
            "dialling",
            {
                key: value
                for key, value in parameters.items()
                if key not in ("transport", "prompt", "realtimeStartInstructions")
            },
        )
        await self._request("thread/realtime/start", parameters)

        deadline = self._settings.connect_timeout_seconds
        assert self._recorder.sdp is not None and self._recorder.started is not None
        answer = await asyncio.wait_for(self._recorder.sdp, deadline)
        await self._transport.accept_answer(answer)
        await asyncio.wait_for(self._recorder.started, deadline)
        await self._transport.wait_connected(deadline)
        self._recorder.note("call is up")


# ----------------------------------------------------------------------
# The stand-in.
# ----------------------------------------------------------------------

#: The stand-in's whole source. Written out rather than shipped as a file
#: because it is generated per run — it has to know this run's log path and this
#: run's real `bridgectl`, and a file on disk that remembered the last run's
#: paths is the one way this device can lie.
STAND_IN = '''#!/usr/bin/env python3
"""A `bridgectl` that forwards every read and stops the two verbs that write.

Written by scripts/realtime_instruction_tracer.py. Regenerated every run.
"""
import json
import subprocess
import sys
from datetime import UTC, datetime

LOG = {log!r}
REAL = {real!r}
STOPPED = {stopped!r}
RECEIPT = {receipt!r}


def action(argv):
    """The first word that is not a flag or a flag's value. See the generated rules."""
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word == "--socket":
            skip = True
            continue
        if word.startswith("-"):
            continue
        return word
    return ""


def note(record):
    record["utc"] = datetime.now(UTC).isoformat()
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\\n")


def main():
    argv = sys.argv[1:]
    verb = action(argv)
    if verb in STOPPED:
        note({{"argv": argv, "action": verb, "stopped": True, "answered": RECEIPT}})
        print(RECEIPT)
        return 0
    ran = subprocess.run([REAL, *argv], capture_output=True, text=True, check=False)
    note(
        {{
            "argv": argv,
            "action": verb,
            "stopped": False,
            "returncode": ran.returncode,
            "stdout": ran.stdout,
            "stderr": ran.stderr,
        }}
    )
    sys.stdout.write(ran.stdout)
    sys.stderr.write(ran.stderr)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_stand_in(*, real: Path, log: Path) -> Path:
    """Write this run's `bridgectl` and hand back the path the rules will name.

    The success line is the shipping one, built from the shipping enums rather
    than typed out: a receipt the Call Agent has never seen before is a reason
    for it to behave differently, and this device exists to change nothing about
    what it decides.
    """
    receipt = receipt_line(
        state=str(Lifecycle.DELIVERED),
        grade=str(Delivery.DELIVERED),
        reason=str(RelayReason.DELIVERED),
    )
    stand_in = PROBE_DIR / "bin" / "bridgectl"
    stand_in.parent.mkdir(parents=True, exist_ok=True)
    stand_in.write_text(
        STAND_IN.format(log=str(log), real=str(real), stopped=STOPPED_ACTIONS, receipt=receipt),
        encoding="utf-8",
    )
    stand_in.chmod(0o755)
    return stand_in


def real_bridgectl(stated: str | None) -> Path:
    """The control-plane CLI this installation ships, found the way the engine finds it.

    Same derivation as `engine/composition.py:_instruction_context`: the console
    script beside this interpreter, used only after it is found to be runnable.
    A stated one wins, because a bundle moves the binary.
    """
    found = (
        Path(stated).expanduser().resolve() if stated else Path(sys.executable).parent / "bridgectl"
    )
    if not (found.is_file() and os.access(found, os.X_OK)):
        raise SystemExit(
            f"no control-plane CLI to forward to: {found} is not there or cannot be run"
        )
    return found


# ----------------------------------------------------------------------
# Reading codex's own record back.
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandOff:
    """One `<realtime_delegation>` as the Call Agent received it.

    `<transcript_delta>` is codex's own accumulator since the last hand-off
    (#284), written as `role: text` lines. Its **last user line is the utterance
    this hand-off is about** — and that is what makes the timeline align, where
    time cannot. The first run of this script paired every turn with the wrong
    sentence because it hung speech on the nearest hand-off by clock: the
    backend delegates on end-of-speech, but `transcript/done` lands *after*
    that, so on the 23:56 call the hand-off at +19.6 s belonged to an utterance
    whose transcript did not close until +24.8 s. The envelope says which
    sentence it carries; nothing has to be inferred from a timestamp.
    """

    at: datetime
    ordinal: int
    given: str
    delta: str

    @property
    def about(self) -> str:
        """The user's newest line inside the delta — the sentence being handed over."""
        spoken = [text for role, text in self.delta_lines if role == "user"]
        return spoken[-1] if spoken else ""

    @property
    def delta_lines(self) -> tuple[tuple[str, str], ...]:
        """The delta as `(role, text)`, with a wrapped line kept on its own entry."""
        lines: list[list[str]] = []
        for raw in self.delta.splitlines():
            role, sep, rest = raw.partition(": ")
            if sep and role.strip() in ("user", "assistant"):
                lines.append([role.strip(), rest])
            elif lines:
                lines[-1][1] += "\n" + raw
        return tuple((role, text) for role, text in lines)


@dataclass(frozen=True, slots=True)
class Ran:
    """One command the Call Agent ran, and what came back.

    A second, independent witness to step 4. The stand-in's log says what
    `bridgectl` was asked for; this says what the Call Agent believes it asked
    for and what it read as the answer — and the two disagreeing is exactly the
    kind of thing this timeline exists to make visible.

    **Two shapes, because codex has two.** A tool call is a `function_call` on
    some versions and a `custom_tool_call` on others (0.153.4 writes the second,
    with the command inside a JavaScript `exec` call). Reading only the first
    found nothing at all on a rollout with six commands in it.
    """

    at: datetime
    command: str
    output: str


@dataclass(frozen=True, slots=True)
class Rollout:
    """The Call Agent's record for this call, and how it was recognised as this call's."""

    path: Path
    matched_by: str
    hand_offs: tuple[HandOff, ...]
    ran: tuple[Ran, ...]


def find_rollout(thread_id: str | None, span: tuple[datetime, datetime]) -> Rollout | None:
    """The rollout this call's Call Agent wrote, by identity first and time second.

    The thread id is the strong answer and it is tried first. The window is the
    weak one and it says so in `matched_by`, because a timeline that silently
    read the wrong call's hand-offs would be worse than one that has none.
    """
    opened, closed = span[0] - ROLLOUT_WINDOW, span[1] + ROLLOUT_WINDOW
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        meta = _first_record(path)
        if meta is None or meta.get("type") != "session_meta":
            continue
        payload = meta.get("payload") or {}
        started = _utc(meta.get("timestamp"))
        if started is None or not (opened <= started <= closed):
            continue
        candidates.append((started, path, payload))

    for _, path, payload in candidates:
        if thread_id and payload.get("session_id") == thread_id:
            return _read_rollout(path, "session_id == threadId")
    # The real engine may be up and dialling calls of its own, so "a
    # gpt-voicecoding rollout from the right minute" is not by itself this
    # call's. A rollout with no hand-off in it delegated nothing and cannot be
    # the record of a call somebody spoke into, so it is dropped before the
    # window is asked to break a tie it cannot break well.
    ours = [
        _read_rollout(path, "")
        for _, path, payload in candidates
        if (payload.get("originator") or "") == "gpt-voicecoding"
    ]
    spoke = [rollout for rollout in ours if rollout.hand_offs]
    if len(spoke) == 1:
        return replace(spoke[0], matched_by="the only rollout with a hand-off in the call's window")
    if spoke:
        newest = max(spoke, key=lambda rollout: rollout.hand_offs[-1].at)
        return replace(
            newest,
            matched_by=(
                f"newest of {len(spoke)} rollouts with hand-offs in the window — "
                "check the path before trusting steps 3 and 4"
            ),
        )
    return None


def _first_record(path: Path) -> dict[str, Any] | None:
    with contextlib.suppress(OSError, ValueError):
        with path.open(encoding="utf-8") as handle:
            line = handle.readline()
        if line.strip():
            loaded = json.loads(line)
            return loaded if isinstance(loaded, dict) else None
    return None


def _read_rollout(path: Path, matched_by: str) -> Rollout:
    hand_offs: list[HandOff] = []
    calls: dict[str, tuple[datetime, str]] = {}
    outputs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record: Any = None
        with contextlib.suppress(ValueError):
            record = json.loads(line)
        if not isinstance(record, dict) or record.get("type") != "response_item":
            continue
        at = _utc(record.get("timestamp"))
        payload = record.get("payload") or {}
        if at is None or not isinstance(payload, dict):
            continue

        text = _message_text(payload)
        if text:
            found = DELEGATION.search(text)
            if found:
                hand_offs.append(
                    HandOff(
                        at=at,
                        ordinal=len(hand_offs) + 1,
                        given=(found.group("input") or "").strip(),
                        delta=(found.group("delta") or "").strip(),
                    )
                )

        kind = payload.get("type")
        call_id = str(payload.get("call_id", "") or payload.get("id", ""))
        if kind in ("function_call", "custom_tool_call"):
            asked = payload.get("arguments") if kind == "function_call" else payload.get("input")
            calls[call_id] = (at, str(asked or ""))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            outputs[call_id] = _output_text(payload.get("output"))

    ran = tuple(
        Ran(at=at, command=command, output=outputs.get(call_id, ""))
        for call_id, (at, command) in sorted(calls.items(), key=lambda row: row[1][0])
    )
    return Rollout(path=path, matched_by=matched_by, hand_offs=tuple(hand_offs), ran=ran)


def _output_text(output: object) -> str:
    """What a tool call returned, whichever of the two shapes it came back in."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in output
            if isinstance(part, dict) and part.get("text")
        )
    return ""


def _message_text(payload: dict[str, Any]) -> str:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text")
    )


def _utc(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
    return None


def stand_in_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            with contextlib.suppress(ValueError):
                rows.append(json.loads(line))
    return rows


# ----------------------------------------------------------------------
# The timeline. The thing this script exists to produce.
# ----------------------------------------------------------------------


@dataclass(slots=True)
class Turn:
    """One hand-off and the speech it carries. The unit the timeline reads in.

    The hand-off is the spine because it is the only event all three streams
    agree on, and the speech is hung on it **by content** rather than by clock:
    the envelope names the sentence it carries, so there is nothing to infer.
    """

    hand_off: HandOff
    heard: list[tuple[datetime, str]] = field(default_factory=list)
    said: list[tuple[datetime, str]] = field(default_factory=list)
    relays: list[dict[str, Any]] = field(default_factory=list)
    duplicate_of: int | None = None


def _same(left: str, right: str) -> bool:
    """Two renderings of one sentence, ignoring only how they were spaced."""
    return " ".join(left.split()) == " ".join(right.split())


def turns_of(
    recorder: TracingRecorder, rollout: Rollout, relays: list[dict[str, Any]]
) -> tuple[list[Turn], list[tuple[datetime, str]]]:
    """The turns, and the speech no hand-off ever claimed.

    The second return value is not an error path. An utterance the backend
    answered without delegating — *"好了好了,你在吗"* on the 23:56 call — is a
    real event of the call and belongs in the record; hanging it on the next
    hand-off, which is what the first version did, put words in a turn that
    never carried them.
    """
    built = [Turn(hand_off=hand_off) for hand_off in rollout.hand_offs]
    finals = _finals(recorder)
    claimed: set[int] = set()

    seen: dict[str, int] = {}
    for turn in built:
        about = turn.hand_off.about
        key = " ".join(f"{about}|{turn.hand_off.given}".split())
        if key in seen:
            turn.duplicate_of = seen[key]
        else:
            seen[key] = turn.hand_off.ordinal

        for index, (at, role, text) in enumerate(finals):
            if role == "user" and about and _same(text, about):
                turn.heard.append((at, text))
                claimed.add(index)

    # The Voice's own speech has no envelope naming it, so it is cut by clock —
    # which is sound for it: what the Voice said before a hand-off is what it
    # said, and nothing downstream is being aligned to it.
    for at, role, text in finals:
        if role != "user":
            later = [turn for turn in built if turn.hand_off.at >= at]
            (later[0] if later else built[-1]).said.append((at, text))

    for row in relays:
        at = _utc(row.get("utc"))
        if at is not None and row.get("stopped"):
            earlier = [turn for turn in built if turn.hand_off.at <= at]
            (earlier[-1] if earlier else built[0]).relays.append(row)

    unclaimed = [
        (at, text)
        for index, (at, role, text) in enumerate(finals)
        if role == "user" and index not in claimed
    ]
    return built, unclaimed


def _finals(recorder: TracingRecorder) -> list[tuple[datetime, str, str]]:
    """Every finished transcript line, both roles, on the shared clock."""
    lines = []
    for event in recorder.events:
        params = event.get("params")
        if event.get("method") != "thread/realtime/transcript/done" or not isinstance(params, dict):
            continue
        text = str(params.get("text", "")).strip()
        if text:
            lines.append((recorder.at_utc(float(event["at"])), str(params.get("role", "?")), text))
    return lines


@dataclass(frozen=True, slots=True)
class Seen:
    """One hand-off as **our own** notifications saw it, before codex wrote it down.

    `thread/realtime/itemAdded` carries a `handoff_request` item holding
    `input_transcript` — the sentence the Voice authored — and
    `active_transcript`, the same role/text entries that reach the Call Agent as
    `<transcript_delta>`. So the whole of step 3 is on this side of the wire,
    on this script's own clock, keyed by `handoff_id`.

    The first version looked for `active_transcript` among the *top-level* keys
    of every notification's params and found nothing, then printed "Not once in
    this run" onto a page whose record held two of them. It is nested one level
    down, inside `item`.

    #287 said this field arrives on every hand-off and is read by nothing. Two
    hand-offs, two items: it still does, and still is.
    """

    at: datetime
    handoff_id: str
    given: str
    entries: tuple[tuple[str, str], ...]


def hand_offs_seen(recorder: TracingRecorder) -> list[Seen]:
    """Every `handoff_request` this call's notifications carried, in order."""
    seen = []
    for event in recorder.events:
        params = event.get("params")
        if not isinstance(params, dict):
            continue
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "handoff_request":
            continue
        entries = item.get("active_transcript")
        seen.append(
            Seen(
                at=recorder.at_utc(float(event["at"])),
                handoff_id=str(item.get("handoff_id", "")),
                given=str(item.get("input_transcript", "")),
                entries=tuple(
                    (str(row.get("role", "")), str(row.get("text", "")))
                    for row in (entries if isinstance(entries, list) else [])
                    if isinstance(row, dict)
                ),
            )
        )
    return seen


@dataclass(frozen=True, slots=True)
class Ask:
    """One stopped `relay`, read back with the engine's own parser.

    The ticket asks what a Relay was asked to put into **which Session**, and
    that is two facts, not one line of argv. They are pulled apart by
    `build_request` — the same code the real `bridgectl` would have used — so
    the timeline never disagrees with the engine about what a command said.
    """

    into: str
    words: str
    route: str
    answered: str

    @property
    def summary(self) -> str:
        route = "" if self.route == "deliver" else f" ({self.route})"
        return f"into {self.into}{route}: {self.words}"


def _ask(row: dict[str, Any]) -> Ask | None:
    """One stand-in log row as the two facts, or None if it was not a `relay`."""
    argv = [str(word) for word in row.get("argv", [])]
    trimmed: list[str] = []
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word == "--socket":
            skip = True
            continue
        trimmed.append(word)
    if not trimmed or trimmed[0] != "relay":
        return None
    try:
        request = build_request(trimmed[0], trimmed[1:])
    except CommandError:
        return None
    return Ask(
        into=format_address(request.payload["target"]),  # type: ignore[arg-type]
        words=str(request.payload["text"]),
        route=str(request.payload["route"]),
        answered=str(row.get("answered", "")),
    )


def _carried(turn: Turn) -> str:
    """Did the Call Agent carry `<input>` into the Relay, or rework it on the way?

    The step the first two versions of this page never compared, and the one the
    23:11 run turned on: the Voice handed over *"你告诉它就按照,第一个方向去做就
    可以了"* unchanged, and what reached `relay` was *"就按照第一个方向去做就可以
    了"* — the Call Agent had dropped the words addressed to the Voice. That is a
    reworking of the user's instruction, by the half whose rules say it carries
    them as handed over, and a page about where the words change has to show it.
    """
    asks = [ask for ask in (_ask(row) for row in turn.relays) if ask is not None]
    if not asks:
        return ""
    if all(_same(ask.words, turn.hand_off.given) for ask in asks):
        return "carried whole"
    return "**reworked by the Call Agent**"


def _block(label: str, body: str, *, empty: str) -> list[str]:
    """One labelled step of the chain. Quoted, so a sentence stays a sentence."""
    if not body.strip():
        return [f"**{label}** — {empty}", ""]
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    return [f"**{label}**", "", quoted, ""]


def _clock(at: datetime) -> str:
    return at.astimezone().strftime("%H:%M:%S")


def _short(text: str, width: int = 28) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _reworking(hand_off: HandOff) -> str:
    """Did the Voice rework the sentence on its way into `<input>`?

    Both sides of this comparison come out of the **same** envelope: the delta's
    newest user line is the ASR as codex accumulated it, and `<input>` is what
    the Voice authored from it (#284). That is the shaping question #289 asks,
    and it is answerable without our notifications at all.

    The first version compared `<input>` against *our* `transcript/done`
    instead. Those are two different questions — the second is ASR-versus-ASR —
    and asking the wrong one flagged `reworked` on turns where the Voice had
    changed nothing.

    Whitespace-insensitive and nothing cleverer: this is a signpost telling a
    reader which turns to open, not a grader. What "unchanged content" means is
    #283's own open question, and a similarity score here would look like an
    answer to it.
    """
    if not hand_off.about:
        return "no user line in the delta"
    if not hand_off.given:
        return "no <input>"
    return "same" if _same(hand_off.given, hand_off.about) else "reworked"


def _asr_agrees(turn: Turn) -> str:
    """Did our own ASR and codex's accumulator hear the same thing? Usually moot."""
    if not turn.heard:
        return "no matching ASR line"
    return (
        "ASR agrees"
        if all(_same(text, turn.hand_off.about) for _, text in turn.heard)
        else ("ASR differs")
    )


def timeline(
    *,
    recorder: TracingRecorder,
    prose: Prose,
    rollout: Rollout | None,
    relays: list[dict[str, Any]],
    thread_id: str | None,
    roster: str,
) -> str:
    """The whole run as one page, written for somebody asking where the words changed."""
    lines = [
        "# One spoken instruction, from the microphone to a Relay",
        "",
        f"- Call dialled: {recorder.dialled_at.astimezone().isoformat()}",
        f"- Thread: `{thread_id}`",
        f"- Notifications: `{recorder.path}`",
        f"- Voice prompt: {prose.voice_from} ({len(prose.voice.encode())} bytes)",
        f"- Call Agent rules: {prose.agent_from} ({len(prose.agent.encode())} bytes)",
        f"- Hand-over at dial: {roster}",
        "",
    ]

    # The state of the control plane is stated rather than assumed. The first
    # run of this script printed "the roster is this machine's real one" onto a
    # page whose every `brief` had failed — a false sentence inside the evidence
    # is worse than a missing one.
    failed = [row for row in relays if not row.get("stopped") and row.get("returncode")]
    if failed:
        lines += [
            f"> **{len(failed)} of the Call Agent's forwarded commands failed.** The engine",
            "> did not answer, so anything the Voice said about a Session came from nowhere.",
            "> The first failure said:",
            "",
            f"> `{_short(str(failed[0].get('stderr', '')).strip(), 200)}`",
            "",
        ]
    else:
        lines += [
            "Relays below were **logged and not delivered**: the stand-in `bridgectl` stops",
            "`relay` and `approve` and forwards every other command to the real control plane,",
            "so the roster the Call Agent chose from is this machine's real one.",
            "",
        ]

    if rollout is None:
        lines += [
            "## No Call Agent record",
            "",
            "No rollout under `~/.codex/sessions/` could be matched to this call, so steps 3",
            "and 4 are missing and steps 1 and 2 are all there is. The JSONL still holds every",
            "notification.",
            "",
        ]
        return "\n".join(lines + _speech_only(recorder))

    lines += [
        f"- Call Agent record: `{rollout.path}`",
        f"- Matched by: {rollout.matched_by}",
        "",
    ]

    built, unclaimed = turns_of(recorder, rollout, relays)
    if not built:
        lines += [
            "## No hand-off",
            "",
            "The rollout holds no `<realtime_delegation>`: the backend never handed this call's",
            "speech to the Call Agent. What was heard and said is below.",
            "",
        ]
        return "\n".join(lines + _speech_only(recorder))

    lines += [
        "## Index",
        "",
        "Two comparisons, because the words can change twice. **Voice** is `<input>`",
        "against the newest user line inside the same envelope — what the Voice made of",
        "what was heard. **Agent** is what reached `relay` against that `<input>` — what",
        "the Call Agent made of what it was handed. Open any turn not marked `same`.",
        "",
        "| # | time | heard | `<input>` | Voice | relayed into | Agent |",
        "|---|---|---|---|---|---|---|",
    ]
    for turn in built:
        asks = [ask for ask in (_ask(row) for row in turn.relays) if ask is not None]
        flag = _reworking(turn.hand_off)
        if turn.duplicate_of is not None:
            flag += f" · repeat of {turn.duplicate_of}"
        lines.append(
            f"| {turn.hand_off.ordinal} | {_clock(turn.hand_off.at)} "
            f"| {_short(turn.hand_off.about) or '—'} | {_short(turn.hand_off.given) or '—'} "
            f"| {flag} | {' '.join(ask.into for ask in asks) or '—'} "
            f"| {_carried(turn) or '—'} |"
        )
    lines.append("")

    for turn in built:
        verdicts = [f"Voice: {_reworking(turn.hand_off)}"]
        if _carried(turn):
            verdicts.append(f"Agent: {_carried(turn)}")
        if turn.duplicate_of is not None:
            verdicts.append(f"same envelope as turn {turn.duplicate_of}")
        lines += [
            f"## Turn {turn.hand_off.ordinal} — hand-off at {_clock(turn.hand_off.at)}"
            f" ({'; '.join(verdicts)})",
            "",
        ]

        lines += _block(
            f"1. What the ASR heard ({_asr_agrees(turn)})",
            "\n".join(f"[{_clock(at)}] {text}" for at, text in turn.heard),
            empty=(
                "no transcript of ours matches this envelope's newest user line — "
                "the delta below is the only ASR there is for this turn"
            ),
        )
        # Labelled by the clock rather than asserted. The 09:14 turn on the
        # 23:11 run had the Voice speaking seven seconds *after* its hand-off,
        # under a heading that said "before it".
        lines += _block(
            "2. What the Voice said",
            "\n".join(
                f"[{_clock(at)}] {'(after the hand-off) ' if at > turn.hand_off.at else ''}{text}"
                for at, text in turn.said
            ),
            empty="nothing",
        )
        lines += _block(
            "3. What the Call Agent was handed — `<input>`",
            turn.hand_off.given,
            empty="the envelope carried no `<input>`",
        )
        lines += _block(
            "   …and `<transcript_delta>`",
            turn.hand_off.delta,
            empty="the envelope carried no `<transcript_delta>`",
        )
        asks = [ask for ask in (_ask(row) for row in turn.relays) if ask is not None]
        lines += _block(
            "4. What a Relay was asked to carry, and into which Session",
            "\n".join(
                f"into:  {ask.into}"
                + (f"  ({ask.route})" if ask.route != "deliver" else "")
                + f"\nwords: {ask.words}"
                + f"\n→ {ask.answered}   [logged, not delivered]"
                for ask in asks
            ),
            empty="no Relay was asked for in this turn",
        )

    lines += ["## Heard, but never handed to the Call Agent", ""]
    if unclaimed:
        lines += [
            "The backend answered these out of the Voice alone; no envelope carries them.",
            "",
        ]
        lines += [f"- [{_clock(at)}] {text}" for at, text in unclaimed]
    else:
        lines.append("Every utterance the ASR closed reached the Call Agent.")
    lines.append("")

    seen = hand_offs_seen(recorder)
    lines += [
        "## The hand-off as our own notifications saw it",
        "",
        "`thread/realtime/itemAdded` carries a `handoff_request` holding `input_transcript`",
        "and `active_transcript` — the whole of step 3, on this side of the wire and on this",
        "script's clock. The engine reads neither (#287). Read against the envelopes above:",
        "the two should agree, and a disagreement is news.",
        "",
    ]
    if not seen:
        lines.append("No `handoff_request` reached us on this call.")
    for index, item in enumerate(seen):
        envelope = rollout.hand_offs[index] if index < len(rollout.hand_offs) else None
        agrees = (
            "agrees with the envelope"
            if envelope is not None and _same(envelope.given, item.given)
            else "**differs from the envelope**"
        )
        lines += [
            f"- **[{_clock(item.at)}] `{item.handoff_id}`** — {agrees}",
            f"  - `input_transcript`: {item.given}",
            f"  - `active_transcript`: {len(item.entries)} entries, "
            f"{sum(1 for role, _ in item.entries if role == 'user')} of them the user's",
        ]
    lines.append("")

    lines += [
        "## Everything the Call Agent ran",
        "",
        "The rollout's own witness to step 4, beside the stand-in's log. What it read back",
        "is here too, because the roster it was answering from is half of why it chose the",
        "Session it chose.",
        "",
    ]
    if rollout.ran:
        for ran in rollout.ran:
            lines += [f"- **[{_clock(ran.at)}]** `{_short(ran.command, 160)}`"]
            lines += [f"  - → {line}" for line in ran.output.splitlines() if line.strip()][:8]
    else:
        lines.append("It ran nothing.")
    lines.append("")
    return "\n".join(lines)


def _speech_only(recorder: TracingRecorder) -> list[str]:
    lines = ["## What was heard and said", ""]
    lines += [f"- [{_clock(at)}] **{role}** {text}" for at, role, text in _finals(recorder)] or [
        "Nothing. If every line is missing, the microphone grant is the first thing to check: "
        "run this from your own terminal."
    ]
    return lines + [""]


# ----------------------------------------------------------------------
# The run.
# ----------------------------------------------------------------------


async def until_done(arguments: argparse.Namespace, recorder: TracingRecorder) -> None:
    """Let the person talk for as long as they like, and stop when they say so.

    Return on the keyboard is the way out, because the whole point is an
    open-ended conversation and a timer would cut somebody off mid-sentence.
    `--seconds` is the way out for a run with nobody at the keyboard, and
    Ctrl-C works everywhere; all three land in the same `finally`.
    """
    print("\n" + "=" * 72, flush=True)
    print("  SPEAK. Take as long as you like.", flush=True)
    print("  Press Return here when you are done, and the call hangs up.", flush=True)
    print("=" * 72 + "\n", flush=True)
    recorder.note("the call is yours")

    if arguments.seconds:
        await asyncio.sleep(arguments.seconds)
        recorder.note(f"--seconds {arguments.seconds} is up")
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sys.stdin.readline)
    recorder.note("Return pressed")


async def main(arguments: argparse.Namespace) -> int:
    # First, before a file is opened or a stamp is taken. Refused rather than
    # warned: the 23:56 run of 2026-09-07 was spent on a dead socket — every
    # `brief` the Call Agent ran failed, the Voice answered "its project is
    # brainstorming, its task is 想点子" out of nothing, and a live call bought
    # a record of the Voice inventing. A warning scrolls past in the middle of a
    # notification stream; a refusal does not, and it leaves no half-run behind.
    socket_path = Path(arguments.control_socket).expanduser()
    if not socket_path.exists() and not arguments.no_engine:
        raise SystemExit(
            f"no control plane at {socket_path}: start GPT-VoiceCoding.app first, or pass "
            "--no-engine to trace a call whose Call Agent can reach no Session at all"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(arguments.out).expanduser()
    recorder = TracingRecorder(out / f"{stamp}-tracer.jsonl")
    recorder.arm()
    PROBE_DIR.mkdir(parents=True, exist_ok=True)

    real = real_bridgectl(arguments.real_bridgectl)
    log = PROBE_DIR / f"{stamp}-bridgectl.jsonl"
    stand_in = install_stand_in(real=real, log=log)
    cli = ControlPlaneCli(command=stand_in, version=__version__, socket_path=socket_path)
    prose = prose_for(arguments, cli)

    recorder.note(
        "instruction sets",
        {"voice_from": prose.voice_from, "agent_from": prose.agent_from},
    )
    # Whole, not summarised: two runs are only comparable if the record says
    # exactly what each of them sent.
    recorder.note("voice prompt", prose.voice)
    recorder.note("call agent rules", prose.agent)
    recorder.note(
        "stand-in bridgectl", {"path": str(stand_in), "forwards_to": str(real), "log": str(log)}
    )

    if socket_path.exists():
        print(
            "  warn   the real engine is up and may dial a call of its own over this one;",
            flush=True,
        )
        print("  warn   flip the Duty and Voice switches off first if that matters", flush=True)

    os.environ.setdefault("RUST_LOG", "codex_core::realtime_conversation=info,codex_core=info")
    roster = "not read"
    settings = RealtimeCallSettings(workspace=Path(arguments.cwd).expanduser().resolve())
    server = OwnedAppServer(
        settings=CodexSettings(),
        socket_path=Path(arguments.socket),
        log_path=Path(arguments.server_log) if arguments.server_log else None,
        version=__version__,
    )
    server.listen(recorder.heard)
    transport = webrtc_transport(
        input_device=arguments.input, output_device=arguments.output, silent=False
    )
    call = TracingCall(server=server, recorder=recorder, settings=settings, transport=transport)

    print(f"  probe  recording to {recorder.path}")
    print(f"  probe  bridgectl stand-in at {stand_in} → {real}")
    await server.start()
    try:
        carried, roster = hand_over(real, socket_path, recorder)
        await call.dial(prose=prose, hand_over=carried)
        await until_done(arguments, recorder)
        return 0
    finally:
        print("  end    hanging up")
        with contextlib.suppress(Exception):
            await call.hang_up()
        await server.aclose()
        closed = datetime.now(UTC)
        rollout = find_rollout(call.thread_id, (recorder.dialled_at, closed))
        page = timeline(
            recorder=recorder,
            prose=prose,
            rollout=rollout,
            relays=stand_in_log(log),
            thread_id=call.thread_id,
            roster=roster,
        )
        written = recorder.path.with_name(recorder.path.stem + "-timeline.md")
        written.write_text(page, encoding="utf-8")
        recorder.close()
        print(f"  end    timeline at {written}")


def parsed() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instructions",
        choices=("shipping",),
        default="shipping",
        help="which catalogue to dial with; a variant is a file on the two flags below",
    )
    parser.add_argument("--voice-prompt", default=None, help="file replacing the Voice's prose")
    parser.add_argument(
        "--agent-instructions", default=None, help="file replacing the Call Agent's rules"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="hang up after this long instead of waiting for Return (for a run with nobody there)",
    )
    parser.add_argument(
        "--control-socket",
        default=str(default_socket_path()),
        help="the running engine's control plane, which the stand-in forwards to",
    )
    parser.add_argument(
        "--real-bridgectl", default=None, help="the control-plane CLI to forward to"
    )
    parser.add_argument(
        "--no-engine",
        action="store_true",
        help="dial anyway with no control plane; the Call Agent will reach no Session",
    )
    parser.add_argument("--out", default="docs/research/probes", help="where the record goes")
    parser.add_argument("--cwd", default=str(Path.home()), help="where the thread runs")
    parser.add_argument("--socket", default="/tmp/gpt-voicecoding-probe288/app-server.sock")
    parser.add_argument("--server-log", default=None, help="where the app-server's output goes")
    parser.add_argument("--input", type=int, default=None, help="input device index")
    parser.add_argument("--output", type=int, default=None, help="output device index")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parsed())))
