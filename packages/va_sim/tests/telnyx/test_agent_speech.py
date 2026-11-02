"""Agent speech detection of the caller runner: the RMS threshold and turn/silence bookkeeping."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from va_sim.audio.codec import ulaw_decode
from va_sim.telnyx.agent_speech import AGENT_SPEECH_LEVEL, SpeechDetector, is_speech

ASSETS = Path(__file__).resolve().parents[4] / "assets" / "audio"


def dc_frame(level: int) -> bytes:
    return bytes(np.full(160, level, dtype="<i2").tobytes())


def sine_frame(rms: float) -> bytes:
    t = np.arange(160) / 8000
    return bytes(np.rint(rms * np.sqrt(2) * np.sin(2 * np.pi * 400 * t)).astype("<i2").tobytes())


def test_the_default_static_background_bed_is_not_speech() -> None:
    static = ulaw_decode((ASSETS / "static.ulaw").read_bytes())
    frames = static[: static.size // 160 * 160].reshape(-1, 160)
    assert not any(is_speech(frame.astype("<i2").tobytes()) for frame in frames)


def feed(detector: SpeechDetector, pattern: str) -> None:
    """``s`` = one speech frame, ``.`` = one silent frame."""
    for symbol in pattern:
        detector.feed(symbol == "s")


def test_threshold_is_frame_rms() -> None:
    assert AGENT_SPEECH_LEVEL == 1024
    assert not is_speech(dc_frame(1023))
    assert not is_speech(dc_frame(-1024))
    assert is_speech(dc_frame(1025))
    assert is_speech(dc_frame(-1025))
    assert not is_speech(sine_frame(1000))
    assert is_speech(sine_frame(1050))
    assert not is_speech(b"")


def test_turn_then_silence() -> None:
    detector = SpeechDetector(gap_ms=100)
    feed(detector, "sssss")
    assert (detector.turn_ms, detector.silence_ms) == (100, 0)
    feed(detector, ".....")
    assert (detector.turn_ms, detector.silence_ms) == (100, 100)


def test_short_gaps_stay_inside_the_turn() -> None:
    detector = SpeechDetector(gap_ms=100)
    feed(detector, "sss....sss")
    assert (detector.turn_ms, detector.silence_ms) == (120, 0)


def test_speech_after_a_long_gap_starts_a_new_turn() -> None:
    detector = SpeechDetector(gap_ms=100)
    feed(detector, "ss.....s")
    assert (detector.turn_ms, detector.silence_ms) == (20, 0)


def test_zero_gap_keeps_only_contiguous_speech() -> None:
    detector = SpeechDetector(gap_ms=0)
    feed(detector, "sss")
    assert detector.turn_ms == 60
    feed(detector, ".ss")
    assert detector.turn_ms == 40


def test_leading_silence_is_counted() -> None:
    detector = SpeechDetector(gap_ms=700)
    feed(detector, "...")
    assert (detector.turn_ms, detector.silence_ms) == (0, 60)
