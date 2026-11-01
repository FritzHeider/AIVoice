"""Committed VAD fixture WAVs (16 kHz mono PCM16) under ``packages/va_voice/tests/fixtures/vad/``.

The fixtures are generated, never recorded: speech is the simulator's formant caller speech (specs/30 §3.1,
synthesized natively at 16 kHz without the marker preamble), noise and the static gaps are slices of the
committed background assets, DTMF uses the simulator's tone generator. Everything is deterministic, so a
regeneration must be byte-identical with the committed files (``test_vad_fixtures_are_reproducible``).

Regenerate after a change: ``uv run python packages/va_voice/tests/support/vad_fixtures.py --write``.
"""

from __future__ import annotations

import argparse
import io
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio import MARKER_MS, l16be_to_pcm, synthesize_dtmf, synthesize_pcm16

__all__ = [
    "FIXTURE_DIR",
    "FIXTURE_NAMES",
    "SAMPLE_RATE",
    "SPEECH_SEGMENTS_MS",
    "build_fixtures",
    "main",
    "read_fixture",
]

SAMPLE_RATE: Final = 16_000
_REPO_ROOT: Final = Path(__file__).resolve().parents[4]
FIXTURE_DIR: Final = _REPO_ROOT / "packages" / "va_voice" / "tests" / "fixtures" / "vad"
_ASSETS: Final = _REPO_ROOT / "assets" / "audio"
FIXTURE_NAMES: Final = ("speech.wav", "silence.wav", "noise.wav", "dtmf.wav")


@dataclass(frozen=True, slots=True)
class _Utterance:
    text: str
    f0_hz: float
    seed: int


_UTTERANCES: Final = (
    _Utterance("hello there how are you", 140.0, 7),
    _Utterance("thanks for calling back", 200.0, 11),
)
_RATE_MS_PER_CHAR: Final = 70
# Static before, between, and after the two utterances.
_GAPS_MS: Final = (500, 800, 600)
_NOISE_MS: Final = 2_000
_SILENCE_MS: Final = 2_000
_DTMF_DIGITS: Final = "159#"
_DTMF_TONE_MS: Final = 120
_DTMF_GAP_MS: Final = 80


def _samples(ms: int) -> int:
    return ms * SAMPLE_RATE // 1000


def _asset(track: str, ms: int, offset_ms: int = 0) -> NDArray[np.int16]:
    raw = np.fromfile(_ASSETS / f"{track}.pcm16k", dtype="<i2")
    start = _samples(offset_ms)
    return raw[start : start + _samples(ms)].astype(np.int16)


def _body(utterance: _Utterance) -> NDArray[np.int16]:
    pcm = synthesize_pcm16(
        1,
        utterance.text,
        sample_rate=SAMPLE_RATE,
        f0_hz=utterance.f0_hz,
        rate_ms_per_char=_RATE_MS_PER_CHAR,
        seed=utterance.seed,
    )
    return pcm[_samples(MARKER_MS) :]


def _speech_layout() -> tuple[NDArray[np.int16], tuple[tuple[float, float], ...]]:
    parts: list[NDArray[np.int16]] = []
    segments: list[tuple[float, float]] = []
    cursor_ms = 0.0
    offset_ms = 0
    for gap_ms, utterance in zip(_GAPS_MS, (*_UTTERANCES, None), strict=True):
        parts.append(_asset("static", gap_ms, offset_ms))
        offset_ms += gap_ms
        cursor_ms += gap_ms
        if utterance is None:
            break
        body = _body(utterance)
        parts.append(body)
        body_ms = body.size * 1000 / SAMPLE_RATE
        segments.append((cursor_ms, cursor_ms + body_ms))
        cursor_ms += body_ms
    return np.concatenate(parts), tuple(segments)


SPEECH_SEGMENTS_MS: Final = _speech_layout()[1]
"""Truth for ``speech.wav``: (start, end) ms of each voiced body."""


def _dtmf() -> NDArray[np.int16]:
    gap = np.zeros(_samples(_DTMF_GAP_MS), dtype=np.int16)
    parts: list[NDArray[np.int16]] = [gap]
    for digit in _DTMF_DIGITS:
        parts.append(l16be_to_pcm(synthesize_dtmf(digit, _DTMF_TONE_MS, sample_rate=SAMPLE_RATE)))
        parts.append(gap)
    return np.concatenate(parts)


def _wav(pcm: NDArray[np.int16]) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.astype("<i2").tobytes())
    return buffer.getvalue()


def build_fixtures() -> dict[str, bytes]:
    """File name → WAV bytes of every fixture."""
    return {
        "speech.wav": _wav(_speech_layout()[0]),
        "silence.wav": _wav(np.zeros(_samples(_SILENCE_MS), dtype=np.int16)),
        "noise.wav": _wav(_asset("office", _NOISE_MS)),
        "dtmf.wav": _wav(_dtmf()),
    }


def read_fixture(name: str) -> NDArray[np.int16]:
    """Samples of a committed fixture (16 kHz mono PCM16)."""
    with wave.open(str(FIXTURE_DIR / name), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, SAMPLE_RATE)
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.int16)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the committed fixtures")
    mode.add_argument("--check", action="store_true", help="exit 1 when a committed fixture differs")
    args = parser.parse_args(argv)
    fixtures = build_fixtures()
    if args.write:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for name, data in fixtures.items():
            (FIXTURE_DIR / name).write_bytes(data)
        return 0
    stale = [
        name
        for name, data in fixtures.items()
        if not (FIXTURE_DIR / name).is_file() or (FIXTURE_DIR / name).read_bytes() != data
    ]
    for name in stale:
        sys.stderr.write(f"{FIXTURE_DIR / name} is stale (run with --write)\n")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
