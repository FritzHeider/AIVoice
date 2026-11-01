"""Stream formats and 20 ms frame sizes (specs/07-media-pipeline.md §4 table)."""

from __future__ import annotations

import pytest

from va_voice.audio import FRAME_MS, L16_16000, PCM16LE_16000, ULAW_8000, AudioFormat


@pytest.mark.parametrize(
    ("fmt", "frame_bytes", "frame_samples", "silence"),
    [(ULAW_8000, 160, 160, 0xFF), (L16_16000, 640, 320, 0x00), (PCM16LE_16000, 640, 320, 0x00)],
)
def test_frame_sizes_match_the_spec_table(
    fmt: AudioFormat, frame_bytes: int, frame_samples: int, silence: int
) -> None:
    assert FRAME_MS == 20
    assert fmt.frame_bytes == frame_bytes
    assert fmt.frame_samples == frame_samples
    assert fmt.silence_byte == silence


def test_durations_follow_rate_and_width() -> None:
    assert ULAW_8000.bytes_per_ms == 8.0
    assert ULAW_8000.duration_ms(800) == 100.0
    assert L16_16000.bytes_per_ms == 32.0
    assert L16_16000.duration_ms(3200) == 100.0
    assert PCM16LE_16000.duration_ms(640) == 20.0
    assert PCM16LE_16000.name == "pcm_16000"
