"""The call's pre-rendered phrases for the pipeline Speaker (specs/07 §11 step 5, specs/09 §9; Decision
P05-T10d).

:class:`CallPhrases` is the :class:`va_voice.pipeline.PhraseSource` of one call: it reads the phrase cache
(``tts:cache:*``, written by ``prefetch_call_context``) with the call's voice and the leg's TTS format, never
renders, and counts ``va_tts_cache_hits_total`` / ``va_tts_cache_misses_total``. A Redis failure or a
malformed entry is a miss (logged by :func:`va_voice.phrase_cache.read_phrase`), so the Speaker falls back to
the live socket.
"""

from __future__ import annotations

from redis.asyncio import Redis

from va_voice.audio import AudioFormat
from va_voice.phrase_cache import CachedPhrase, PhraseCacheMetrics, PhraseVoice, read_phrase

__all__ = ["CallPhrases"]


class CallPhrases:
    """Phrase cache lookups of one call (voice and format fixed at construction)."""

    def __init__(
        self, redis: Redis, voice: PhraseVoice, fmt: AudioFormat, *, metrics: PhraseCacheMetrics | None = None
    ) -> None:
        self._redis = redis
        self._voice = voice
        self._fmt = fmt
        self._metrics = metrics

    async def lookup(self, text: str) -> CachedPhrase | None:
        phrase = await read_phrase(self._redis, self._voice, text, self._fmt)
        if self._metrics is not None:
            if phrase is None:
                self._metrics.miss()
            else:
                self._metrics.hit()
        return phrase
