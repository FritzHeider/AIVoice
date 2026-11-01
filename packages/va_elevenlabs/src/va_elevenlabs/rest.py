"""The ElevenLabs REST client (specs/09 §9, specs/08 §8, specs/15 §2-§3; Decision 2026-11-01 P03-T12)."""

from __future__ import annotations

import random

import httpx
from opentelemetry.trace import TracerProvider

from va_common.time import Clock
from va_elevenlabs.rest_metrics import ElevenLabsRestMetrics
from va_elevenlabs.rest_transport import ElevenLabsTransport
from va_elevenlabs.stt_batch import BatchSttCommands
from va_elevenlabs.tts_rest import TtsRestCommands
from va_elevenlabs.voices_rest import VoiceCommands

__all__ = ["ElevenLabsRest"]


class ElevenLabsRest(TtsRestCommands, VoiceCommands, BatchSttCommands):
    """REST synthesis, the voice library, and batch transcription for one API key.

    The caller owns ``http`` (built with ``va_common.http.http_client``); every operation sets its own
    timeout. ``repr`` never shows the key.
    """

    def __init__(  # noqa: PLR0913 - collaborators are injected for tests (clock, rng, metrics, tracing)
        self,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        clock: Clock,
        rng: random.Random | None = None,
        metrics: ElevenLabsRestMetrics | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._transport = ElevenLabsTransport(
            api_key=api_key,
            base_url=base_url,
            http=http,
            clock=clock,
            rng=rng or random.Random(),  # noqa: S311 - retry jitter, not a secret
            metrics=metrics,
            tracer_provider=tracer_provider,
        )

    def __repr__(self) -> str:
        return f"ElevenLabsRest(base_url={self._transport.base_url!r})"
