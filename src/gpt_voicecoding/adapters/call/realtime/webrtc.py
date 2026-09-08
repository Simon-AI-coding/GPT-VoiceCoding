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
24 kHz internally. Both directions carry a bounded jitter buffer, and both drop
the *oldest* audio when it overflows: a consumer that fell behind should cost
latency, never an exception per frame, and never unbounded memory. The playback
side copies only `samples * 2` bytes out of each resampled plane — the plane's
buffer is padded, and playing the padding is audible as static. That last one
was learned from the prototype the hard way and is the sort of detail a rewrite
silently loses.
"""

from __future__ import annotations

import asyncio
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

#: How many frames the speaker must go without a new inbound one before what it
#: has is called the *last* one. Three: shorter than any pause a listener hears
#: as the end of a sentence, and longer than the jitter between two frames of one
#: continuous utterance. It bounds only the *recognition* of the end, never the
#: playout itself — the buffer being empty is the other half of the answer, and
#: both must hold. Since #235 this is the *fallback* recognition: a peer that
#: keeps streaming silence after the Voice has finished never goes quiet, and
#: the server's own word (`OutputAudioEvent.FINISHED`) closes a span there.
PLAYBACK_QUIET_FRAMES = 3

#: The label of the events data channel this adapter offers. The backend's SDP
#: expects one to be described; since #235 it is also read, for the family of
#: events above. Every *other* event this adapter acts on still arrives as a
#: JSON-RPC notification on the app-server socket.
EVENTS_CHANNEL = "realtime-events"

#: How often `playback_drained` looks: one frame, because a poll finer than the
#: thing it is measuring buys nothing but wake-ups.
PLAYBACK_POLL_SECONDS = FRAME_SECONDS

#: How much captured audio is held before the oldest is dropped. Two seconds:
#: long enough to ride out a scheduling hiccup, short enough that what is
#: eventually sent is still a reply to what was said.
MAX_CAPTURE_FRAMES = 100

#: The same bound for playback, in bytes of 16-bit mono at `SAMPLE_RATE`.
MAX_PLAYBACK_BYTES = SAMPLE_RATE * SAMPLE_BYTES * 2

#: What to install when the import fails.
INSTALL_HINT = "pip install 'gpt-voicecoding[voice]'"

#: The distributions this route cannot run without.
REQUIRED = ("aiortc", "av", "sounddevice", "CoreAudio", "livekit.rtc")


class VoiceDependencyError(Exception):
    """The voice extra is not installed, so there is no audio path to build."""


class OutputAudioEvent(enum.Enum):
    """The server's own words about a response's audio playing out (#235).

    OpenAI Realtime API server events, documented as **WebRTC/SIP only** and
    delivered on the client-created events data channel (the realtime-webrtc
    guide, "Set up data channel for sending and receiving events"). Per
    response, not per buffer: each carries the `response_id` it is about.

    `FINISHED` is `output_audio_buffer.stopped`: "Emitted when the output audio
    buffer has been completely drained on the server, and no more audio is
    forthcoming. This event is emitted after the full response data has been
    sent to the client (`response.done`)." Fields: `event_id`, `response_id`,
    `type`. `STARTED` is its opening word, `output_audio_buffer.started`.
    Verified 2026-09-05 against `openai/openai-python` main,
    `src/openai/types/realtime/realtime_server_event.py:79-113`
    (`OutputAudioBufferStarted`, `OutputAudioBufferStopped`; last changed
    2026-08-10), whose docstrings link
    https://platform.openai.com/docs/guides/realtime-conversations#client-and-server-events-for-audio-in-webrtc
    — the rendered reference at developers.openai.com surfaces the family only
    through those SDK types.

    **Whether this backend sends them is what the next run decides.** The call
    is codex's `realtime/calls` proxy (`intent=quicksilver&architecture=avas`,
    v3 vocabulary: `turn.done`, `output_audio.delta`), not the public API, and
    codex itself has no WebRTC client that reads the channel, so no source
    settles it. `_WebRtcTransport` therefore logs each event type the channel
    carries, once per call, and the stop-edge line names the fact that closed
    it — the run's engine log answers the question either way.

    `STARTED`'s one job here is to take back a `FINISHED` that arrived for the
    previous response after the quiet rule had already closed that span —
    left latched, it would close the next span the moment its buffer was
    empty. Any other event type on the channel is not this family and is
    dropped where the channel is read.
    """

    STARTED = "output_audio_buffer.started"
    FINISHED = "output_audio_buffer.stopped"


class SpanClosedBy(enum.Enum):
    """Which fact closed a span of the Voice's playout (#235).

    The value is the clause the stop-edge line carries, so the log and the code
    cannot name the same fact two ways. `SERVER` is the rule; `QUIET` is the
    fallback for a peer that never says so; a wait that ran out its bound has
    no member here, because nothing closed it.
    """

    SERVER = "the server said its audio had finished"
    QUIET = "inbound audio went quiet"


@dataclass(frozen=True)
class Playout:
    """What the speaker saw over one stretch of waiting — the four facts (#230).

    Until #235, `drained` was decided by two things this side cannot influence
    and did not report: whether inbound frames stopped for
    `PLAYBACK_QUIET_FRAMES` frames, and whether the device still has audio
    queued. So a stop edge that ran out its bound said only that it had, and
    two entirely different causes — a remote peer that keeps RTP flowing
    through silence, and an event loop starved into delivering frames in
    bursts — arrived at the same one line.

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
        # describe one. Every event this adapter *acts* on still arrives as a
        # JSON-RPC notification on the app-server socket — one stream of truth
        # for the call's course. What is read here is one family only: the
        # server's word that a response's audio has finished playing out, which
        # exists nowhere else (#235; `OutputAudioEvent`). Event types seen
        # on the channel are named in the log once each, so a run shows what
        # this backend actually sends.
        self._events_seen: set[str] = set()
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

        **Both exits say what the speaker saw** (#230). The timeout line used to
        report that a thing had not happened and nothing about why, which left
        the one measurement that distinguishes a peer still sending from a
        starved event loop unrecorded on the only run that needed it. The
        ordinary exit carries the same four facts for the same reason a control
        needs to be measured too: a stalled run is only diagnosable against what
        a clean one on this machine looks like.

        The window is closed on the way past either exit and never twice, so
        each line describes exactly the wait it ends. `Playout` renders itself,
        so this decides nothing about wording.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while (closed_by := self._speaker.closed_by) is None:
            if asyncio.get_running_loop().time() >= deadline:
                _log.info(
                    "playout had not drained %gs after the Voice stopped generating "
                    "(neither the server's word nor quiet arrived); "
                    "reporting the stop edge anyway — %s",
                    timeout_seconds,
                    self._speaker.take_playout(),
                )
                return
            await asyncio.sleep(PLAYBACK_POLL_SECONDS)
        _log.info(
            "the Voice's playout drained: %s — %s", closed_by.value, self._speaker.take_playout()
        )

    def _read_channel_event(self, message: Any) -> None:
        """One message off the events channel, parsed for the speaker (#235).

        Anything that is not a JSON object with a string `type` is not a server
        event and is dropped; a type outside `OutputAudioEvent` is dropped here
        too, so the speaker only ever hears a typed word. Each type is named in
        the log the first time this call sees it — the engine log has to show,
        on its own, whether this backend sends `OutputAudioEvent.FINISHED` at all.
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
        try:
            event = OutputAudioEvent(kind)
        except ValueError:
            return
        self._speaker.heard_from_server(event)

    def on_lost(self, handler: LostHandler) -> None:
        self._on_lost = handler

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
    """What the user says, as 20 ms frames, or paced silence in a silent run."""

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
        self._frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_CAPTURE_FRAMES)
        self._dropped = 0

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
            return await self._frames.get()
        if track._started is None:
            track._started = time.monotonic()
        delay = track._started + track._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        return b"\x00\x00" * FRAME_SAMPLES

    def _open(self, output: _OutputIdentity | None) -> None:
        import sounddevice

        loop = asyncio.get_event_loop()

        def push(data: bytes) -> None:
            if self._frames.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._frames.get_nowait()
                self._dropped += 1
            self._frames.put_nowait(data)

        def failed(error: Exception) -> None:
            loop.call_soon_threadsafe(self._failed, error)

        def deliver(pcm: bytes) -> None:
            loop.call_soon_threadsafe(push, pcm)

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
            _log.info("dropped %d captured frames while the consumer lagged", self._dropped)


class _Speaker:
    """What the call says, played out — or counted, in a silent run.

    **The drain rule, decided from run data (#235).** A span of the Voice's
    playout is over when nothing is still queued for the device *and* one of:

    1. the server said the response's audio has finished playing out
       (`OutputAudioEvent.FINISHED`, read off the events data channel) — the rule;
    2. no inbound frame has arrived for `PLAYBACK_QUIET_FRAMES` frames — the
       fallback, for a peer that never sends the word;

    and a wait that reaches neither is closed by `playback_drained`'s bound as
    the last resort, which should then almost never fire.

    Decided by runs `20260905T071849Z`, `075128Z`, `090222Z` and `092046Z`:
    the bound fired seven times, every time with 0 bytes buffered, the last
    inbound frame 2-20 ms ago and the largest gap under 0.35 s — around
    10 000 frames (≈200 s of audio) held open behind a 15-second answer. That
    is a peer that keeps streaming silence after the Voice has finished, so
    the quiet rule alone can never close the span; not a starved event loop,
    which would have shown buffered bytes and large gaps. Whether the peer pads
    its stream is the peer's business, so what closes a span is now the
    server's own word, and the quiet rule is kept for the runs — `075128Z`
    drained 13 of 13 spans on it — where the peer does stop.
    """

    def __init__(self, *, silent: bool, device: int | None) -> None:
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._task: asyncio.Task[None] | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._dropped = 0
        #: When the last inbound frame arrived. `None` while none has, which
        #: reads as drained: there is nothing playing that has not finished.
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
        #: Whether the server has said the current response's audio finished
        #: playing out (#235). Set from the events channel, taken back by the
        #: next response starting, and consumed with the window it closed.
        #: Touched from the data-channel callback and read from
        #: `playback_drained`, both on the event loop: no lock.
        self._server_finished = False

    def heard_from_server(self, event: OutputAudioEvent) -> None:
        """The server's word about the current response's audio: finished, or not yet.

        `STARTED` while a wait is still polling for the previous span erases
        nothing that matters: the Voice speaking again is what makes that wait
        stale in the adapter (`speaking_span` moves on), and the span stays
        open — correctly — until this response's own `FINISHED`.
        """
        self._server_finished = event is OutputAudioEvent.FINISHED

    @property
    def closed_by(self) -> SpanClosedBy | None:
        """Which fact says the last inbound frame has finished playing, if any.

        Nothing still queued for the device is required first: whatever the
        server or the quiet rule says, audio this side has not yet written is
        audio the user has not yet heard. A silent run buffers nothing, which
        leaves the other fact answering on its own — correctly, because there
        is no speaker for audio to trail in. No frame ever arriving reads as
        quiet: there is nothing playing that has not finished.
        """
        with self._lock:
            if self._buffer:
                return None
        if self._server_finished:
            return SpanClosedBy.SERVER
        if (
            self._last_frame_at is None
            or time.monotonic() - self._last_frame_at >= PLAYBACK_QUIET_FRAMES * FRAME_SECONDS
        ):
            return SpanClosedBy.QUIET
        return None

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
        self._server_finished = False
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
            for out in resampler.resample(frame):
                # Only the first `samples * SAMPLE_BYTES` bytes are real audio;
                # the rest of the plane is padding, and padding is audible static.
                chunk = bytes(out.planes[0])[: out.samples * SAMPLE_BYTES]
                with self._lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - MAX_PLAYBACK_BYTES
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._dropped += 1

    def _open(self) -> None:
        import sounddevice

        def wanted(outdata: Any, frames: int, _time: Any, _status: Any) -> None:
            need = frames * SAMPLE_BYTES
            with self._lock:
                available = bytes(self._buffer[:need])
                del self._buffer[: len(available)]
            outdata[: len(available)] = available
            if len(available) < need:
                outdata[len(available) :] = b"\x00" * (need - len(available))

        try:
            self._stream = sounddevice.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=SAMPLE_FORMAT,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=wanted,
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
        deliver: Callable[[bytes], None],
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
                        )
                    )
        except Exception as error:
            self._fail(error)
            if not self._stop.is_set():
                self._failed(error)

    def close(self) -> None:
        self._stop.set()
        self._tap.close()
        self._thread.join()
