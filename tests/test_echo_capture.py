"""The private capture-processing seam selected in #293's Agent Brief."""

import array
import math

import pytest

from gpt_voicecoding.adapters.call.realtime import webrtc


def test_capture_requires_a_playback_reference():
    pytest.importorskip("livekit.rtc")
    processor = webrtc._EchoProcessor(reference_channels=1)
    with pytest.raises(webrtc.TransportError, match="reference"):
        processor.capture(bytes(1920), captured_at=1.0, processed_at=1.02)


def test_real_canceller_removes_delayed_echo_through_capture_interface():
    pytest.importorskip("livekit.rtc")
    processor = webrtc._EchoProcessor(reference_channels=1)
    # A known changing signal, 60 ms of acoustic delay, and no near speaker.
    signal = array.array(
        "h", (int(6000 * math.sin(i * (0.03 + i / 80000000))) for i in range(48000 * 8))
    )
    echo = array.array("h", [0] * 2880)
    echo.extend(int(x * 0.45) for x in signal)
    result = array.array("h")
    for offset in range(0, len(signal), 960):
        when = offset / 48000
        processor.reference(
            signal[offset : offset + 960].tobytes(), rendered_at=when, analyzed_at=when
        )
        result.frombytes(
            processor.capture(
                echo[offset : offset + 960].tobytes(), captured_at=when, processed_at=when + 0.06
            )
        )
    raw_energy = sum(x * x for x in echo[-48000:])
    output_energy = sum(x * x for x in result[-48000:])
    assert len(result) == len(signal)
    assert output_energy < raw_energy / 100


def test_capture_worker_stops_uploading_when_its_reference_fails():
    pytest.importorskip("livekit.rtc")
    pytest.importorskip("av")
    import threading

    sources = []

    class Playback:
        channels = 1
        layout = "mono"
        closed = False

        def __init__(self, output, receive, failed):
            self.failed = failed
            sources.append(self)
            receive(webrtc._ReferenceBlock((bytes(1920),), 0))

        def convert(self, block, resampler):
            return [(block.planes[0], block.at)]

        def close(self):
            self.closed = True

    delivered = []
    failure = threading.Event()
    worker = webrtc._CaptureWorker(
        webrtc._OutputIdentity(0, "fixture-device", 0, 0),
        lambda pcm, _captured_at: delivered.append(pcm),
        lambda error: failure.set(),
        source_factory=Playback,
    )
    try:
        sources[0].failed(webrtc.TransportError("reference device disappeared"))
        assert failure.wait(1)
        worker.capture(bytes(1920), 0)
    finally:
        worker.close()
    assert not delivered
    assert sources[0].closed
