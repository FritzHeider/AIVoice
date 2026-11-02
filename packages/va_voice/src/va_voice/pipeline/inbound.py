"""Inbound fan-out of one leg (specs/07-media-pipeline.md §5, Decision P04-T12a).

Every frame goes through the :class:`~va_voice.reorder.ReorderBuffer`; each in-order frame is sent to the
STT (raw μ-law bytes; 16 kHz legs as little-endian PCM16, §15) and, decoded to 16 kHz PCM16 (μ-law resampled
with soxr, 16 kHz legs as they are), to the VAD. VAD event times are stream time; :class:`InboundPath` maps
them onto the pipeline timeline with ``origin_ms`` = arrival time of the first in-order frame minus one frame
(frame ``k`` ends at stream time ``20 x k``). With ``noise_cancellation`` the VAD is wrapped in a
:class:`~va_voice.pipeline.gated_vad.GatedVad` (one spectral gate per leg, §10.1).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from va_voice.audio import FRAME_MS, ULAW_8000, Resampler, l16_to_pcm, pcm_to_pcm16le, ulaw_decode
from va_voice.pipeline.config import PipelineConfig
from va_voice.pipeline.gated_vad import GatedVad
from va_voice.pipeline.protocols import InboundAudio, SttStream, VoiceActivityDetector
from va_voice.reorder import NULL_OBSERVER as NULL_REORDER_OBSERVER
from va_voice.reorder import OrderedFrame, ReorderBuffer, ReorderObserver
from va_voice.vad import VadEvent

__all__ = ["InboundPath"]

type _Decoded = tuple[bytes, NDArray[np.int16]]


class InboundPath:
    """Reorder, STT fan-out and VAD of one leg; returns VAD events on the pipeline timeline."""

    __slots__ = ("_decode", "_now_ms", "_origin_ms", "_reorder", "_stt", "_vad")

    def __init__(
        self,
        config: PipelineConfig,
        *,
        stt: SttStream,
        vad: VoiceActivityDetector,
        now_ms: Callable[[], float],
        reorder_observer: ReorderObserver = NULL_REORDER_OBSERVER,
    ) -> None:
        self._stt = stt
        self._vad: VoiceActivityDetector = GatedVad(vad) if config.noise_cancellation else vad
        self._now_ms = now_ms
        self._reorder = ReorderBuffer(config.fmt, window=config.reorder_window, observer=reorder_observer)
        self._decode = _decoder(config)
        self._origin_ms: float | None = None

    @property
    def reorder(self) -> ReorderBuffer:
        return self._reorder

    @property
    def origin_ms(self) -> float | None:
        """Pipeline time of VAD stream time 0 (``None`` before the first frame)."""
        return self._origin_ms

    async def push(self, audio: InboundAudio) -> list[VadEvent]:
        """Process one inbound frame; VAD events of every frame it released, in order."""
        return await self._process(self._reorder.push(audio.chunk, audio.payload))

    async def drain(self) -> list[VadEvent]:
        """End of stream: process the held frames."""
        return await self._process(self._reorder.drain())

    async def _process(self, frames: list[OrderedFrame]) -> list[VadEvent]:
        events: list[VadEvent] = []
        for frame in frames:
            if self._origin_ms is None:
                self._origin_ms = self._now_ms() - FRAME_MS
            stt_bytes, pcm16k = self._decode(frame.payload)
            await self._stt.send_audio(stt_bytes)
            origin = self._origin_ms
            detected = await self._vad.feed(pcm16k)
            events.extend(dataclasses.replace(event, at_ms=origin + event.at_ms) for event in detected)
        return events


def _decoder(config: PipelineConfig) -> Callable[[bytes], _Decoded]:
    """``payload -> (STT bytes, 16 kHz PCM16)`` for the leg format."""
    if config.fmt == ULAW_8000:
        resampler = Resampler(8000, 16000)

        def decode_ulaw(payload: bytes) -> _Decoded:
            return payload, resampler.process(ulaw_decode(payload))

        return decode_ulaw
    byte_order = config.wire_byte_order

    def decode_l16(payload: bytes) -> _Decoded:
        pcm = l16_to_pcm(payload, byte_order)
        return (payload if byte_order == "little" else pcm_to_pcm16le(pcm)), pcm

    return decode_l16
