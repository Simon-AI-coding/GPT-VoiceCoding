"""#293's bounded speech regression; generated audio only, no devices or network.

Run with the voice extra installed, macOS `say`, whisper.cpp, its small.en model
and upstream Silero v5.1.2 VAD model. Model files are explicit inputs; this script
never downloads one or changes the product's runtime dependencies. All conditions
use the same recognizer configuration, frozen before any condition runs.

    .venv/bin/python scripts/echo_capture_regression.py --model /path/ggml-small.en.bin \
        --vad-model /path/ggml-silero-v5.1.2.bin --out /tmp/echo-regression

The JSON records expected and actual transcripts for clean, unprocessed mixed,
processed mixed, unprocessed echo and processed echo. The unprocessed conditions
represent the original raw capture interface. This does not grade real acoustics,
installed-app permissions or Simon's interruption experience.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import unicodedata
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.call.realtime.webrtc import (  # noqa: E402
    FRAME_SAMPLES,
    SAMPLE_RATE,
    _EchoProcessor,
)

PHRASES = (
    "Please open the project and show the latest changes.",
    "Do not delete the backup folder.",
    "Set the timer for 23 minutes.",
)
FAR_PHRASE = "The weather is sunny today and the train leaves after breakfast."
RECOGNIZER_ARGS = ("-l", "en", "-nt", "-otxt", "-ng", "-sns", "--vad")
# Fixed synthetic acoustics, not a delay or gain used by the product.
ECHO_DELAY = 0.07
ECHO_GAIN = 0.45
FAR_RMS = 4500
NEAR_START = 5
TAIL_SECONDS = 2


def normalized(text: str) -> str:
    return "".join(
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def model_hash(path: Path) -> str:
    with path.open("rb") as model:
        return hashlib.file_digest(model, "sha256").hexdigest()


def speech(path: Path, text: str, voice: str):
    import numpy as np

    subprocess.run(
        [
            "say",
            "-v",
            voice,
            "-o",
            str(path),
            "--file-format=WAVE",
            f"--data-format=LEI16@{SAMPLE_RATE}",
            text,
        ],
        check=True,
    )
    with wave.open(str(path)) as source:
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(float)


def write_wave(path: Path, data) -> Path:
    import numpy as np

    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes(np.clip(data, -32768, 32767).astype("<i2").tobytes())
    return path


def processed(far, microphone):
    import numpy as np

    processor = _EchoProcessor(reference_channels=1)
    output = bytearray()
    for offset in range(0, len(microphone), FRAME_SAMPLES):
        at = offset / SAMPLE_RATE
        processor.reference(
            far[offset : offset + FRAME_SAMPLES].astype("<i2").tobytes(),
            rendered_at=at,
            analyzed_at=at,
        )
        output.extend(
            processor.capture(
                microphone[offset : offset + FRAME_SAMPLES].astype("<i2").tobytes(),
                captured_at=at,
                processed_at=at + ECHO_DELAY,
            )
        )
    return np.frombuffer(output, dtype="<i2")


def main() -> int:
    import numpy as np

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--vad-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    command = ["whisper-cli", "-m", str(args.model), *RECOGNIZER_ARGS, "-vm", str(args.vad_model)]
    contract = {
        "phrases": PHRASES,
        "far_phrase": FAR_PHRASE,
        "command": command,
        "model_sha256": model_hash(args.model),
        "vad_model_sha256": model_hash(args.vad_model),
        "normalization": "remove punctuation and whitespace only",
    }
    (args.out / "contract.json").write_text(json.dumps(contract, indent=2))

    def transcript(name: str, data) -> str:
        path = write_wave(args.out / f"{name}.wav", data)
        with (args.out / f"{name}.log").open("w") as log:
            subprocess.run([*command, "-f", str(path)], check=True, stdout=log, stderr=log)
        return Path(str(path) + ".txt").read_text().strip()

    far_speech = speech(args.out / "far.wav", FAR_PHRASE, "Daniel")
    results = []
    for index, phrase in enumerate(PHRASES):
        near_speech = speech(args.out / f"near-{index}.wav", phrase, "Samantha")
        samples = (NEAR_START + TAIL_SECONDS) * SAMPLE_RATE + len(near_speech)
        samples = ((samples + FRAME_SAMPLES - 1) // FRAME_SAMPLES) * FRAME_SAMPLES
        far = np.resize(far_speech, samples)
        far *= FAR_RMS / np.sqrt(np.mean(far**2))
        near = np.zeros(samples)
        start = NEAR_START * SAMPLE_RATE
        near[start : start + len(near_speech)] = near_speech
        echo = np.pad(far, (round(ECHO_DELAY * SAMPLE_RATE), 0))[:samples] * ECHO_GAIN
        clean = transcript(f"clean-{index}", near)
        raw = transcript(f"raw-mixed-{index}", near + echo)
        mixed = transcript(f"processed-mixed-{index}", processed(far, near + echo))
        result = {
            "expected": phrase,
            "clean": clean,
            "raw_mixed": raw,
            "processed_mixed": mixed,
            "passed": normalized(clean) == normalized(phrase) == normalized(mixed)
            and normalized(raw) != normalized(phrase),
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        if index == 0:
            raw_echo = transcript("raw-echo", echo)
            cancelled = transcript("processed-echo", processed(far, echo))
            silent = transcript("silence", np.zeros(samples))
            results.append(
                {
                    "raw_echo": raw_echo,
                    "processed_echo": cancelled,
                    "silent_control": silent,
                    "passed": bool(raw_echo) and not cancelled and not silent,
                }
            )
            print(json.dumps(results[-1]), flush=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
