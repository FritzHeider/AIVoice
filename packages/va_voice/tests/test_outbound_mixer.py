"""FrameMixer: passthrough, unit-gain mixing, clipping and wire encoding (specs/07 §9, §10.2, §15)."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from va_voice.audio import (
    L16_16000,
    PCM16LE_16000,
    ULAW_8000,
    AudioFormat,
    L16ByteOrder,
    gain_for_db,
    l16_to_pcm,
    pcm_to_pcm16le,
    rms,
    rms_dbfs,
    ulaw_decode,
    ulaw_encode,
)
from va_voice.background import BackgroundLoop
from va_voice.outbound import FrameMixer


def _tone(fmt: AudioFormat, frames: int, amplitude: float, hz: float = 440.0) -> NDArray[np.int16]:
    t = np.arange(frames * fmt.frame_samples) / fmt.sample_rate
    return np.rint(amplitude * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def _noise(fmt: AudioFormat, frames: int, amplitude: float, seed: int = 5) -> NDArray[np.int16]:
    rng = np.random.default_rng(seed)
    values = rng.normal(0.0, amplitude, frames * fmt.frame_samples)
    return np.clip(np.rint(values), -32_768, 32_767).astype(np.int16)


def _frames(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_ulaw_without_background_is_passed_through() -> None:
    mixer = FrameMixer(ULAW_8000)
    frame = bytes(range(160))
    assert mixer.speech(frame) is frame
    with pytest.raises(RuntimeError, match="no background"):
        mixer.idle()
    assert not mixer.has_background
    assert mixer.format == ULAW_8000


@pytest.mark.parametrize(("order", "dtype"), [("big", ">i2"), ("little", "<i2")])
def test_pcm_without_background_is_converted_to_the_wire_order(order: L16ByteOrder, dtype: str) -> None:
    pcm = _tone(PCM16LE_16000, 1, 12_000.0)
    wire = FrameMixer(PCM16LE_16000, wire_byte_order=order).speech(pcm_to_pcm16le(pcm))
    assert np.array_equal(np.frombuffer(wire, dtype=dtype), pcm)


@pytest.mark.parametrize("order", ["big", "little"])
def test_pcm_mix_is_the_clipped_sum(order: L16ByteOrder) -> None:
    speech = _tone(PCM16LE_16000, 4, 30_000.0)
    bed = _noise(PCM16LE_16000, 4, 6_000.0)
    mixer = FrameMixer(PCM16LE_16000, background=BackgroundLoop(bed, 320), wire_byte_order=order)
    wire = b"".join(mixer.speech(frame) for frame in _frames(pcm_to_pcm16le(speech), 640))
    expected = np.clip(speech.astype(np.int32) + bed, -32_768, 32_767)
    mixed = l16_to_pcm(wire, order)
    assert np.array_equal(mixed, expected)
    assert mixed.max() == 32_767
    assert mixed.min() == -32_768


def test_ulaw_mix_stays_within_one_quantization_step() -> None:
    speech = ulaw_decode(ulaw_encode(_tone(ULAW_8000, 10, 8_000.0)))
    bed = _noise(ULAW_8000, 10, 1_000.0)
    mixer = FrameMixer(ULAW_8000, background=BackgroundLoop(bed, 160))
    wire = b"".join(mixer.speech(frame) for frame in _frames(ulaw_encode(speech), 160))
    expected = np.clip(speech.astype(np.int32) + bed, -32_768, 32_767).astype(np.int16)
    assert np.array_equal(ulaw_decode(wire), ulaw_decode(ulaw_encode(expected)))


def test_idle_frames_carry_the_background_and_share_its_position() -> None:
    bed = _noise(ULAW_8000, 3, 2_000.0)
    loop = BackgroundLoop(bed, 160)
    mixer = FrameMixer(ULAW_8000, background=loop)
    silence = bytes([0xFF]) * 160
    assert mixer.idle() + mixer.speech(silence) + mixer.idle() == ulaw_encode(bed)
    assert loop.position == 0


def test_gain_scales_the_background() -> None:
    bed = _noise(PCM16LE_16000, 50, 4_000.0)
    mixer = FrameMixer(
        PCM16LE_16000, background=BackgroundLoop(bed, 320), gain_db=-6.0, wire_byte_order="little"
    )
    wire = b"".join(mixer.idle() for _ in range(50))
    assert rms(np.frombuffer(wire, dtype="<i2")) == pytest.approx(rms(bed) * gain_for_db(-6.0), rel=1e-3)


@pytest.mark.parametrize("fmt", [ULAW_8000, PCM16LE_16000], ids=lambda f: f.name)
def test_mixed_level_is_the_power_sum(fmt: AudioFormat) -> None:
    frames = 500
    speech = _tone(fmt, frames, 32_767 * 10 ** (-20 / 20) * np.sqrt(2))
    bed = _noise(fmt, frames, 32_767 * 10 ** (-22 / 20))
    order: L16ByteOrder = "little"
    mixer = FrameMixer(fmt, background=BackgroundLoop(bed, fmt.frame_samples), wire_byte_order=order)
    encoded = ulaw_encode(speech) if fmt == ULAW_8000 else pcm_to_pcm16le(speech)
    wire = b"".join(mixer.speech(frame) for frame in _frames(encoded, fmt.frame_bytes))
    mixed = ulaw_decode(wire) if fmt == ULAW_8000 else l16_to_pcm(wire, order)
    expected = 10 * np.log10(10 ** (rms_dbfs(speech) / 10) + 10 ** (rms_dbfs(bed) / 10))
    assert rms_dbfs(mixed) == pytest.approx(expected, abs=1.0)


def test_speech_frames_must_be_whole_frames() -> None:
    with pytest.raises(ValueError, match="expected a 160-byte frame, got 159"):
        FrameMixer(ULAW_8000).speech(bytes(159))


def test_unsupported_formats_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported TTS format l16_16000"):
        FrameMixer(L16_16000)


def test_background_frames_must_match_the_format() -> None:
    with pytest.raises(ValueError, match="background frames have 160 samples, pcm_16000 needs 320"):
        FrameMixer(PCM16LE_16000, background=BackgroundLoop(np.zeros(800, dtype=np.int16), 160))
