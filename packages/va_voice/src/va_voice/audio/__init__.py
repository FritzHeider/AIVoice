"""Audio codecs, formats, framing, resampling and levels (specs/07-media-pipeline.md §4)."""

from va_voice.audio.codec import (
    FULL_SCALE,
    PCM16_TO_ULAW,
    ULAW_SILENCE,
    ULAW_TO_PCM16,
    L16ByteOrder,
    float32_to_pcm,
    l16_to_pcm,
    l16be_to_pcm,
    pcm16le_to_pcm,
    pcm_to_float32,
    pcm_to_l16,
    pcm_to_l16be,
    pcm_to_pcm16le,
    ulaw_decode,
    ulaw_encode,
)
from va_voice.audio.formats import FRAME_MS, L16_16000, PCM16LE_16000, ULAW_8000, AudioFormat
from va_voice.audio.framing import Framer, join_frames, split_frames
from va_voice.audio.levels import apply_gain, dbfs_to_amplitude, gain_for_db, rms, rms_dbfs
from va_voice.audio.resample import Quality, Resampler, resample

__all__ = [
    "FRAME_MS",
    "FULL_SCALE",
    "L16_16000",
    "PCM16LE_16000",
    "PCM16_TO_ULAW",
    "ULAW_8000",
    "ULAW_SILENCE",
    "ULAW_TO_PCM16",
    "AudioFormat",
    "Framer",
    "L16ByteOrder",
    "Quality",
    "Resampler",
    "apply_gain",
    "dbfs_to_amplitude",
    "float32_to_pcm",
    "gain_for_db",
    "join_frames",
    "l16_to_pcm",
    "l16be_to_pcm",
    "pcm16le_to_pcm",
    "pcm_to_float32",
    "pcm_to_l16",
    "pcm_to_l16be",
    "pcm_to_pcm16le",
    "resample",
    "rms",
    "rms_dbfs",
    "split_frames",
    "ulaw_decode",
    "ulaw_encode",
]
