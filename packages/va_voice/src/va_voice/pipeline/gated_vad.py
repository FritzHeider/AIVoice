"""``noise_cancellation`` on the VAD path (specs/07-media-pipeline.md §10.1, Decision P05-T15).

:class:`GatedVad` puts a :class:`~va_voice.noise.SpectralGate` in front of a VAD of one leg: the 16 kHz
PCM goes through the gate, the gated samples feed the VAD, and the VAD's ``speech_start``/``speech_end``
decisions are fed back to the gate (its noise profile learns only during non-speech). The gate delays the
stream by :data:`~va_voice.noise.LATENCY_MS`, so event times are shifted back by that much (clamped at 0)
and stay on the raw stream's timeline. The STT path is never filtered (Scribe gets
``filter_background_audio`` instead).

With ``window_samples`` (the VAD's window, a multiple of the gate hop) the raw samples wait in
:class:`GatedVad` until the gate's release would complete at least one more VAD window (specs/07 §10.1
Decision P05-T17e2): the gate releases ``floor(T / 256) x 256`` samples after ``T`` raw samples, so that is
exactly the feed on which the VAD would have produced its next window anyway. Events, their times and the
gate's output are identical to gating every feed; the gate and the VAD just run once per window instead of
once per 20 ms frame, and the trailing partial window never reaches the VAD.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from numpy.typing import NDArray

from va_voice.noise import HOP, LATENCY_MS, SpectralGate
from va_voice.pipeline.protocols import VoiceActivityDetector
from va_voice.vad import VadEvent

__all__ = ["GatedVad"]


class GatedVad:
    """A :class:`VoiceActivityDetector` that feeds ``vad`` the noise-gated stream of one leg."""

    __slots__ = ("_gate", "_raw", "_received", "_vad", "_window")

    def __init__(
        self, vad: VoiceActivityDetector, gate: SpectralGate | None = None, *, window_samples: int = HOP
    ) -> None:
        if window_samples < HOP or window_samples % HOP:
            message = f"window_samples must be a positive multiple of {HOP}, got {window_samples}"
            raise ValueError(message)
        self._vad = vad
        self._gate = gate if gate is not None else SpectralGate()
        self._window = window_samples
        self._raw: list[NDArray[np.int16]] = []
        self._received = 0

    @property
    def gate(self) -> SpectralGate:
        return self._gate

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        """Gate ``pcm16k`` and run the VAD on the samples the gate released; events on the raw timeline."""
        if pcm16k.dtype != np.int16 or pcm16k.ndim != 1:
            message = f"expected 1-D int16 samples, got {pcm16k.dtype} with shape {pcm16k.shape}"
            raise ValueError(message)
        before, self._received = self._received, self._received + pcm16k.size
        if self._received // self._window == before // self._window:
            self._raw.append(pcm16k)
            return []
        raw = np.concatenate([*self._raw, pcm16k]) if self._raw else pcm16k
        self._raw = []
        gated = self._gate.process(raw)
        if gated.size == 0:
            return []
        events = await self._vad.feed(gated)
        for event in events:
            if event.kind == "speech_start":
                self._gate.set_speech(True)
            elif event.kind == "speech_end":
                self._gate.set_speech(False)
        return [dataclasses.replace(event, at_ms=max(0.0, event.at_ms - LATENCY_MS)) for event in events]
