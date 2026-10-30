"""Agent speech detection for the scenario caller runner (specs/30-testing-and-evals.md §3.5, Decision
2026-10-29 P03-T03c rule 5).

The runner listens to the agent audio the media session *plays out*: every 20 ms tick yields one 8 kHz PCM16
frame, which counts as speech when its RMS exceeds :data:`AGENT_SPEECH_LEVEL`. :class:`SpeechDetector` turns
those per-frame decisions into the length of the agent's current turn and the silence since it last spoke.
"""

from __future__ import annotations

from typing import Final

import numpy as np

from va_sim.telnyx.media_format import FRAME_MS

__all__ = ["AGENT_SPEECH_LEVEL", "SpeechDetector", "is_speech"]

# Agent audio louder than this counts as speech (frame RMS for the runner, peak sample for timeline latency).
AGENT_SPEECH_LEVEL: Final = 256


def is_speech(pcm8k: bytes) -> bool:
    """Whether one frame of PCM16 little-endian audio is speech (RMS above :data:`AGENT_SPEECH_LEVEL`)."""
    samples = np.frombuffer(pcm8k, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return False
    return float(np.sqrt(np.mean(samples * samples))) > AGENT_SPEECH_LEVEL


class SpeechDetector:
    """Turn length and trailing silence of the agent, fed one decision per 20 ms frame.

    Silent gaps shorter than ``gap_ms`` stay inside the turn (they are not counted as speech); speech that
    resumes after a silence of at least ``gap_ms`` starts a new turn.
    """

    def __init__(self, gap_ms: int) -> None:
        self._gap_ms = gap_ms
        self.turn_ms = 0
        self.silence_ms = 0

    def feed(self, speech: bool) -> None:
        if not speech:
            self.silence_ms += FRAME_MS
            return
        if self.silence_ms and self.silence_ms >= self._gap_ms:
            self.turn_ms = 0
        self.turn_ms += FRAME_MS
        self.silence_ms = 0
