"""CallPhrases: read-only phrase cache lookups with hit/miss metrics, and the opener voice matching the TTS
socket's voice (specs/07 §11 step 5, Decision P05-T10d)."""

from __future__ import annotations

import uuid

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from media_helpers import call_context
from prometheus_client import CollectorRegistry

from va_common.metrics import Service, build_metrics, new_registry
from va_domain.voices import VoiceSettings
from va_engine.opener import phrase_voice
from va_media.phrases import CallPhrases
from va_media.runtime_config import tts_voice
from va_voice.audio import PCM16LE_16000, ULAW_8000
from va_voice.phrase_cache import (
    CachedPhrase,
    PhraseCacheMetrics,
    PhraseVoice,
    encode_entry,
    phrase_cache_key,
)

VOICE = PhraseVoice("voice-1", "eleven_flash_v2_5", VoiceSettings(), "en")
TEXT = "Hi, this is Maya."


def metered() -> tuple[PhraseCacheMetrics, CollectorRegistry]:
    service = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    return PhraseCacheMetrics(service), service.registry


def counts(registry: CollectorRegistry) -> tuple[float | None, float | None]:
    return (
        registry.get_sample_value("va_tts_cache_hits_total"),
        registry.get_sample_value("va_tts_cache_misses_total"),
    )


async def store(redis: FakeAsyncRedis, text: str = TEXT) -> CachedPhrase:
    phrase = CachedPhrase(text, ULAW_8000, b"\x55" * 1600, None, 200, cached=True)
    await redis.set(phrase_cache_key(VOICE, text, ULAW_8000), encode_entry(phrase))
    return phrase


async def test_a_stored_phrase_is_a_counted_hit() -> None:
    redis = FakeAsyncRedis()
    phrase = await store(redis)
    metrics, registry = metered()
    assert await CallPhrases(redis, VOICE, ULAW_8000, metrics=metrics).lookup(TEXT) == phrase
    assert counts(registry) == (1.0, 0.0)


async def test_absent_text_or_another_format_is_a_counted_miss() -> None:
    redis = FakeAsyncRedis()
    await store(redis)
    metrics, registry = metered()
    assert await CallPhrases(redis, VOICE, ULAW_8000, metrics=metrics).lookup("Other.") is None
    assert await CallPhrases(redis, VOICE, PCM16LE_16000, metrics=metrics).lookup(TEXT) is None
    assert counts(registry) == (0.0, 2.0)


async def test_redis_down_is_a_miss() -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    server.connected = False
    assert await CallPhrases(redis, VOICE, ULAW_8000).lookup(TEXT) is None


@pytest.mark.parametrize("language", ["es", "babel", "en-US", "not-a-language"])
def test_the_phrase_voice_is_the_socket_voice(language: str) -> None:
    context = call_context(uuid.uuid4(), language=language, voice_settings={"speed": 1.1})
    socket = tts_voice(context, model_id="eleven_flash_v2_5")
    phrase = phrase_voice(context, model_id="eleven_flash_v2_5")
    assert (phrase.voice_id, phrase.model_id, phrase.settings, phrase.language_code) == (
        socket.provider_voice_id,
        socket.model_id,
        socket.settings,
        socket.language_code,
    )
