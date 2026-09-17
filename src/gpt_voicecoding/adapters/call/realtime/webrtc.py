"""The one module in this repository that opens a microphone and a peer connection.

Every `aiortc`, `av` and `sounddevice` import in the system is here, and that is
the whole reason the module exists. `tests/test_architecture.py` asserts it:
Bridge Core and the seams may not speak a protocol at all, and this spoke's
protocol may not leak out of this file into the adapter's signalling logic,
which is the part CI actually runs.

They are optional dependencies (`pip install 'gpt-voicecoding[voice]'`). This
module therefore imports them at call time rather than at import time, and
`probe()` is what the factory calls to turn "the operator did not install the
voice extra" into a refusal to assemble the engine — before the user is on a
call at two in the morning, rather than during one.

**What the shape of the audio path is, and why.** 48 kHz mono `s16` in 20 ms
frames, which is `aiortc`'s Opus-native rate; the backend resamples to its own
24 kHz internally. Each direction crosses between a device clock and the call's
clock once, and each crossing has one owner with its own rule (#365, ADR 0030):
the speaker trades a few tens of milliseconds of delay for continuous
listening, and the microphone trades completeness for fresh speaking. Both
drop the *oldest* audio when they overflow — never an exception per frame, and
never unbounded memory. The playback
side copies only `samples * 2` bytes out of each resampled plane — the plane's
buffer is padded, and playing the padding is audible as static. That last one
was learned from the prototype the hard way and is the sort of detail a rewrite
silently loses.
"""

from __future__ import annotations

import array
import asyncio
import collections
import contextlib
import enum
import fractions
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gpt_voicecoding.adapters.call.realtime.transport import (
    CallTransport,
    CueOutput,
    EventHandler,
    LostHandler,
    TransportError,
    TransportFactory,
)

_log = logging.getLogger(__name__)

#: `aiortc`'s Opus-native sample rate. The backend resamples to its own.
SAMPLE_RATE = 48_000

#: 20 ms at that rate — one Opus frame.
FRAME_SAMPLES = 960

#: The rest of the shape, named once. Every stream this module opens is the same
#: mono 16-bit audio at `SAMPLE_RATE`, and so is every buffer handed to one — the
#: microphone's frames, the speaker's playback and a cue's PCM. Spelled out at
#: each `sounddevice` call it would be three chances to open one stream in a
#: format the buffers feeding it are not in, and `bytes // 2` scattered around is
#: the same fact written as arithmetic nobody can search for.
CHANNELS = 1
SAMPLE_FORMAT = "int16"
SAMPLE_BYTES = 2

#: One frame, as a duration. Derived from the two numbers above rather than
#: written out, so the two things measured in frames below cannot drift from the
#: frames they are counting.
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

#: LiveKit APM accepts exactly 10 ms per processing call.
APM_SAMPLES = SAMPLE_RATE // 100
APM_BYTES = APM_SAMPLES * SAMPLE_BYTES

#: How long queued playback takes to be heard, as a rate. This is the whole of
#: the closing rule's arithmetic (#301) and it is not a tunable: it is
#: `SAMPLE_RATE` and `SAMPLE_BYTES` multiplied, which is what "how long does
#: this much audio last" means for this format and can mean nothing else. A
#: number somebody picked would be a threshold; this one is the stream's shape.
PLAYBACK_BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_BYTES

#: The label of the events data channel this adapter offers. The backend's SDP
#: expects one to be described; it is also read, but since #301 for one purpose
#: only — naming each event type it carries, once, so a codex protocol change
#: shows up in the engine log. Every event this adapter *acts* on arrives as a
#: JSON-RPC notification on the app-server socket.
EVENTS_CHANNEL = "realtime-events"

#: How long the capture worker's own handoff may hold audio before it gives up:
#: two seconds, long enough to ride out a scheduling hiccup. It no longer bounds
#: what reaches the sender — `MAX_SENDER_LAG_FRAMES` does (#365).
MAX_CAPTURE_FRAMES = 100

#: The same bound for playback, as two seconds of it. Also the ceiling on a
#: Playout span: what the span begins holding is what is in this buffer, so no
#: span can be longer than this however much audio the peer sent (#301).
MAX_PLAYBACK_BYTES = PLAYBACK_BYTES_PER_SECOND * 2

#: One frame of playback, as bytes: the unit the device takes and the unit a
#: stretch of digital silence is recognised in.
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_BYTES

#: The most listening delay the speaker may add to ride out late audio (#365,
#: ADR 0030). Not the headroom — that is measured from arrival lateness — but the
#: product's limit on it: a live conversation tolerates tens of milliseconds, so
#: this is the most whole frames that stay under a hundred. The recorded calls
#: measure lateness far past it (the far side's own startup hold), so it binds.
MAX_HEADROOM_SECONDS = FRAME_SECONDS * 4

#: How far the sender may fall behind the microphone before the oldest captured
#: frame is dropped (#365, ADR 0030). One frame for each thread handoff between
#: the capture callback and the sender — callback to echo-cancellation worker,
#: worker to event loop — because each can be holding one frame at the instant
#: the sender pulls. Anything queued beyond that is backlog, not jitter.
MAX_SENDER_LAG_FRAMES = 2

#: What to install when the import fails.
INSTALL_HINT = "pip install 'gpt-voicecoding[voice]'"

#: The distributions this route cannot run without.
REQUIRED = ("aiortc", "av", "sounddevice", "CoreAudio", "livekit.rtc")


class VoiceDependencyError(Exception):
    """The voice extra is not installed, so there is no audio path to build."""


class SpanClosedBy(enum.Enum):
    """Which fact closed a span of the Voice's playout (#235, rewritten by #301).

    The value is the clause the stop-edge line carries, so the log and the code
    cannot name the same fact two ways. There is one member, because there is
    one rule: the audio the Voice had generated has had time to be heard. A
    wait that ran out its bound has no member here, because nothing closed it —
    and telling those two lines apart is why an enum with one member is still
    an enum.

    The two members this replaces — the server's own word, and inbound audio
    going quiet — are gone with the rules they named (#301).
    """

    HEARD = "the audio the Voice generated had time to be heard"


@dataclass(frozen=True)
class Playout:
    """What the speaker saw over one stretch of waiting — the four facts (#230).

    Until #235, `drained` was decided by two things this side cannot influence
    and did not report: whether inbound frames stopped, and whether the device
    still has audio queued. So a stop edge that ran out its bound said only
    that it had, and two entirely different causes — a remote peer that keeps
    RTP flowing through silence, and an event loop starved into delivering
    frames in bursts — arrived at the same one line.

    Since #301 none of the inbound facts *decides* anything: the span's end is
    computed from `started_with_bytes` alone. They are kept, all four, because
    they are what made this ticket diagnosable and they are the sentinel for
    the next stall — a measurement that stopped being a condition is still a
    measurement.

    The four together are what tells them apart. A large `largest_gap_seconds`
    with `frames` still climbing is a starved loop: the pauses were real, and
    the burst that followed refreshed the trailing gap before anyone looked. A
    small one beside a small `since_last_frame_seconds` is a peer that never
    stopped sending. Either beside a non-zero `buffered_bytes` is neither: the
    device stalled with audio still queued, which leaves no `dropped` line
    either, and was the one candidate the ticket's own reasoning did not
    exclude.

    Read as a value, never as live attributes: every field is measured at the
    same instant, and a reader comparing two of them sampled a moment apart
    would be comparing two different stalls.
    """

    #: How much of the Voice's audio was still queued for the device when the
    #: span began, and so the whole input to the closing rule (#301). Beside
    #: `buffered_bytes` it also says whether the span played out everything it
    #: was given: one that closed holding less than it began with did. Read it
    #: against `MAX_PLAYBACK_BYTES` before reading it as the length of what the
    #: Voice said — a buffer that overflowed reports the ceiling, and the
    #: `dropped` line is what says it was the ceiling and not the answer.
    started_with_bytes: int
    #: Inbound frames over the window — see `_Speaker.take_playout` for which.
    frames: int
    #: How long ago the last inbound frame arrived, `None` if none ever has.
    #: Measured from the whole call, not the window: a window with no frames in
    #: it still has a meaningful answer, and it is the interesting one.
    since_last_frame_seconds: float | None
    #: The longest silence between two consecutive frames in the window, `None`
    #: when the window holds no gap to measure.
    largest_gap_seconds: float | None
    #: What was still queued for the device, and so still unplayed.
    buffered_bytes: int

    def __str__(self) -> str:
        """One clause per fact, in the order a reader needs them.

        Rendered here rather than at each log call so the two lines that carry
        this cannot drift into two vocabularies for one measurement. Absent is
        spelled out rather than printed as zero: "no frame ever arrived" and "a
        gap of zero" are opposite diagnoses.
        """
        last = self.since_last_frame_seconds
        widest = self.largest_gap_seconds
        return (
            f"began with {self.started_with_bytes} bytes of audio, "
            f"{self.frames} inbound frame{'' if self.frames == 1 else 's'}, "
            f"{'none has arrived' if last is None else f'last {last:.3f}s ago'}, "
            f"{'no gap measured' if widest is None else f'largest gap {widest:.3f}s'}, "
            f"{self.buffered_bytes} bytes still buffered"
        )


def probe() -> None:
    """Prove the voice extra is really installed, or refuse by name.

    Called when the adapter is constructed — that is, while the engine is being
    assembled — so a deployment that configured this Call adapter without the
    dependencies it needs fails at start with a sentence naming the fix. ADR
    0003's rule is that a seam configured but not loadable is an outage; voice
    is this product's main path, and discovering the outage at the moment the
    user tries to talk is the worst possible place to discover it.
    """
    missing = []
    for name in REQUIRED:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        raise VoiceDependencyError(
            f"the bridge-owned realtime call needs {', '.join(missing)}, which "
            f"{'are' if len(missing) > 1 else 'is'} not installed: {INSTALL_HINT}"
        )


def call_audio(
    *, input_device: int | None, output_device: int | None
) -> tuple[TransportFactory, CueOutput]:
    """Build the transport factory and its independent Cue player with one device owner."""
    probe()
    output = _OutputSelection(output_device)

    def build() -> CallTransport:
        return _WebRtcTransport(
            input_device=input_device, output_device=output_device, silent=False, output=output
        )

    return build, CuePlayer(_output=output)


def webrtc_transport(
    *,
    input_device: int | None = None,
    output_device: int | None = None,
    silent: bool = False,
) -> CallTransport:
    """One WebRTC audio path, ready to be offered.

    `silent` opens no audio device at all: it sends paced silence and counts
    what comes back. That is what makes a signalling round trip against a real
    app-server runnable on a machine with no microphone grant, and it is the
    only reason the flag exists — it is not a mode the engine ever selects.
    """
    probe()
    return _WebRtcTransport(
        input_device=input_device,
        output_device=output_device,
        silent=silent,
        output=_OutputSelection(output_device),
    )


class _WebRtcTransport:
    """`aiortc` behind the `CallTransport` interface. Built by `webrtc_transport`."""

    def __init__(
        self,
        *,
        input_device: int | None,
        output_device: int | None,
        silent: bool,
        output: _OutputSelection | None = None,
    ) -> None:
        from aiortc import RTCPeerConnection

        self._pc = RTCPeerConnection()
        self._silent = silent
        self._output = output or _OutputSelection(output_device)
        self._speaker = _Speaker(silent=silent, device=output_device)
        self._microphone = _Microphone(
            silent=silent, device=input_device, output=self._output, failed=self._audio_failed
        )
        self._on_lost: LostHandler | None = None
        self._closing = False
        #: Whether the loss has already been reported upward. Its own flag, and
        #: not `_closing`: see `_note`.
        self._reported = False
        self._connected: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._connected.add_done_callback(_retrieved)

        # The realtime events data channel. The backend expects the offer to
        # describe one. Every event this adapter acts on arrives as a JSON-RPC
        # notification on the app-server socket — one stream of truth for the
        # call's course — and since #301 nothing on this channel is acted on at
        # all. Event types seen here are named in the log once each, so a run
        # shows what this backend actually sends.
        self._events_seen: set[str] = set()
        #: Who is handed each of those events (#377). None until the adapter asks.
        self._on_event: EventHandler | None = None
        channel = self._pc.createDataChannel(EVENTS_CHANNEL)

        @channel.on("message")
        def _on_message(message: Any) -> None:
            self._read_channel_event(message)

        try:
            if not silent:
                # An open silent output keeps the device clock/reference running
                # before Voice arrives and between utterances.
                identity = self._speaker.open()
            else:
                identity = None
            self._pc.addTrack(self._microphone.track(identity))
        except BaseException:
            self._microphone.stop()
            self._speaker.stop()
            asyncio.ensure_future(self._pc.close())
            raise

        @self._pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind == "audio":
                self._speaker.attach(track)

        @self._pc.on("connectionstatechange")
        def _on_state() -> None:
            self._note(self._pc.connectionState)

    # -- the interface ----------------------------------------------------

    async def offer(self) -> str:
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        # aiortc gathers without trickling, so this settles in milliseconds.
        while self._pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        description = self._pc.localDescription
        if description is None:  # pragma: no cover - aiortc always sets one
            raise TransportError("the peer connection produced no local description")
        return str(description.sdp)

    async def accept_answer(self, sdp: str) -> None:
        from aiortc import RTCSessionDescription

        try:
            await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        except Exception as unusable:
            raise TransportError(f"the app-server's SDP answer was refused: {unusable}") from None

    async def wait_connected(self, timeout_seconds: float) -> None:
        self._note(self._pc.connectionState)
        try:
            await asyncio.wait_for(asyncio.shield(self._connected), timeout_seconds)
        except TimeoutError:
            raise TransportError(
                f"audio never started flowing within {timeout_seconds:g}s "
                f"(the peer connection is {self._pc.connectionState})"
            ) from None

    @property
    def is_connected(self) -> bool:
        return bool(self._pc.connectionState == "connected")

    async def playback_drained(self, timeout_seconds: float) -> None:
        """Wait out the speaker, within a bound. See `CallTransport.playback_drained`.

        **The span's end is known at its start, so it is awaited rather than
        polled** (#301). This call *is* the moment the Voice stopped
        generating, so the audio the speaker is holding right now is the whole
        of what the user has yet to hear, and it lasts exactly as long as its
        own byte rate says. There is nothing to re-ask, and so no poll loop:
        the wait is that duration, or the bound, whichever comes first.

        **The bound is a last resort and this is what it now takes to reach
        it** — a span holding more audio than the caller allowed time for.
        `MAX_PLAYBACK_BYTES` caps a span at two seconds, so only a
        `voice_playout_wait_seconds` configured below that can reach it, and
        the shipped default is nowhere near. That is the intended state of a
        last resort, and it is kept for a backend that stalls in some way this
        rule has not met yet.

        **Both exits say what the speaker saw** (#230). The timeout line used to
        report that a thing had not happened and nothing about why, which left
        the one measurement that distinguishes a peer still sending from a
        starved event loop unrecorded on the only run that needed it. The
        ordinary exit carries the same facts for the same reason a control needs
        to be measured too: a stalled run is only diagnosable against what a
        clean one on this machine looks like.

        The window is closed on the way past either exit and never twice, so
        each line describes exactly the wait it ends. `Playout` renders itself,
        so this decides nothing about wording.
        """
        span_seconds = self._speaker.begin_span()
        if span_seconds > timeout_seconds:
            await asyncio.sleep(timeout_seconds)
            _log.info(
                "playout had not drained %gs after the Voice stopped generating "
                "(the audio it began holding needed longer than that to be heard); "
                "reporting the stop edge anyway — %s",
                timeout_seconds,
                self._speaker.take_playout(),
            )
            return
        await asyncio.sleep(span_seconds)
        _log.info(
            "the Voice's playout drained: %s — %s",
            SpanClosedBy.HEARD.value,
            self._speaker.take_playout(),
        )

    def _read_channel_event(self, message: Any) -> None:
        """One message off the events channel, named once per type (#235, #301).

        Nothing here closes a span any more. #235 read this channel for the
        server's own end-of-playout event and logged every type beside it, so
        that a run could answer whether the backend sends that event at all.
        Across 82 sessions it answered no, and the rule went (#301) — but the
        census stays: it is how the next codex protocol change gets noticed
        rather than silently absorbed.

        Since #377 each event is also handed to whoever asked with `on_event`:
        the Voice's own usage arrives on this channel and nowhere else.

        Anything that is not a JSON object with a string `type` is not a server
        event, and is neither named nor handed up.
        """
        if isinstance(message, bytes | bytearray):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return
        try:
            event = json.loads(message)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if not isinstance(kind, str):
            return
        if kind not in self._events_seen:
            self._events_seen.add(kind)
            _log.info("the realtime events channel carried %s", kind)
        handler = self._on_event
        if handler is None:
            return
        try:
            handler(event)
        except Exception:
            _log.exception("a realtime events channel event handler raised")

    def on_lost(self, handler: LostHandler) -> None:
        self._on_lost = handler

    def on_event(self, handler: EventHandler) -> None:
        self._on_event = handler

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        if not self._connected.done():
            # Cancelled rather than failed: a close this side asked for is not
            # the connection having gone wrong, and anything still waiting on
            # the handshake is being abandoned, not told about a fault.
            self._connected.cancel()
        self._microphone.stop()
        self._speaker.stop()
        with contextlib.suppress(Exception):
            await self._pc.close()

    def _audio_failed(self, error: Exception) -> None:
        if self._closing:
            return
        reason = f"the call's audio processing failed: {error}"
        self._report_loss(reason)
        asyncio.ensure_future(self.aclose())

    # -- state ------------------------------------------------------------

    def _note(self, state: str) -> None:
        """One connection-state reading, turned into the two things that matter.

        **Reporting a loss is not closing.** This once set `_closing` to keep
        itself from reporting the same loss twice, and `_closing` is also what
        makes `aclose` idempotent — so a connection that went away by itself was
        marked closed without anything having been closed, and the `aclose` the
        adapter then ran returned at the first line. The microphone and the
        speaker stayed open on a dead call, which is a device held and a
        microphone live with nothing listening. Two facts, two flags.
        """
        if state == "connected" and not self._connected.done():
            self._connected.set_result(None)
            return
        if state not in ("failed", "closed"):
            return
        self._report_loss(f"the call's audio connection is {state}")

    def _report_loss(self, reason: str) -> None:
        if not self._connected.done():
            self._connected.set_exception(TransportError(reason))
        if self._closing or self._reported:
            return  # a close this side asked for is not a loss, and once is enough
        self._reported = True
        handler, self._on_lost = self._on_lost, None
        if handler is not None:
            handler(reason)


def _retrieved(waiting: asyncio.Future[None]) -> None:
    """Look at the outcome, so one nobody awaited is not an asyncio warning.

    Whether anything is waiting on the handshake depends on exactly where in it
    the connection died — and on a call that failed before `wait_connected` was
    reached, an "exception was never retrieved" warning is noise standing where
    the real reason, already reported upward as a dropped call, should be.
    """
    if not waiting.cancelled():
        waiting.exception()


class CuePlayer:
    """Independent cue stream; share the call device only while capture is alive.

    ENDED can sound after capture closes, so its stream is never owned by the
    peer connection. The adapter's existing worker controls ordering; this
    player blocks until the device has drained and always closes its stream.
    """

    def __init__(
        self, *, device: int | None = None, _output: _OutputSelection | None = None
    ) -> None:
        self._output = _output or _OutputSelection(device)

    @property
    def device(self) -> int | None:
        return self._output.device

    def play(self, pcm: bytes) -> None:
        """Open, write, drain, close. Blocking; the adapter owns worker ordering."""
        import sounddevice

        stream = sounddevice.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=SAMPLE_FORMAT,
            blocksize=FRAME_SAMPLES,
            device=self.device,
        )
        try:
            stream.start()
            stream.write(pcm)
            stream.stop()
        finally:
            stream.close()


class _Microphone:
    """What the user says, as 20 ms frames, or paced silence in a silent run.

    **The speaking rule: this class owns the crossing to the sender (#365, ADR
    0030).** Capture opens when the call attempt is registered, so echo
    cancellation warms up early, but the far side cannot hear until aiortc's
    sender first pulls — seconds later. Speaking must be fresh, so completeness
    is traded for it: everything captured before that first pull is dropped —
    judged by when a frame was captured, so audio still inside the
    echo-cancellation worker at that moment goes too — and after it the sender
    is never more than `MAX_SENDER_LAG_FRAMES` behind — the oldest frames go,
    and a sender that stalled resumes on current audio rather than a burst of
    backlog. What was dropped is counted and logged when capture stops.
    """

    def __init__(
        self,
        *,
        silent: bool,
        device: int | None,
        output: _OutputSelection,
        failed: Callable[[Exception], None],
    ) -> None:
        self._output = output
        self._worker: _CaptureWorker | None = None
        self._failed = failed
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._track: Any = None
        #: Processed frames, each with when its first sample was captured.
        self._frames: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue(
            maxsize=MAX_SENDER_LAG_FRAMES
        )
        self._dropped = 0
        #: When the sender first pulled — the moment the far side can hear.
        self._heard_from: float | None = None

    def track(self, output: _OutputIdentity | None = None) -> Any:
        from aiortc.mediastreams import AudioStreamTrack

        microphone = self

        class _Track(AudioStreamTrack):  # type: ignore[misc, valid-type]
            def __init__(self) -> None:
                super().__init__()
                self._pts = 0
                self._started: float | None = None

            async def recv(self) -> Any:
                import av

                payload = await microphone._next(self)
                frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
                frame.planes[0].update(payload)
                frame.sample_rate = SAMPLE_RATE
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
                self._pts += FRAME_SAMPLES
                return frame

        self._track = _Track()
        if not self._silent:
            self._open(output)
        return self._track

    async def _next(self, track: Any) -> bytes:
        """One frame's worth of PCM: captured, or silence paced in real time.

        Silence has to be *paced*. Handing the encoder frames as fast as it asks
        would run the media clock far ahead of the wall clock, and the far side
        would hear a call that had already ended.
        """
        if not self._silent:
            if self._heard_from is None:
                self._heard_from = time.monotonic()
            while True:
                pcm, captured_at = await self._frames.get()
                # Decided by when it was captured, not when it was handed over:
                # a frame still inside the echo-cancellation worker at the first
                # pull reaches this queue after it, and is just as stale.
                if captured_at >= self._heard_from:
                    return pcm
                self._dropped += 1
        if track._started is None:
            track._started = time.monotonic()
        delay = track._started + track._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        return b"\x00\x00" * FRAME_SAMPLES

    def _open(self, output: _OutputIdentity | None) -> None:
        import sounddevice

        loop = asyncio.get_event_loop()

        def push(data: bytes, captured_at: float) -> None:
            if self._frames.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._frames.get_nowait()
                self._dropped += 1
            self._frames.put_nowait((data, captured_at))

        def failed(error: Exception) -> None:
            loop.call_soon_threadsafe(self._failed, error)

        def deliver(pcm: bytes, captured_at: float) -> None:
            loop.call_soon_threadsafe(push, pcm, captured_at)

        def captured(indata: Any, _frames: int, timing: Any, status: Any) -> None:
            worker = self._worker
            if worker is None:
                return
            if status:
                _log.info("microphone reported %s", status)
            # PortAudio's currentTime and ADC timestamp share a clock. Translate
            # that measured age to the same monotonic clock as the process tap.
            at = time.monotonic() - (timing.currentTime - timing.inputBufferAdcTime)
            worker.capture(bytes(indata), at)

        try:
            if output is None:
                raise TransportError("capture has no resolved playback device")
            self._worker = _CaptureWorker(output, deliver, failed)
            self._stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=SAMPLE_FORMAT,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=captured,
            )
            self._stream.start()
            self._output.active = output
        except Exception as unavailable:
            self.stop()
            raise TransportError(f"the microphone could not be opened: {unavailable}") from None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        self._output.active = None
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.close()
        track, self._track = self._track, None
        if track is not None:
            with contextlib.suppress(Exception):
                track.stop()
        if self._dropped:
            _log.info(
                "dropped %d captured frames: captured before the far side could hear, "
                "or behind a lagging sender",
                self._dropped,
            )


class _Speaker:
    """What the call says, played out — or counted, in a silent run.

    **The listening rule: this class owns the crossing to the device (#365, ADR
    0030).** Inbound audio arrives on the network's clock; the device takes a
    block on its own. Listening must be continuous, so delay is traded for it:

    - *Headroom is measured.* Each frame's lateness is how far behind the
      stream's own pace it arrived, and the headroom is the widest lateness seen
      above the earliest, capped at `MAX_HEADROOM_SECONDS` — the delay limit.
    - *It changes only where it cannot be heard.* Silence is inserted or shed
      only between a device block that ended at zero and a whole frame of
      digital silence, which is what the far side's generated silence decodes
      to and what speech never contains. No level is picked: zero is not a
      threshold.
    - *A dry spell is concealed, never cut.* A block the buffer cannot fill fades
      to zero across itself, and playback waits to refill to the headroom — or
      for that long, if nothing more comes — then fades back in.
    - *The two-second ceiling stays.* Overflow still drops the oldest audio.

    Headroom is held as buffered audio, so a Playout span begun while it is
    held already counts it (ADR 0027's rule, unchanged in meaning).

    **The drain rule, decided from run data (#301).** A span of the Voice's
    playout is over when the audio the Voice had generated has had time to be
    heard: the bytes this speaker was still holding when the span began,
    divided by `PLAYBACK_BYTES_PER_SECOND`. That duration is known at the
    span's start, so `playback_drained` awaits it rather than polling for it,
    and its bound is a last resort behind it — one that `MAX_PLAYBACK_BYTES`
    puts out of reach at the shipped wait, which is what a last resort should
    be.

    **What this leaves uncounted, on purpose.** Audio the device callback has
    already taken out of `_buffer` is not yet audible, so a span closes by
    roughly the device's own block and latency — a frame or so — before the
    last sample is heard. #301 measured the buffer rather than what the device
    consumed because the device's callback is not drivable at this module's
    test seam, and named the span-start snapshot as the place to widen if a
    real call is ever heard to lose its last syllable.

    **Nothing about the far side decides it any more.** Two rules used to, and
    the engine log falsified both across 91 spans and 82 sessions:

    - *The server's word* (`output_audio_buffer.stopped`, read off the events
      data channel) was made the rule by #235 on the strength of the OpenAI
      Realtime API. It fired **0** times. That whole event family does not
      exist on the codex app-server wire — which this repo's own research had
      already established on 2026-09-01, four days before #235 installed it.
    - *Inbound audio going quiet* closed 84 spans and cannot close the other 7.
      A peer that keeps streaming silence after the Voice has finished never
      goes quiet, and this backend sometimes does exactly that.

    **The buffer-empty gate went with them**, and that is the load-bearing
    half. The old check was `buffer empty AND (server's word OR quiet)`, and
    across all 91 spans the gate and the quiet rule were perfectly correlated:
    every clean close reported 0 bytes buffered, every stall 1920 or 3840 — the
    standing backlog of a continuous stream, which never reaches zero. Left in
    place as an AND it would stop the new rule firing at all. Removing it is
    safe for the reason the rule exists: the span's duration already accounts
    for every byte the span began with, and bytes arriving after the model said
    its turn was done are not the Voice speaking.

    **The one assumption**, and how to check it: no real speech audio arrives
    after the turn is done. On 2026-09-09 the Voice spoke ≈1359 frames, the
    span carried 10,395, and the 180 s stall accounts for 9,000 of them at
    exactly 50 frames a second — a peer generating silence live, not one
    flushing buffered speech. A short stretch of real speech in the first few
    hundred milliseconds after `turn.done` is what this does not rule out, and
    `Playout.started_with_bytes` is where a log would show it.
    """

    def __init__(self, *, silent: bool, device: int | None) -> None:
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._task: asyncio.Task[None] | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._dropped = 0
        #: When the last inbound frame arrived. `None` while none has. Since
        #: #301 this is reported and never consulted: what closes a span is
        #: the audio this side holds, not when the far side last sent.
        self._last_frame_at: float | None = None
        #: The window `take_playout` reports and then clears. All three are
        #: touched only from the receive loop and read only from
        #: `playback_drained`, which runs on the same event loop — so unlike
        #: `_buffer`, which a device callback thread also drains, they need no
        #: lock. `_lock` guards the buffer and nothing else.
        self._window_frames = 0
        self._window_largest_gap: float | None = None
        #: The frame the next gap is measured from, cleared at every window
        #: boundary — which is what keeps `_window_largest_gap` a gap *between
        #: two frames of this window* and not the idle since the last one.
        self._window_gap_from: float | None = None
        #: How much audio was queued for the device when this span began, and
        #: so how long the span lasts (#301). Set by `begin_span`, reported
        #: with the window, and cleared with it.
        self._window_started_with = 0
        #: The listening rule's state (#365). The receive loop writes the first
        #: three and the device callback reads them, so they are under `_lock`
        #: with the buffer; the rest belong to one side each and need no lock.
        #: `_headroom` is bytes. `_pace` is when the first frame would have
        #: arrived had every frame kept the recent earliest pace, and
        #: `_arrived_samples` how much audio has arrived since — together, how
        #: much audio is owed right now but still in flight.
        self._headroom = 0
        self._pace: float | None = None
        self._arrived_samples = 0
        #: Receive loop: when the first frame arrived, and the recent frames'
        #: lateness as a sliding-window minimum (arrival, lateness), oldest first.
        self._first_arrived_at: float | None = None
        self._recent: collections.deque[tuple[float, float]] = collections.deque()
        #: Device callback: whether playback is waiting for the buffer to refill,
        #: since when there has been audio to wait on, and the last sample played.
        self._refilling = True
        self._refill_since: float | None = None
        self._last_sample = 0

    def begin_span(self) -> float:
        """Open a span of playout, and say how long its audio takes to be heard.

        Called once, by `playback_drained`, at the moment the Voice stopped
        generating — so what is still queued right now is the whole of what it
        said that the user has yet to hear. Everything that arrives afterwards
        is the peer padding its stream, and is not this span's audio.

        A silent run holds nothing, which answers zero — correctly, not as a
        stand-in: there is no speaker for audio to trail in.
        """
        with self._lock:
            self._window_started_with = len(self._buffer)
        return self._window_started_with / PLAYBACK_BYTES_PER_SECOND

    @property
    def playout(self) -> Playout:
        """What the open window holds right now, taking nothing from it.

        Reading and closing are separate so that reading is safe: a second
        caller — part 2 of #230 will want one — must be able to look without
        silently emptying the window the stop edge is about to report.

        `since_last_frame_seconds` deliberately spans windows: a window with no
        frames in it at all still has an answer, and "nothing has arrived for
        four minutes" is exactly the reading that matters there.
        """
        with self._lock:
            buffered = len(self._buffer)
        now = time.monotonic()
        return Playout(
            started_with_bytes=self._window_started_with,
            frames=self._window_frames,
            since_last_frame_seconds=(
                None if self._last_frame_at is None else now - self._last_frame_at
            ),
            largest_gap_seconds=self._window_largest_gap,
            buffered_bytes=buffered,
        )

    def take_playout(self) -> Playout:
        """The window just ended, and the start of the next one (#230).

        `playback_drained` calls this exactly once per wait, so a window is one
        stretch of the Voice speaking and two of them on one call are directly
        comparable — which is the whole point, since the question a reader
        brings to these numbers is why *this* stretch stalled when the last one
        did not. A cumulative count over a call could not answer it: by the
        third stall the totals are dominated by the audio that played correctly.
        """
        taken = self.playout
        self._window_frames = 0
        self._window_largest_gap = None
        self._window_gap_from = None
        self._window_started_with = 0
        return taken

    def open(self) -> _OutputIdentity:
        """Open playback and resolve the native identity from that actual stream."""
        self._open()
        return _output_identity(self._stream)

    def attach(self, track: Any) -> None:
        if not self._silent and self._stream is None:
            self._open()
        self._task = asyncio.ensure_future(self._playing(track))

    async def _playing(self, track: Any) -> None:
        import av

        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        while True:
            try:
                frame = await track.recv()
            except Exception:
                return
            arrived = time.monotonic()
            if self._window_gap_from is not None:
                # Between two frames of *this* window, never across its opening
                # edge. The interval from the previous window's last frame to
                # this one's first is the user's whole turn — several seconds of
                # perfectly correct silence — and letting it in would make it
                # the maximum on every healthy call, burying the 1.4s hole this
                # measurement exists to find (#230).
                gap = arrived - self._window_gap_from
                if self._window_largest_gap is None or gap > self._window_largest_gap:
                    self._window_largest_gap = gap
            self._window_gap_from = arrived
            self._window_frames += 1
            self._last_frame_at = arrived
            if self._silent:
                continue
            headroom, pace = self._lateness(arrived)
            for out in resampler.resample(frame):
                # Only the first `samples * SAMPLE_BYTES` bytes are real audio;
                # the rest of the plane is padding, and padding is audible static.
                chunk = bytes(out.planes[0])[: out.samples * SAMPLE_BYTES]
                with self._lock:
                    self._headroom = max(self._headroom, headroom)
                    self._pace = pace
                    self._arrived_samples += out.samples
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - MAX_PLAYBACK_BYTES
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._dropped += 1

    def _lateness(self, arrived: float) -> tuple[int, float]:
        """The headroom this frame's arrival calls for, in bytes, and the pace it keeps (#365).

        A frame's lateness is how far behind the stream's own pace it arrived:
        the first frame's arrival plus the audio that has arrived since. What
        calls for headroom is lateness above the earliest recent frame, so a far
        side that runs steadily fast or slow reads as none until it varies.

        *Recent* is the ceiling plus one frame: a hold the headroom can cover
        is caught up by then, so lateness that has lasted longer is the pace
        moving — a far side that paused — and the pace follows it rather than
        charging it as owed audio forever. Capped at the delay limit.
        """
        if self._first_arrived_at is None:
            self._first_arrived_at = arrived
        lateness = arrived - self._first_arrived_at - self._arrived_samples / SAMPLE_RATE
        recent = self._recent
        # Windowed from the frame before this one, not from this one: a hold
        # longer than the window must still be measured against the frames
        # that arrived before it, or the longest holds would read as none.
        previous = recent[-1][0] if recent else arrived
        while recent and recent[0][0] < previous - MAX_HEADROOM_SECONDS - FRAME_SECONDS:
            recent.popleft()
        while recent and recent[-1][1] >= lateness:
            recent.pop()
        recent.append((arrived, lateness))
        earliest = recent[0][1]
        wanted = min(lateness - earliest, MAX_HEADROOM_SECONDS)
        return round(wanted * SAMPLE_RATE) * SAMPLE_BYTES, self._first_arrived_at + earliest

    def _fill_block(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        """One block for the device, under the listening rule. See the class."""
        need = frames * SAMPLE_BYTES
        with self._lock:
            block, rising, falling = self._take(need)
        if rising or falling:
            block = self._faded(block, need, rising=rising, falling=falling)
        outdata[:need] = block
        self._last_sample = int.from_bytes(block[-SAMPLE_BYTES:], "little", signed=True)

    def _take(self, need: int) -> tuple[bytes, bool, bool]:
        """What the buffer gives this block, and whether it fades in, out, or both.

        Under `_lock`. Adjusts the headroom only at digital silence, refills
        after a dry spell, and never hands back more or less than `need` bytes
        except a short block, which the caller conceals.
        """
        buffer = self._buffer
        now = time.monotonic()
        target = need + self._headroom
        # What the device can count on: the audio held, plus the audio the
        # recent pace says is owed but still in flight — up to the headroom,
        # which is all the lateness this side ever covers. Unlike the buffer
        # alone this does not jump by a frame with each arrival's phase, so the
        # headroom is set against the pace and not against whichever frame
        # happened to be late at the moment of looking.
        owed = 0
        if self._pace is not None:
            due = round((now - self._pace) * SAMPLE_RATE) - self._arrived_samples
            owed = min(max(due, 0) * SAMPLE_BYTES, self._headroom)
        rising = False
        if self._refilling:
            if not buffer:
                self._refill_since = None
                return bytes(need), False, False
            if self._refill_since is None:
                self._refill_since = now
            waited = now - self._refill_since
            if len(buffer) + owed < target and waited < self._headroom / PLAYBACK_BYTES_PER_SECOND:
                return bytes(need), False, False
            self._refilling = False
            self._refill_since = None
            rising = True
        inserted = 0
        if (
            self._last_sample == 0
            and len(buffer) >= FRAME_BYTES
            and buffer.count(0, 0, FRAME_BYTES) == FRAME_BYTES
        ):
            available = len(buffer) + owed
            if available < target:
                inserted = min(need, target - available)
            elif available > target:
                excess = bytes(buffer[: min(available - target, len(buffer))])
                silent = len(excess) - len(excess.lstrip(b"\x00"))
                del buffer[: silent - silent % SAMPLE_BYTES]
        taken = min(need - inserted, len(buffer))
        block = bytes(inserted) + bytes(buffer[:taken])
        del buffer[:taken]
        falling = len(block) < need
        if falling:
            self._refilling = True
        return block, rising, falling

    def _faded(self, block: bytes, need: int, *, rising: bool, falling: bool) -> bytes:
        """Conceal a block's edges: a linear ramp across the whole block (#365).

        A block is 20 ms, long enough that the ramp is inaudible as a step and
        short enough to be the one block that was going to be wrong anyway.
        What a short block lacks holds its last sample, so the fade out starts
        from where the audio actually was rather than from zero.
        """
        samples = array.array("h", block)
        count = need // SAMPLE_BYTES
        held = samples[-1] if samples else self._last_sample
        samples.extend([held] * (count - len(samples)))
        for index in range(count):
            gain = 1.0
            if rising:
                gain *= index / count
            if falling:
                gain *= (count - 1 - index) / count
            samples[index] = round(samples[index] * gain)
        return samples.tobytes()

    def _open(self) -> None:
        import sounddevice

        try:
            self._stream = sounddevice.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=SAMPLE_FORMAT,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=self._fill_block,
            )
            self._stream.start()
        except Exception as unavailable:
            raise TransportError(f"the speaker could not be opened: {unavailable}") from None

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        if self._dropped:
            _log.info("dropped playback audio %d times while the buffer overflowed", self._dropped)


class _EchoProcessor:
    """One call's canceller; only the audio worker crosses this private seam.

    Reference PCM is resampled device playback, never a second copy of Voice or
    Cues. Capture stays mono s16 / 20 ms. Both timestamps name the first sample
    on the monotonic clock. The APM's ten-millisecond framing and measured delay
    stay here, so neither signalling nor the encoder learns about them.
    """

    def __init__(self, *, reference_channels: int) -> None:
        from livekit import rtc

        self._apm = rtc.AudioProcessingModule(echo_cancellation=True)
        self._channels = reference_channels
        self._reference = bytearray()
        self._render_offset: float | None = None

    def reference(self, pcm: bytes, *, rendered_at: float, analyzed_at: float) -> bool:
        from livekit import rtc

        size = APM_BYTES * self._channels
        buffered_seconds = len(self._reference) / (SAMPLE_RATE * SAMPLE_BYTES * self._channels)
        self._reference.extend(pcm)
        first = rendered_at - buffered_seconds
        while len(self._reference) >= size:
            frame = rtc.AudioFrame(
                bytes(self._reference[:size]), SAMPLE_RATE, self._channels, APM_SAMPLES
            )
            del self._reference[:size]
            self._apm.process_reverse_stream(frame)
            self._render_offset = first - analyzed_at
            first += APM_SAMPLES / SAMPLE_RATE
        return self._render_offset is not None

    def capture(self, pcm: bytes, *, captured_at: float, processed_at: float) -> bytes:
        from livekit import rtc

        if self._render_offset is None:
            raise TransportError("the playback reference has not started")
        if len(pcm) != FRAME_SAMPLES * SAMPLE_BYTES:
            raise TransportError("capture did not supply one 20 ms frame")
        size = APM_BYTES
        result = bytearray()
        for offset in range(0, len(pcm), size):
            # LiveKit: (t_render - t_analyze) + (t_process - t_capture).
            delay = (
                self._render_offset
                + processed_at
                - captured_at
                - offset / (SAMPLE_RATE * SAMPLE_BYTES)
            )
            self._apm.set_stream_delay_ms(max(0, round(delay * 1000)))
            frame = rtc.AudioFrame(pcm[offset : offset + size], SAMPLE_RATE, CHANNELS, APM_SAMPLES)
            self._apm.process_stream(frame)
            result.extend(frame.data)
        return bytes(result)


@dataclass(frozen=True)
class _OutputIdentity:
    index: int
    uid: str
    stream: int
    render_latency: float


class _OutputSelection:
    """Cues share the resolved device only for the lifetime of capture."""

    def __init__(self, configured: int | None) -> None:
        self.configured = configured
        self.active: _OutputIdentity | None = None

    @property
    def device(self) -> int | None:
        active = self.active
        return self.configured if active is None else active.index


def _core_property(device: int, selector: int, scope: int | None = None) -> bytes:
    import CoreAudio

    address = CoreAudio.AudioObjectPropertyAddress(
        selector,
        CoreAudio.kAudioObjectPropertyScopeGlobal if scope is None else scope,
        CoreAudio.kAudioObjectPropertyElementMain,
    )
    status, size = CoreAudio.AudioObjectGetPropertyDataSize(device, address, 0, b"", None)
    _audio_status(status, "read the audio property size")
    status, _, data = CoreAudio.AudioObjectGetPropertyData(device, address, 0, b"", size, None)
    _audio_status(status, "read the audio property")
    return bytes(data)


def _audio_status(status: int, operation: str) -> None:
    if status:
        raise TransportError(
            f"could not {operation} (Core Audio status {status}); check the selected audio "
            "device and allow GPT-VoiceCoding in System Settings > Privacy & Security > "
            "Screen & System Audio Recording"
        )


def _output_identity(stream: Any) -> _OutputIdentity:
    """Ask PortAudio which native device this actual stream opened; never match names."""
    import ctypes
    import struct

    import CoreAudio
    import objc
    import sounddevice

    portaudio = ctypes.CDLL(sounddevice._libname)
    native_device = portaudio.PaMacCore_GetStreamOutputDevice
    native_device.argtypes = [ctypes.c_void_p]
    native_device.restype = ctypes.c_uint32
    device = native_device(int(sounddevice._ffi.cast("uintptr_t", stream._ptr)))
    if not device:
        raise TransportError("PortAudio did not identify the selected Core Audio output")
    uid_pointer = struct.unpack(
        "P", _core_property(device, CoreAudio.kAudioDevicePropertyDeviceUID)
    )[0]
    uid = str(objc.objc_object(c_void_p=uid_pointer))
    scope = CoreAudio.kAudioObjectPropertyScopeOutput
    stream_ids = _core_property(device, CoreAudio.kAudioDevicePropertyStreams, scope)
    streams = list(enumerate(value[0] for value in struct.iter_unpack("I", stream_ids)))
    if len(streams) != 1:
        # A single device-targeted tap must cover the complete device. Selecting
        # one of several streams would silently exclude other applications.
        raise TransportError(
            "cannot establish a complete playback reference for this multi-stream output"
        )
    stream_index, native_stream = streams[0]
    rate = struct.unpack(
        "d", _core_property(device, CoreAudio.kAudioDevicePropertyNominalSampleRate)
    )[0]
    latency = (
        sum(
            struct.unpack("I", _core_property(owner, selector, property_scope))[0]
            for owner, selector, property_scope in (
                (device, CoreAudio.kAudioDevicePropertyLatency, scope),
                (device, CoreAudio.kAudioDevicePropertySafetyOffset, scope),
                (native_stream, CoreAudio.kAudioStreamPropertyLatency, None),
            )
        )
        / rate
    )
    return _OutputIdentity(
        index=int(stream.device), uid=uid, stream=stream_index, render_latency=latency
    )


@dataclass(frozen=True)
class _ReferenceBlock:
    planes: tuple[bytes, ...]
    at: float


class _SystemPlayback:
    """Device-targeted, private, unmuted Core Audio tap; copies only in its callback."""

    def __init__(
        self,
        output: _OutputIdentity,
        receive: Callable[[_ReferenceBlock], None],
        failed: Callable[[Exception], None],
    ) -> None:
        import ctypes
        import ctypes.util
        import struct
        import uuid
        import warnings

        import CoreAudio
        import objc

        self._tap: int | None = None
        self._aggregate: int | None = None
        self._proc: Any = None
        self._callback: Any = None
        self._hal = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        # PyObjC 12.2.2's ^? metadata cannot consume the IOProcID it returns.
        # These are the same Core Audio operations with their SDK signatures.
        for name in ("AudioDeviceStart", "AudioDeviceStop", "AudioDeviceDestroyIOProcID"):
            function = getattr(self._hal, name)
            function.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            function.restype = ctypes.c_int32

        class Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        timebase = Timebase()
        _audio_status(
            ctypes.CDLL(None).mach_timebase_info(ctypes.byref(timebase)), "read the audio clock"
        )
        seconds_per_tick = timebase.numer / timebase.denom / 1_000_000_000
        description = (
            CoreAudio.CATapDescription.alloc().initExcludingProcesses_andDeviceUID_withStream_(
                [], output.uid, output.stream
            )
        )
        description.setPrivate_(True)
        description.setMuteBehavior_(CoreAudio.CATapUnmuted)
        try:
            status, tap = CoreAudio.AudioHardwareCreateProcessTap(description, None)
            _audio_status(status, "create the system playback tap")
            self._tap = int(tap)
            raw = _core_property(self._tap, CoreAudio.kAudioTapPropertyFormat)
            rate, kind, flags, _, _, self._bytes_per_frame, channels, bits, _ = struct.unpack(
                "dIIIIIIII", raw
            )
            if kind != CoreAudio.kAudioFormatLinearPCM or channels not in (1, 2):
                raise TransportError("the selected playback tap must supply mono or stereo PCM")
            self.rate = int(rate)
            self.channels = channels
            self.layout = "mono" if channels == 1 else "stereo"
            if flags & CoreAudio.kAudioFormatFlagIsFloat and bits in (32, 64):
                self.format = "flt" if bits == 32 else "dbl"
            elif flags & CoreAudio.kAudioFormatFlagIsSignedInteger and bits in (16, 32):
                self.format = "s16" if bits == 16 else "s32"
            else:
                raise TransportError("the selected playback tap has an unsupported PCM format")
            if flags & CoreAudio.kAudioFormatFlagIsBigEndian:
                raise TransportError("the selected playback tap uses unsupported big-endian PCM")
            if flags & CoreAudio.kAudioFormatFlagIsNonInterleaved:
                self.format += "p"
            config = {
                CoreAudio.kAudioAggregateDeviceNameKey.decode(): "GPT-VoiceCoding echo reference",
                CoreAudio.kAudioAggregateDeviceUIDKey.decode(): str(uuid.uuid4()),
                CoreAudio.kAudioAggregateDeviceIsPrivateKey.decode(): True,
                CoreAudio.kAudioAggregateDeviceTapAutoStartKey.decode(): True,
                CoreAudio.kAudioAggregateDeviceTapListKey.decode(): [
                    {
                        CoreAudio.kAudioSubTapUIDKey.decode(): str(description.UUID().UUIDString()),
                        CoreAudio.kAudioSubTapDriftCompensationKey.decode(): True,
                    }
                ],
            }
            status, aggregate = CoreAudio.AudioHardwareCreateAggregateDevice(config, None)
            _audio_status(status, "create the playback reference input")
            self._aggregate = int(aggregate)

            @objc.callbackFor(CoreAudio.AudioDeviceCreateIOProcID)
            def copied(
                _device: Any,
                _now: Any,
                inputs: Any,
                stamp: Any,
                _outputs: Any,
                _output_stamp: Any,
                _context: Any,
            ) -> int:
                try:
                    if not stamp.mFlags & CoreAudio.kAudioTimeStampHostTimeValid:
                        raise TransportError("the playback reference has no hardware timestamp")
                    planes = tuple(bytes(buffer.mData) for buffer in inputs)
                    if planes and planes[0]:
                        receive(
                            _ReferenceBlock(
                                planes, stamp.mHostTime * seconds_per_tick + output.render_latency
                            )
                        )
                except Exception as error:
                    failed(error)
                return 0

            self._callback = copied
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", objc.ObjCPointerWarning)
                status, self._proc = CoreAudio.AudioDeviceCreateIOProcID(
                    self._aggregate, copied, None, None
                )
            _audio_status(status, "register the playback reference callback")
            _audio_status(
                self._hal.AudioDeviceStart(self._aggregate, self._proc.pointerAsInteger),
                "start system audio capture",
            )
        except BaseException:
            self.close()
            raise

    def convert(self, block: _ReferenceBlock, resampler: Any) -> list[tuple[bytes, float]]:
        import av

        count = len(block.planes[0]) // self._bytes_per_frame
        frame = av.AudioFrame(format=self.format, layout=self.layout, samples=count)
        frame.sample_rate = self.rate
        # Preserve the hardware timeline through resampling, including its filter delay.
        frame.time_base = fractions.Fraction(1, self.rate)
        frame.pts = round(block.at * self.rate)
        for plane, data in zip(frame.planes, block.planes, strict=True):
            plane.update(data)
        return [
            (
                bytes(out.planes[0])[: out.samples * self.channels * SAMPLE_BYTES],
                float(out.pts * out.time_base),
            )
            for out in resampler.resample(frame)
        ]

    def close(self) -> None:
        import CoreAudio

        if self._proc is not None:
            for name in ("AudioDeviceStop", "AudioDeviceDestroyIOProcID"):
                status = getattr(self._hal, name)(self._aggregate, self._proc.pointerAsInteger)
                if status:
                    _log.warning("%s returned Core Audio status %s", name, status)
            self._proc = None
        if self._aggregate is not None:
            status = CoreAudio.AudioHardwareDestroyAggregateDevice(self._aggregate)
            if status:
                _log.warning("destroying the playback aggregate returned %s", status)
            self._aggregate = None
        if self._tap is not None:
            status = CoreAudio.AudioHardwareDestroyProcessTap(self._tap)
            if status:
                _log.warning("destroying the playback tap returned %s", status)
            self._tap = None
        self._callback = None


@dataclass(frozen=True)
class _CapturedBlock:
    pcm: bytes
    at: float


class _CaptureWorker:
    """Bounded callback handoff, one serialized APM, and processed capture only."""

    def __init__(
        self,
        output: _OutputIdentity,
        deliver: Callable[[bytes, float], None],
        failed: Callable[[Exception], None],
        *,
        source_factory: Callable[..., _SystemPlayback] = _SystemPlayback,
    ) -> None:
        import queue

        self._queue: queue.Queue[_ReferenceBlock | _CapturedBlock] = queue.Queue(
            maxsize=MAX_CAPTURE_FRAMES * 2
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._deliver = deliver
        self._failed = failed
        self._tap = source_factory(output, self._put, self._fail)
        self._thread = threading.Thread(
            target=self._run, name="call-echo-cancellation", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(MAX_CAPTURE_FRAMES * FRAME_SECONDS) or self._error is not None:
            self.close()
            raise TransportError(
                "system audio capture did not become ready: "
                f"{self._error or 'no reference frames'}; "
                "allow GPT-VoiceCoding in Screen & System Audio Recording"
            )

    def _fail(self, error: Exception) -> None:
        self._error = error
        self._ready.set()

    def _put(self, block: _ReferenceBlock | _CapturedBlock) -> None:
        import queue

        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            # Dropping reference frames would knowingly invalidate the canceller.
            self._fail(TransportError("audio processing fell behind its bounded buffer"))

    def capture(self, pcm: bytes, at: float) -> None:
        self._put(_CapturedBlock(pcm, at))

    def _run(self) -> None:
        import queue

        import av

        try:
            processor = _EchoProcessor(reference_channels=self._tap.channels)
            resampler = av.AudioResampler(format="s16", layout=self._tap.layout, rate=SAMPLE_RATE)
            last_reference = time.monotonic()
            while not self._stop.is_set():
                if self._error is not None:
                    raise self._error
                try:
                    block = self._queue.get(timeout=FRAME_SECONDS)
                except queue.Empty:
                    block = None
                now = time.monotonic()
                if now - last_reference > MAX_CAPTURE_FRAMES * FRAME_SECONDS:
                    raise TransportError("the system playback reference stopped arriving")
                if isinstance(block, _ReferenceBlock):
                    for pcm, at in self._tap.convert(block, resampler):
                        if processor.reference(pcm, rendered_at=at, analyzed_at=time.monotonic()):
                            self._ready.set()
                    last_reference = now
                elif isinstance(block, _CapturedBlock):
                    self._deliver(
                        processor.capture(
                            block.pcm, captured_at=block.at, processed_at=time.monotonic()
                        ),
                        block.at,
                    )
        except Exception as error:
            self._fail(error)
            if not self._stop.is_set():
                self._failed(error)

    def close(self) -> None:
        self._stop.set()
        self._tap.close()
        self._thread.join()
