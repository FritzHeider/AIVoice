"""WAV input for greeting audio: PCM16 only, channels averaged, resampled to the stream rate."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from va_sim.audio import goertzel_power, read_wav_pcm
from va_sim.wav import pcm16_wav


def sine(freq: float, ms: int, rate: int, amplitude: int = 8000) -> np.ndarray:
    t = np.arange(ms * rate // 1000) / rate
    return np.rint(amplitude * np.sin(2 * np.pi * freq * t)).astype("<i2")


def test_mono_round_trip(tmp_path: Path) -> None:
    samples = sine(440, 100, 8000)
    path = tmp_path / "hello.wav"
    path.write_bytes(pcm16_wav([samples.tobytes()], 8000))
    read = read_wav_pcm(path, 8000)
    assert read.dtype == np.int16
    assert np.array_equal(read, samples)


def test_stereo_is_averaged_and_resampled(tmp_path: Path) -> None:
    left = sine(500, 200, 16000)
    right = (left // 2).astype("<i2")
    path = tmp_path / "wide.wav"
    path.write_bytes(pcm16_wav([left.tobytes(), right.tobytes()], 16000))
    read = read_wav_pcm(path, 8000)
    assert read.size == 1600
    assert np.max(np.abs(read)) == pytest.approx(6000, rel=0.02)
    assert goertzel_power(read.astype(np.float64), 500, 8000) > 50 * goertzel_power(
        read.astype(np.float64), 1500, 8000
    )


def test_upsampling_keeps_the_tone(tmp_path: Path) -> None:
    path = tmp_path / "narrow.wav"
    path.write_bytes(pcm16_wav([sine(700, 100, 8000).tobytes()], 8000))
    read = read_wav_pcm(path, 16000)
    assert read.size == 1600
    assert goertzel_power(read.astype(np.float64), 700, 16000) > 50 * goertzel_power(
        read.astype(np.float64), 2000, 16000
    )


def test_rejects_eight_bit_audio(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(8000)
        writer.writeframes(bytes([128]) * 800)
    path = tmp_path / "old.wav"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match=r"old\.wav: only 16-bit PCM WAV is supported, got 8-bit"):
        read_wav_pcm(path, 8000)


def test_rejects_empty_long_and_non_wav_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(pcm16_wav([b""], 8000))
    with pytest.raises(ValueError, match=r"empty\.wav: the WAV file has no audio"):
        read_wav_pcm(empty, 8000)
    long = tmp_path / "long.wav"
    long.write_bytes(pcm16_wav([sine(440, 120, 8000).tobytes()], 8000))
    with pytest.raises(ValueError, match=r"long\.wav: the WAV file is longer than 100 ms"):
        read_wav_pcm(long, 8000, max_ms=100)
    text = tmp_path / "text.wav"
    text.write_text("not audio", encoding="utf-8")
    with pytest.raises(ValueError, match=r"text\.wav: not a PCM WAV file"):
        read_wav_pcm(text, 8000)


def test_missing_file_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_wav_pcm(tmp_path / "missing.wav", 8000)
