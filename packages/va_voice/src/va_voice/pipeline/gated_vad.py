"""``noise_cancellation`` on the VAD path (specs/07-media-pipeline.md §10.1, Decision P05-T15).

:class:`GatedVad` puts a :class:`~va_voice.noise.SpectralGate` in front of a VAD of one leg: the 16 kHz
PCM goes through the gate, the gated samples feed the VAD, and the VAD's ``speech_start``/``speech_end``
decisions are fed back to the gate (its noise profile learns only during non-speech). The gate delays the
stream by :data:`~va_voice.noise.LATENCY_MS`, so event times are shifted back by that much (clamped at 0)
and stay on the raw stream's timeline. The STT path is never filtered (Scribe gets
``filter_background_audio`` instead).
"""

from __future__ import annotations

import dataclasses

import numpy as np
from numpy.typing import NDArray

from va_voice.noise import LATENCY_MS, SpectralGate
from va_voice.pipeline.protocols import VoiceActivityDetector
from va_voice.vad import VadEvent

__all__ = ["GatedVad"]


class GatedVad:
    """A :class:`VoiceActivityDetector` that feeds ``vad`` the noise-gated stream of one leg."""

    __slots__ = ("_gate", "_vad")

    def __init__(self, vad: VoiceActivityDetector, gate: SpectralGate | None = None) -> None:
        self._vad = vad
        self._gate = gate if gate is not None else SpectralGate()

    @property
    def gate(self) -> SpectralGate:
        return self._gate

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        """Gate ``pcm16k`` and run the VAD on the samples the gate released; events on the raw timeline."""
        gated = self._gate.process(pcm16k)
        if gated.size == 0:
            return []
        events = await self._vad.feed(gated)
        for event in events:
            if event.kind == "speech_start":
                self._gate.set_speech(True)
            elif event.kind == "speech_end":
                self._gate.set_speech(False)
        return [dataclasses.replace(event, at_ms=max(0.0, event.at_ms - LATENCY_MS)) for event in events]
