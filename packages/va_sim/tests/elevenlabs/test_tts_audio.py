"""Deterministic TTS audio: per-format durations, tones, levels, 200 ms chunking, alignment, silent mp3."""

from __future__ import annotations

import math

import numpy as np
import pytest
from tts_helpers import decode_pcm

from va_sim.audio import dbfs_peak, goertzel_power
from va_sim.elevenlabs.mp3 import frame_bytes, frame_start_ms
from va_sim.elevenlabs.tts_audio import (
    CHAR_MS,
    OUTPUT_FORMATS,
    absolute_alignment,
    char_frequency,
    chunks,
    parse_output_format,
    pcm_samples,
    synthesize,
)

PCM_FORMATS = [name for name, fmt in OUTPUT_FORMATS.items() if fmt.codec != "mp3"]


@pytest.mark.parametrize("name", PCM_FORMATS)
def test_each_character_lasts_60_ms(name: str) -> None:
    fmt = parse_output_format(name)
    audio = synthesize("hello world", fmt)
    assert len(audio) == 11 * fmt.sample_rate * CHAR_MS // 1000 * fmt.bytes_per_sample


def test_tone_frequency_follows_the_character_code() -> None:
    assert char_frequency("a") == 300 + (97 % 12) * 25 == 325
    assert char_frequency(" ") is None
    assert char_frequency("\n") is None
    samples = pcm_samples("a", 16_000).astype(np.float64)
    energy = float(np.dot(samples, samples))
    assert goertzel_power(samples, 325, 16_000) / energy > 0.95
    assert goertzel_power(samples, 350, 16_000) / energy < 0.05


def test_whitespace_is_silence_and_tones_peak_at_minus_10_dbfs() -> None:
    samples = pcm_samples("a b", 8_000)
    per_char = 8_000 * CHAR_MS // 1000
    assert not samples[per_char : 2 * per_char].any()
    assert abs(int(np.abs(samples).max()) - dbfs_peak(-10)) <= 1


def test_ulaw_decodes_to_the_same_tone() -> None:
    fmt = parse_output_format("ulaw_8000")
    decoded = decode_pcm(fmt, synthesize("z", fmt)).astype(np.float64)
    freq = char_frequency("z")
    assert freq is not None
    assert goertzel_power(decoded, freq, 8_000) / float(np.dot(decoded, decoded)) > 0.9


def test_pcm_is_little_endian_int16() -> None:
    fmt = parse_output_format("pcm_16000")
    assert np.array_equal(decode_pcm(fmt, synthesize("ab", fmt)), pcm_samples("ab", 16_000))


def test_chunks_are_200_ms_with_relative_alignment() -> None:
    fmt = parse_output_format("pcm_16000")
    pieces = chunks("abcdefg", fmt)  # 420 ms → 200, 200, 20
    piece_bytes = 16_000 * 200 // 1000 * 2
    assert [len(piece.audio) for piece in pieces] == [piece_bytes, piece_bytes, 16_000 * 20 // 1000 * 2]
    assert b"".join(piece.audio for piece in pieces) == synthesize("abcdefg", fmt)
    assert pieces[0].alignment == {
        "chars": ["a", "b", "c", "d"],
        "charStartTimesMs": [0, 60, 120, 180],
        "charDurationsMs": [60, 60, 60, 60],
    }
    assert pieces[1].alignment == {
        "chars": ["e", "f", "g"],
        "charStartTimesMs": [40, 100, 160],
        "charDurationsMs": [60, 60, 60],
    }
    assert pieces[2].alignment is None


def test_empty_text_has_no_audio() -> None:
    for fmt in OUTPUT_FORMATS.values():
        assert synthesize("", fmt) == b""
        assert chunks("", fmt) == []


def test_mp3_frames_are_valid_silent_128k_mono() -> None:
    fmt = parse_output_format("mp3_44100_128")
    audio = synthesize("x" * 10, fmt)  # 600 ms
    offset = 0
    count = 0
    while offset < len(audio):
        header = audio[offset : offset + 4]
        assert header[:2] == b"\xff\xfb"
        assert header[2] in {0x90, 0x92}
        assert header[3] == 0xC0
        size = frame_bytes(count)
        assert size in {417, 418}
        assert (header[2] == 0x92) == (size == 418)
        assert not any(audio[offset + 4 : offset + size])
        offset += size
        count += 1
    assert offset == len(audio)
    assert count == math.ceil(600 * 44.1 / 1152)


@pytest.mark.parametrize("characters", [1, 4, 17, 37, 47, 357])
def test_mp3_pieces_line_up_with_alignment(characters: int) -> None:
    fmt = parse_output_format("mp3_44100_128")
    text = "y" * characters
    pieces = chunks(text, fmt)
    assert len(pieces) == math.ceil(characters * CHAR_MS / 200)
    assert all(piece.audio for piece in pieces)
    assert b"".join(piece.audio for piece in pieces) == synthesize(text, fmt)
    assert "".join("".join(piece.alignment["chars"]) for piece in pieces if piece.alignment) == text  # type: ignore[arg-type]


def test_mp3_frame_timing_averages_128_kbps() -> None:
    total = sum(frame_bytes(index) for index in range(1000))
    seconds = frame_start_ms(1000) / 1000
    assert total * 8 / seconds == pytest.approx(128_000, rel=1e-3)


def test_absolute_alignment_in_seconds() -> None:
    assert absolute_alignment("hi!") == {
        "characters": ["h", "i", "!"],
        "character_start_times_seconds": [0.0, 0.06, 0.12],
        "character_end_times_seconds": [0.06, 0.12, 0.18],
    }


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported output_format 'opus_48000_64'"):
        parse_output_format("opus_48000_64")
