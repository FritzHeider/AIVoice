"""Caller audio rendered in a media stream's format (specs/30-testing-and-evals.md §3.2 control API and §3.5,
Decision 2026-10-29 P03-T03c rules 2 to 4).

Pure CPU (and, for WAV files, blocking file) work shared by the ``/say`` control route and the scenario caller
runner: async callers run these in a worker thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from va_sim.audio.codec import l16be_to_pcm, ulaw_decode
from va_sim.audio.dtmf import synthesize_dtmf
from va_sim.audio.synth import synthesize_pcm16
from va_sim.audio.wav_input import read_wav_pcm
from va_sim.audio.wire import NARROWBAND_RATE
from va_sim.telnyx.media_format import MediaFormat

__all__ = ["DTMF_GAP_MS", "DTMF_TONE_MS", "render_dtmf_tones", "render_text", "render_wav"]

DTMF_TONE_MS: Final = 100
DTMF_GAP_MS: Final = 100


def render_text(utterance_id: int, text: str, fmt: MediaFormat, seed: int) -> bytes:
    """The marker-prefixed synthetic utterance, synthesized at the stream rate and encoded for the stream."""
    return fmt.encode(synthesize_pcm16(utterance_id, text, sample_rate=fmt.sample_rate, seed=seed))


def render_dtmf_tones(digits: str, fmt: MediaFormat) -> bytes:
    """In-band DTMF: per digit a 100 ms tone then 100 ms of silence, in the stream format."""
    parts: list[bytes] = []
    for digit in digits:
        wire = synthesize_dtmf(digit, DTMF_TONE_MS, sample_rate=fmt.sample_rate)
        pcm = ulaw_decode(wire) if fmt.sample_rate == NARROWBAND_RATE else l16be_to_pcm(wire)
        parts.append(fmt.encode(pcm))
        parts.append(fmt.silence(DTMF_GAP_MS * fmt.bytes_per_ms))
    return b"".join(parts)


def render_wav(path: Path, fmt: MediaFormat) -> bytes:
    """A 16-bit PCM WAV file resampled to the stream rate and encoded for the stream."""
    return fmt.encode(read_wav_pcm(path, fmt.sample_rate))
