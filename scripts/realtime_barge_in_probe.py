#!/usr/bin/env python3
"""Can the user interrupt the Voice on realtime v3, and can we make them able to? (#286)

`docs/research/2026-09-07-realtime-barge-in.md` read codex `rust-v0.153.4` and
OpenAI's realtime docs and got as far as three facts and one unanswered question.

The facts. codex's barge-in — `conversation.item.truncate` on the user's first
syllable — is gated to realtime **v2** and v3 is routed down the v1 arm, where
every such branch is a no-op (`realtime_conversation.rs:568-569, 2328-2352`). The
v3 session JSON codex posts sets **no** `turn_detection` at all
(`methods_frameless_bidi.rs:52-95`), unlike v2's `server_vad` with
`interrupt_response: true` (`methods_v2.rs:100-105`). And on WebRTC the *server*
owns the output buffer, so truncation is not ours to do — the engine log of
2026-09-07 19:14 shows the backend transcribing five interruption attempts in
twelve seconds while generating straight through all of them, with `0 bytes
still buffered` on our side at every drain.

The question the source cannot answer: **does the frameless backend honour an
`audio.input.turn_detection` it is handed?** Nothing in codex ever sends one, so
no source says whether the field is unsupported, supported-and-off, or simply
never asked for. Only a live call can tell, and this script is that call.

It matters because the answer decides who can fix it. Honoured → the engine
sends one event on a data channel it already owns and the Voice becomes
interruptible. Not honoured → the fix is OpenAI's, or ours at the price of the
v2/websocket architecture priced in §7 of the research note.

This is a **probe, not a test**: it starts a real call against a real backend
and spends real API. Nothing here asserts; it records, and a person reads the
record. `scripts/realtime_text_entry_probe.py` is the base: the `Recorder`, the
`Call` and the WAV helpers below are its, kept to the same JSONL clock so a
barge-in record can be read beside #175's. They are **copied rather than
imported** because that script does not currently import — its
`from gpt_voicecoding.seams.call import DialReason` names something `src/` no
longer has. Repairing it is a separate errand; a probe that cannot be run is not
a base to build one on.

Four scenarios. The first three need nobody in the room:

    # A. The control. Nothing sent; the Voice is interrupted and we watch.
    .venv/bin/python scripts/realtime_barge_in_probe.py --scenario control

    # B. The question. `session.update` with turn_detection first, then the same cut-in.
    .venv/bin/python scripts/realtime_barge_in_probe.py --scenario session-update

    # C. The lever we already have, re-measured at 0.153.4 (#175 Q3).
    .venv/bin/python scripts/realtime_barge_in_probe.py --scenario append-speech

    # D. Echo. Real speaker, real microphone, no cut-in — does the Voice cut *itself* off?
    .venv/bin/python scripts/realtime_barge_in_probe.py --scenario echo

A and B differ by one event, so **run them back to back in that order**: a
control from a different hour is not a control. C is independent.

**How the cut-in is put.** `--cut-in wav` (the default) synthesises the
interruption with `say` and feeds it onto the production media track from
memory — real audio on the wire, no device opened, nobody in the room. That is
what separates "the backend heard audio and kept going" from "nobody actually
spoke". `--cut-in voice` waits for a person instead, and must be run **from your
own terminal**: the macOS microphone grant attaches to the process that asks, so
a call started from an agent or an IDE is silently muted.

**What makes the verdict readable.** The Voice is asked to count slowly to
sixty. A number is a clock: the transcript says exactly where it was cut, and
"stopped" and "kept going" cannot be confused with each other. The cut-in lands
in the middle of the count, and every notification carries a millisecond offset
from the dial.

Scenario D is worth running only once B has succeeded, and it is not optional
before calling this fixed: our microphone is a raw device capture with no
acoustic echo cancellation anywhere in the path, and the Voice plays out of the
same machine's speakers. Whatever makes the user interruptible makes the Voice
interruptible by itself. D is that measurement — speakers, not headphones.

Requires the voice extra: `.venv/bin/pip install -e '.[voice]'`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fractions
import json
import os
import subprocess
import sys
import time
import wave
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding import __version__  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.transport import CallTransport  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.webrtc import (  # noqa: E402
    EVENTS_CHANNEL,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    webrtc_transport,
)
from gpt_voicecoding.adapters.codex_app_server.process import OwnedAppServer  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings  # noqa: E402

#: Where the WAVs this run synthesises are kept. Outside the repo: they are
#: bytes regenerated on every run, not evidence — the JSONL is the evidence.
PROBE_DIR = Path("/tmp/gptvc-probe286")

#: What the Voice is told at dial time. Deliberately thin: the house rules are
#: not what is under test, and a long instruction is one more thing that could
#: explain a verdict.
INSTRUCTIONS = (
    "You are the voice of the GPT-VoiceCoding bridge, being probed for barge-in. "
    "Do exactly what you are asked, at the pace you are asked for. "
    "Never invent detail about what the system is doing."
)

#: The utterance the cut-in has to interrupt. A count is a clock: where the
#: transcript stops is where the interruption landed, to the number.
#:
#: **Unbroken, and that is the whole point** (#286 §9A). The first version of
#: this asked for one number per second, and the run that followed proved
#: nothing: at 1.6 s a number the cut-in landed in a *gap*, where an ordinary
#: end of turn is all that is needed, and the case the engine log shows failing
#: — a user cutting into continuous speech — was never tested. Asking for the
#: fastest possible count with no breath between numbers is what puts the
#: cut-in inside an utterance instead of between two. `CADENCE_CEILING` is how
#: the record proves it worked.
COUNT_REQUEST = (
    "Count out loud from one to two hundred, in English, as fast as you possibly can, "
    "running the numbers together with no pause and no breath between them, "
    "and do not stop or say anything else until you reach two hundred."
)

#: What the interruption says. Short, unmistakable, and a plain instruction —
#: so a backend that heard it and chose not to stop is distinguishable from one
#: that heard nothing.
CUT_IN_LINE = "Stop. Stop counting now."

#: The event under test, and the reason this script exists. The subtree matches
#: where the public Realtime API puts turn detection (`session.audio.input.
#: turn_detection`) and where codex puts it for v2 (`methods_v2.rs:100-105`);
#: the frameless session JSON has an `audio` object already, so this is the same
#: shape one level deeper.
#:
#: **Only the audio subtree is sent.** If the backend treats `session.update` as
#: a whole-session replace rather than a merge, the Voice loses its instructions
#: and the record will show it — that outcome is a finding too, not a spoiled run.
TURN_DETECTION = {
    "server_vad": {
        "type": "server_vad",
        "interrupt_response": True,
        "create_response": True,
        "silence_duration_ms": 500,
    },
    "semantic_vad": {
        "type": "semantic_vad",
        "interrupt_response": True,
        "create_response": True,
    },
}

#: How long after the cut-in the Voice must fall silent for this to count as an
#: interruption. Generous on purpose: a backend that stops mid-word and a
#: backend that finishes its sentence first are both "it worked", and both are
#: far from the 13 s the engine log measured for "it did not".
STOPPED_WITHIN_SECONDS = 4.0

#: The widest gap between two consecutive assistant transcript deltas that
#: still counts as continuous speech, over the seconds before the cut-in.
#:
#: This is a check on the probe, not on the backend. A run whose cadence is
#: above this measured the same thing #286 §9A did — an interruption between
#: two utterances — and its verdict says nothing about barge-in either way. It
#: cannot be read off the audio: this peer keeps RTP flowing through silence
#: (`Playout`'s own docstring, and the 0.228 s largest gap over a 74 s speaking
#: span in the engine log), so inbound frames cannot tell speech from pause.
#: Transcript cadence can.
CADENCE_CEILING = 0.6

#: How far back from the cut-in the cadence is measured. Long enough to hold
#: several deltas, short enough to be about the moment being interrupted.
CADENCE_WINDOW_SECONDS = 5.0


#: The `say` voice the cut-in is synthesised with, and the rate it is asked for.
#: A voice that is not downloaded exits 0 and writes a stub, so the length check
#: in `_say` is the only thing that catches it.
WAV_VOICE = "Samantha"
WAV_SAMPLE_RATE = 24_000
WAV_MINIMUM_SECONDS = 0.8

#: One 20 ms payload of digital silence, the shape `_Track.recv` expects.
SILENT_FRAME = b"\x00\x00" * FRAME_SAMPLES

#: What the adapter pins, restated here because this script bypasses it.
REALTIME_VERSION = "v3"
OUTPUT_MODALITY = "audio"
APPROVAL_POLICY = "never"
SANDBOX = "danger-full-access"

#: Audio deltas are per 20 ms and would bury the record; everything else prints.
QUIET = ("thread/realtime/outputAudioDelta",)


def _say(text: str, path: Path, *, voice: str, rate: int) -> Path:
    """Synthesise one utterance to a mono 16-bit WAV, and prove it is really one.

    `say` exits 0 for a voice it cannot actually speak with, writing a short stub
    instead of the sentence, so the exit code says nothing. What says something
    is the duration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "say",
            "-v",
            voice,
            "-o",
            str(path),
            f"--data-format=LEI16@{rate}",
            "--file-format=WAVE",
            text,
        ],
        check=True,
    )
    with wave.open(str(path), "rb") as synthesised:
        seconds = synthesised.getnframes() / synthesised.getframerate()
        channels, width = synthesised.getnchannels(), synthesised.getsampwidth()
    if channels != 1 or width != 2:
        raise RuntimeError(f"{path} is not mono 16-bit: channels={channels} width={width}")
    if seconds < WAV_MINIMUM_SECONDS:
        raise RuntimeError(
            f"voice {voice!r} produced {seconds:.2f}s for {text!r}, under the "
            f"{WAV_MINIMUM_SECONDS}s floor — it is almost certainly not installed. "
            "`say -v '?'` lists the names; a premium voice must be downloaded first."
        )
    return path


def _pcm_at_48k(path: Path) -> bytes:
    """One WAV as 48 kHz mono s16 bytes, resampled by `av` if it is not already.

    48 kHz is what the track carries (`webrtc.py`'s `SAMPLE_RATE`), and `av` is
    the resampler already in the process. `MediaPlayer` would have done all this
    and is still the wrong tool: it ends the track at EOF, which would stop RTP
    in the middle of the call.
    """
    import av

    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        payload = source.readframes(source.getnframes())
    if rate == SAMPLE_RATE:
        return payload

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(payload) // 2)
    frame.planes[0].update(payload)
    frame.sample_rate = rate
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, rate)
    resampled = bytearray()
    for out in [*resampler.resample(frame), *resampler.resample(None)]:
        # Only the first `samples * 2` bytes are audio; the rest of the plane is
        # padding, and padding is audible.
        resampled += bytes(out.planes[0])[: out.samples * 2]
    return bytes(resampled)


def _framed(pcm: bytes) -> list[bytes]:
    """PCM cut into the exact 20 ms payloads `_Track.recv` hands to `av`."""
    width = FRAME_SAMPLES * 2
    return [pcm[at : at + width].ljust(width, b"\x00") for at in range(0, len(pcm), width)]


def _silence(seconds: float) -> bytes:
    """Digital silence — an exact-zero floor, which is what a real room never has."""
    return b"\x00\x00" * int(seconds * SAMPLE_RATE)


class Recorder:
    """Every realtime notification: to a file with a timestamp, to the console short.

    The offsets are what make the record readable. Transcript arrival times are
    the only evidence that separates "it stopped" from "it kept going", and a
    silence is only a silence if you can say how long it lasted.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._dialled = time.monotonic()
        self.path = path
        self.events: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.sdp: asyncio.Future[str] | None = None
        self.started: asyncio.Future[None] | None = None
        self.closed: asyncio.Future[dict[str, Any]] | None = None

    def arm(self) -> None:
        """Create the futures. Needs a running loop, so not in `__init__`."""
        loop = asyncio.get_running_loop()
        self.sdp = loop.create_future()
        self.started = loop.create_future()
        self.closed = loop.create_future()

    def note(self, what: str, detail: Any = None) -> None:
        """Put one of this script's own steps into the same record, same clock."""
        record = {"at": self._offset(), "probe": what, "detail": detail}
        self.notes.append(record)
        self._write(record)
        print(f"  {record['at']:7.3f}  ** {what}" + (f": {detail}" if detail else ""), flush=True)

    def heard(self, message: dict[str, Any]) -> None:
        """One app-server notification. Registered with `OwnedAppServer.listen`."""
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not method.startswith("thread/realtime/"):
            return
        record = {"at": self._offset(), "method": method, "params": params}
        self.events.append(record)
        self._write(record)
        if method not in QUIET:
            print(f"  {record['at']:7.3f}  {_console(method, params)}", flush=True)
        self._settle(method, params)

    def transcripts(self, *, role: str, since: float = 0.0) -> list[str]:
        """Final transcript lines for one side of the call, since a mark."""
        return [
            str(event["params"].get("text", ""))
            for event in self._within("thread/realtime/transcript/done", since)
            if event["params"].get("role") == role
        ]

    def deltas(self, *, since: float = 0.0) -> list[tuple[float, str, str]]:
        """`(offset, role, text)` for every transcript delta, both roles.

        A delta names its text `delta`, not `text` — the two notifications do
        not share a field name, and reading `text` here returns an empty string
        for every delta.
        """
        return [
            (
                event["at"],
                str(event["params"].get("role", "")),
                str(event["params"].get("delta", "")),
            )
            for event in self._within("thread/realtime/transcript/delta", since)
        ]

    def _within(self, method: str, since: float) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["method"] == method
            and isinstance(event["params"], dict)
            and event["at"] >= since
        ]

    @property
    def now(self) -> float:
        return self._offset()

    def close(self) -> None:
        self._file.close()

    def _offset(self) -> float:
        return round(time.monotonic() - self._dialled, 3)

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def _settle(self, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        if method == "thread/realtime/sdp" and self.sdp is not None and not self.sdp.done():
            sdp = params.get("sdp")
            if isinstance(sdp, str):
                self.sdp.set_result(sdp)
        elif (
            method == "thread/realtime/started"
            and self.started is not None
            and not self.started.done()
        ):
            self.started.set_result(None)
        elif method == "thread/realtime/closed" and self.closed is not None:
            if not self.closed.done():
                self.closed.set_result(params)


def _console(method: str, params: Any) -> str:
    """One notification on one line, with the text that matters kept whole."""
    short = method.removeprefix("thread/realtime/")
    if not isinstance(params, dict):
        return short
    if short in ("transcript/done", "transcript/delta"):
        spoken = params.get("delta") if "delta" in params else params.get("text", "")
        return f"{short:18} {params.get('role', '?'):9} {spoken!r}"
    if short == "closed":
        return f"{short:18} reason={params.get('reason')!r}"
    return short


class Call:
    """One live v3 call, dialled the way the adapter dials it.

    The adapter cannot stand in for this: it owns its start parameters and its
    transport, and this script has to hold the data channel that the transport
    creates and discards.
    """

    def __init__(
        self,
        *,
        server: OwnedAppServer,
        recorder: Recorder,
        settings: RealtimeCallSettings,
        transport: CallTransport,
    ) -> None:
        self._server = server
        self._recorder = recorder
        self._settings = settings
        self._transport = transport
        self.thread_id: str | None = None

    async def dial(self, *, prompt: str) -> None:
        """The handshake `adapter._opened` runs.

        `prompt` and not `realtimeStartInstructions`: #175 Q4 raced the three
        start-time slots and only `prompt` reached the voice model on every one
        of six trials.
        """
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
            "prompt": prompt,
            "transport": {"type": "webrtc", "sdp": offer},
        }
        self._recorder.note(
            "dialling", {key: value for key, value in parameters.items() if key != "transport"}
        )
        await self._request("thread/realtime/start", parameters)

        deadline = self._settings.connect_timeout_seconds
        assert self._recorder.sdp is not None and self._recorder.started is not None
        answer = await asyncio.wait_for(self._recorder.sdp, deadline)
        await self._transport.accept_answer(answer)
        await asyncio.wait_for(self._recorder.started, deadline)
        await self._transport.wait_connected(deadline)
        self._recorder.note("call is up")

    async def speak(self, text: str) -> None:
        """`appendSpeech` — `session.context.append` with `channel: speakable`."""
        self._recorder.note("appendSpeech", text)
        await self._request(
            "thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": text}
        )

    async def hang_up(self) -> None:
        with contextlib.suppress(Exception):
            await self._request("thread/realtime/stop", {"threadId": self.thread_id})
        with contextlib.suppress(Exception):
            await self._transport.aclose()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._server.connection.request(
            method, params, timeout_seconds=self._settings.request_timeout_seconds
        )


class Interrupter:
    """The frame source that puts the cut-in on the track. Silence until asked.

    The third implementation of the one-method hole `_Microphone` already has
    two of, and the same one `--by wav` fills in #181's probe: captured frames,
    paced silence, queued WAV frames. The pacing is copied exactly because it is
    load-bearing — frames handed over as fast as the encoder asks run the media
    clock ahead of the wall clock, and the far side ends up listening to a call
    that already ended.

    What is different here is *when*: #181 queues its utterance and waits for a
    reply, and this queues one in the middle of somebody else's.
    """

    def __init__(self, recorder: Recorder, frames: list[bytes]) -> None:
        self._recorder = recorder
        self._frames = frames
        self._pending: deque[bytes] = deque()

    @property
    def draining(self) -> bool:
        return bool(self._pending)

    def cut_in(self) -> float:
        """Queue the interruption. Returns how long it takes to go out."""
        self._pending.extend(self._frames)
        seconds = len(self._frames) * FRAME_SAMPLES / SAMPLE_RATE
        self._recorder.note("cut-in on the track", {"seconds": round(seconds, 2)})
        return seconds

    async def next(self, track: Any) -> bytes:
        """One 20 ms payload: the next cut-in frame, or silence, paced in real time."""
        if track._started is None:
            track._started = time.monotonic()
        delay = track._started + track._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if not self._pending:
            return SILENT_FRAME
        payload = self._pending.popleft()
        if not self._pending:
            self._recorder.note("cut-in finished going out")
        return payload


def install_interrupter(
    transport: CallTransport, recorder: Recorder, arguments: argparse.Namespace
) -> Interrupter:
    """Feed the transport's existing track from a WAV instead of a device.

    The substitution is at the frame source and nowhere else: the peer
    connection, the track, the Opus encoder and the real-time pacing are the
    production ones out of `webrtc.py`. A probe that built its own transport
    would be evidence about the probe. Nothing in `src/` changes, and no device
    is opened — the microphone grant is triggered by opening one, and this route
    never does.
    """
    spoken = _pcm_at_48k(
        _say(
            arguments.line,
            PROBE_DIR / "wav" / f"{arguments.stamp}-cut-in.wav",
            voice=arguments.wav_voice,
            rate=WAV_SAMPLE_RATE,
        )
    )
    # A beat of silence each side: a WAV that starts on the first syllable gives
    # the far side's VAD no onset to find, and one that ends on the last gives
    # it no offset.
    padded = _silence(0.4) + spoken + _silence(0.6)
    source = Interrupter(recorder, _framed(padded))
    # Reaching past the transport's own surface, deliberately and only here.
    transport._microphone._next = source.next  # type: ignore[attr-defined]
    recorder.note(
        "cut-in source installed",
        {
            "voice": arguments.wav_voice,
            "line": arguments.line,
            "seconds": round(len(padded) / 2 / SAMPLE_RATE, 2),
        },
    )
    return source


class Channel:
    """The realtime events data channel, held open for writing.

    The engine creates this channel (`webrtc.py:282`) and only ever reads one
    family off it. It is bidirectional, and whether the frameless backend
    accepts a client event on it is the whole question — so this holds the
    channel `webrtc.py` throws away, records **every** event that arrives on it
    (not the first-seen types the engine logs), and can send.

    The channel is caught by wrapping `RTCPeerConnection.createDataChannel`
    before the transport is built. Reading it back off `pc.sctp._data_channels`
    afterwards would work too and would depend on an aiortc private that has no
    reason to stay put; a wrapper depends on the public constructor.
    """

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._channel: Any = None
        self._restore: Any = None

    def arm(self) -> None:
        """Wrap the constructor. Must run before `webrtc_transport`."""
        from aiortc import RTCPeerConnection

        original = RTCPeerConnection.createDataChannel
        holder = self

        def wrapper(self: Any, label: str, *rest: Any, **named: Any) -> Any:
            channel = original(self, label, *rest, **named)
            if label == EVENTS_CHANNEL:
                holder._adopt(channel)
            return channel

        self._restore = original
        RTCPeerConnection.createDataChannel = wrapper  # type: ignore[method-assign]

    def disarm(self) -> None:
        from aiortc import RTCPeerConnection

        if self._restore is not None:
            RTCPeerConnection.createDataChannel = self._restore  # type: ignore[method-assign]

    def _adopt(self, channel: Any) -> None:
        self._channel = channel

        @channel.on("message")
        def _heard(message: Any) -> None:
            self._record(message)

    def _record(self, message: Any) -> None:
        if isinstance(message, bytes | bytearray):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return
        try:
            event = json.loads(message)
        except ValueError:
            self._recorder.note("channel sent something that is not JSON", message[:200])
            return
        kind = event.get("type") if isinstance(event, dict) else None
        # `error` is the one this script cannot afford to summarise away: it is
        # how a rejected `session.update` says so, and it is the answer.
        if kind in ("error", "session.updated", "session.started"):
            self._recorder.note(f"channel event {kind}", event)
        else:
            self._recorder.note(f"channel event {kind}", None)

    def send(self, payload: dict[str, Any]) -> None:
        """One client event onto the channel. The point of the whole script."""
        if self._channel is None:
            raise RuntimeError(f"no {EVENTS_CHANNEL!r} channel was created")
        if self._channel.readyState != "open":
            raise RuntimeError(f"the events channel is {self._channel.readyState}, not open")
        self._recorder.note("sending on the events channel", payload)
        self._channel.send(json.dumps(payload))


def _assistant(recorder: Recorder, *, since: float) -> list[tuple[float, str, str]]:
    """The assistant's transcript deltas since a mark.

    `Recorder.deltas` returns both roles — the role is the second element of
    each tuple, not a filter — and reading it without this was the first thing
    that made a run say the Voice had stopped when it had not.
    """
    return [record for record in recorder.deltas(since=since) if record[1] == "assistant"]


async def counting(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> float:
    """Ask for the count and wait until it is well under way. Returns the mark."""
    # The window opens *before* the request, not at the dial: v3 speaks an
    # opening line of its own, and counting deltas from zero would find that
    # one and call the count started before it had begun.
    asked = recorder.now
    await call.speak(COUNT_REQUEST)
    deadline = time.monotonic() + arguments.reply
    while time.monotonic() < deadline:
        if _assistant(recorder, since=asked):
            break
        await asyncio.sleep(0.1)
    else:
        recorder.note("the Voice never started counting", None)
    recorder.note(f"letting it count for {arguments.cut_in_after}s")
    await asyncio.sleep(arguments.cut_in_after)
    return recorder.now


async def cut_in(
    call: Call,
    recorder: Recorder,
    arguments: argparse.Namespace,
    interrupter: Interrupter | None,
) -> None:
    """Interrupt, by whichever route this run is measuring."""
    if arguments.scenario == "append-speech":
        await call.speak(CUT_IN_LINE)
        return
    if arguments.cut_in == "voice":
        print("\n" + "=" * 72, flush=True)
        print(f'  SAY THIS ALOUD, NOW, OVER THE COUNTING:   "{arguments.line}"', flush=True)
        print("=" * 72 + "\n", flush=True)
        return
    assert interrupter is not None
    interrupter.cut_in()


async def watch(recorder: Recorder, arguments: argparse.Namespace, mark: float) -> None:
    """Let the call run on past the cut-in, so silence has time to be silence."""
    recorder.note(f"watching for {arguments.watch}s after the cut-in")
    await asyncio.sleep(arguments.watch)
    _ = mark


async def control(
    call: Call, recorder: Recorder, arguments: argparse.Namespace, channel: Channel, source: Any
) -> None:
    """A. Nothing sent. What the engine does today, measured deliberately."""
    mark = await counting(call, recorder, arguments)
    await cut_in(call, recorder, arguments, source)
    await watch(recorder, arguments, mark)
    verdict(recorder, arguments, mark)


async def session_update(
    call: Call, recorder: Recorder, arguments: argparse.Namespace, channel: Channel, source: Any
) -> None:
    """B. The question: turn detection asked for on our own channel, then the same cut-in."""
    detection = TURN_DETECTION[arguments.turn_detection]
    channel.send(
        {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "turn_detection": detection,
                    }
                }
            },
        }
    )
    # A rejection comes back as an `error` event, not as a raised exception, so
    # the only way to see one is to give it a moment and read the record.
    await asyncio.sleep(arguments.settle)
    mark = await counting(call, recorder, arguments)
    await cut_in(call, recorder, arguments, source)
    await watch(recorder, arguments, mark)
    verdict(recorder, arguments, mark)


async def append_speech(
    call: Call, recorder: Recorder, arguments: argparse.Namespace, channel: Channel, source: Any
) -> None:
    """C. #175 Q3's lever, re-measured here: does `appendSpeech` still truncate?

    Measured at codex 0.151.0 and read as "the in-flight turn absorbs it and
    abandons what it was saying". That was a backend behaviour, not a codex one,
    so it is not carried forward by the version bump — it has to be re-measured
    at 0.153.4 before anything is built on it.
    """
    mark = await counting(call, recorder, arguments)
    await cut_in(call, recorder, arguments, source)
    await watch(recorder, arguments, mark)
    verdict(recorder, arguments, mark)


async def echo(
    call: Call, recorder: Recorder, arguments: argparse.Namespace, channel: Channel, source: Any
) -> None:
    """D. Real speaker, real microphone, no cut-in at all.

    The failure this looks for is the Voice interrupting *itself*: our capture
    has no echo cancellation, so the Voice's own output goes back up the
    microphone. If barge-in is on and the room is not, the count stops on its
    own and nobody spoke. Run it on speakers — on headphones it can only pass.
    """
    if arguments.scenario == "echo" and arguments.silent:
        recorder.note("echo with no devices proves nothing; rerun without --silent", None)
    mark = await counting(call, recorder, arguments)
    recorder.note("no cut-in: the only speaker in the room is the Voice")
    await watch(recorder, arguments, mark)
    verdict(recorder, arguments, mark)


SCENARIOS = {
    "control": control,
    "session-update": session_update,
    "append-speech": append_speech,
    "echo": echo,
}


def _cadence(recorder: Recorder, mark: float) -> tuple[float | None, bool]:
    """The widest gap between assistant deltas just before the cut-in, and the check.

    Returns `(gap, continuous)`. A `gap` of `None` means the window held fewer
    than two deltas — nothing was being said, and there was nothing to
    interrupt. `continuous` is what decides whether this run is about barge-in
    at all: see `CADENCE_CEILING`.
    """
    window = [
        at for at, _, _ in _assistant(recorder, since=mark - CADENCE_WINDOW_SECONDS) if at <= mark
    ]
    if len(window) < 2:
        return None, False
    widest = max(later - earlier for earlier, later in zip(window, window[1:], strict=False))
    return widest, widest <= CADENCE_CEILING


def verdict(recorder: Recorder, arguments: argparse.Namespace, mark: float) -> None:
    """Read the record back and say, in one screen, what the call did.

    Three things decide it, and they are separable on purpose: whether the
    backend *heard* the cut-in, whether the Voice *stopped*, and how long it
    took. A backend that heard nothing and one that heard and carried on are
    different findings with the same sound in the room.
    """
    after = _assistant(recorder, since=mark)
    heard = recorder.transcripts(role="user", since=mark)
    last = after[-1][0] if after else None
    spoken = "".join(text for _, _, text in after)
    cadence, was_speaking = _cadence(recorder, mark)

    print("\n" + "=" * 72, flush=True)
    print(f"  scenario            {arguments.scenario}")
    print(f"  cut-in at           {mark:.3f}s from the dial")
    if cadence is None:
        print("  speech before it    no deltas in the window — nothing was being said")
    else:
        print(
            f"  speech before it    widest delta gap {cadence:.3f}s over the last "
            f"{CADENCE_WINDOW_SECONDS:.0f}s — "
            f"{'CONTINUOUS' if was_speaking else 'BROKEN — turn-taking, not barge-in'}"
        )
    print(f"  backend heard it    {heard if heard else 'NO user transcript after the cut-in'}")
    if last is None:
        print("  Voice after cut-in  nothing — it was already done, or never started")
    else:
        print(f"  Voice after cut-in  kept going until {last:.3f}s ({last - mark:.1f}s past it)")
        print(f"  and said            {spoken[:200]!r}")
    if not was_speaking:
        print("  VERDICT             INCONCLUSIVE — the cut-in landed in a gap (see above)")
    elif last is not None and last - mark <= STOPPED_WITHIN_SECONDS:
        print(f"  VERDICT             INTERRUPTED — stopped within {STOPPED_WITHIN_SECONDS}s")
    elif last is None:
        print("  VERDICT             INCONCLUSIVE — no counting to interrupt")
    else:
        print("  VERDICT             NOT INTERRUPTED — it talked past the cut-in")
    print(f"  record              {recorder.path}")
    print("=" * 72 + "\n", flush=True)


async def main(arguments: argparse.Namespace) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    arguments.stamp = stamp
    recorder = Recorder(Path(arguments.out).expanduser() / f"{stamp}-{arguments.scenario}.jsonl")
    recorder.arm()

    os.environ.setdefault("RUST_LOG", "codex_core::realtime_conversation=info,codex_core=info")
    settings = RealtimeCallSettings(workspace=Path(arguments.cwd).expanduser().resolve())
    server = OwnedAppServer(
        settings=CodexSettings(),
        socket_path=Path(arguments.socket),
        log_path=Path(arguments.server_log) if arguments.server_log else None,
        version=__version__,
    )
    server.listen(recorder.heard)

    # `echo` is the one scenario whose whole content is a real speaker and a
    # real microphone. Everywhere else a device is a liability: it needs a grant
    # this process may not have, and it puts room noise into a measurement of
    # what the backend does with one known utterance.
    silent = arguments.scenario != "echo" and arguments.cut_in == "wav"
    if arguments.silent is not None:
        silent = arguments.silent

    channel = Channel(recorder)
    channel.arm()
    try:
        transport = webrtc_transport(
            input_device=arguments.input, output_device=arguments.output, silent=silent
        )
    finally:
        channel.disarm()

    source = None
    if arguments.cut_in == "wav" and arguments.scenario in ("control", "session-update"):
        source = install_interrupter(transport, recorder, arguments)

    call = Call(server=server, recorder=recorder, settings=settings, transport=transport)
    print(f"  probe  {arguments.scenario}, {'no audio devices' if silent else 'real audio'}")
    print(f"  probe  recording to {recorder.path}")
    await server.start()
    try:
        await call.dial(prompt=INSTRUCTIONS)
        # The channel opens with the peer connection, but `readyState` trails
        # it by a beat, and a `send` a beat early raises rather than queues.
        await asyncio.sleep(arguments.settle)
        await SCENARIOS[arguments.scenario](call, recorder, arguments, channel, source)
        return 0
    finally:
        print("  end    hanging up")
        with contextlib.suppress(Exception):
            await call.hang_up()
        await server.aclose()
        recorder.close()
        print("  end    done")


def parsed() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="control")
    parser.add_argument(
        "--cut-in",
        choices=("wav", "voice"),
        default="wav",
        help="synthesised audio on the media track, or a person at the microphone (HITL)",
    )
    parser.add_argument(
        "--turn-detection",
        choices=sorted(TURN_DETECTION),
        default="server_vad",
        help="`session-update` only: which detector to ask the backend for",
    )
    parser.add_argument("--line", default=CUT_IN_LINE, help="what the interruption says")
    parser.add_argument(
        "--wav-voice",
        default=WAV_VOICE,
        help="`--cut-in wav` only: the `say` voice. One not downloaded fails the length check",
    )
    parser.add_argument(
        "--cut-in-after",
        type=float,
        default=8.0,
        help="how long the Voice counts before it is interrupted",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=25.0,
        help="how long the call runs on after the cut-in, so silence can be silence",
    )
    parser.add_argument(
        "--reply", type=float, default=20.0, help="how long the count may take to start"
    )
    parser.add_argument(
        "--settle", type=float, default=2.0, help="pause after the dial and the send"
    )
    parser.add_argument(
        "--silent",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open no audio device; defaults to on except for `echo` and `--cut-in voice`",
    )
    parser.add_argument("--out", default="docs/research/probes", help="where the JSONL record goes")
    parser.add_argument("--cwd", default=str(Path.home()), help="where the thread runs")
    parser.add_argument("--socket", default="/tmp/gpt-voicecoding-probe286/app-server.sock")
    parser.add_argument("--server-log", default=None, help="where the app-server's output goes")
    parser.add_argument("--input", type=int, default=None, help="input device index")
    parser.add_argument("--output", type=int, default=None, help="output device index")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parsed())))
