"""Synthetic caller audio for the simulators (specs/30-testing-and-evals.md §3.1).

The simulator's own G.711 μ-law codec and L16 helpers, the utterance marker preamble and its decoder, formant
caller speech, and DTMF tones. Nothing here imports ``va_sim.state`` (the state imports the marker limits).
"""

from va_sim.audio.codec import (
    PCM16_TO_ULAW,
    ULAW_SILENCE,
    ULAW_TO_PCM16,
    dbfs_peak,
    l16be_to_pcm,
    pcm_to_l16be,
    ulaw_decode,
    ulaw_encode,
)
from va_sim.audio.dtmf import DTMF_FREQS, synthesize_dtmf
from va_sim.audio.marker import MARKER_MS, MAX_UTTERANCE_ID, decode_marker, goertzel_power, marker_preamble
from va_sim.audio.rng import AudioRng
from va_sim.audio.synth import (
    VOWEL_FORMANTS,
    synthesize_pcm16,
    synthesize_utterance,
    synthesize_wire,
    utterance_ms,
    vowel_sequence,
)

__all__ = [
    "DTMF_FREQS",
    "MARKER_MS",
    "MAX_UTTERANCE_ID",
    "PCM16_TO_ULAW",
    "ULAW_SILENCE",
    "ULAW_TO_PCM16",
    "VOWEL_FORMANTS",
    "AudioRng",
    "dbfs_peak",
    "decode_marker",
    "goertzel_power",
    "l16be_to_pcm",
    "marker_preamble",
    "pcm_to_l16be",
    "synthesize_dtmf",
    "synthesize_pcm16",
    "synthesize_utterance",
    "synthesize_wire",
    "ulaw_decode",
    "ulaw_encode",
    "utterance_ms",
    "vowel_sequence",
]
