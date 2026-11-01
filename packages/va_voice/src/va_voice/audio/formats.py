"""Audio stream formats and the 20 ms frame size of each (specs/07-media-pipeline.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from va_voice.audio.codec import ULAW_SILENCE

__all__ = ["FRAME_MS", "L16_16000", "PCM16LE_16000", "ULAW_8000", "AudioFormat"]

FRAME_MS: Final = 20
"""Every media frame on the pipeline carries 20 ms of audio."""

_MS_PER_SECOND: Final = 1000


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """A mono stream format: sample rate, bytes per sample and the byte value of codec silence."""

    name: str
    sample_rate: int
    sample_width: int
    silence_byte: int

    @property
    def bytes_per_ms(self) -> float:
        return self.sample_rate * self.sample_width / _MS_PER_SECOND

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * FRAME_MS // _MS_PER_SECOND

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.sample_width

    def duration_ms(self, n_bytes: int) -> float:
        """Milliseconds of audio in ``n_bytes`` of this format."""
        return n_bytes / self.bytes_per_ms


ULAW_8000: Final = AudioFormat("ulaw_8000", 8000, 1, ULAW_SILENCE)
"""PCMU 8 kHz (Telnyx default, Scribe/TTS ``ulaw_8000``): 160-byte frames."""

L16_16000: Final = AudioFormat("l16_16000", 16000, 2, 0x00)
"""L16 16 kHz (hd_audio wire, big-endian by default): 640-byte frames."""

PCM16LE_16000: Final = AudioFormat("pcm_16000", 16000, 2, 0x00)
"""Little-endian PCM16 16 kHz (ElevenLabs ``pcm_16000``, widget): 640-byte frames."""
