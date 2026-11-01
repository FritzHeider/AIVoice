"""PhraseCache over ``va_elevenlabs.ElevenLabsRest`` and FakeElevenLabs: a miss renders once, a hit makes no
request, voice settings select another entry, and a failing REST render asks for the socket fallback
(specs/09-tts-elevenlabs.md §9)."""

from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from rest_client_helpers import sim_rest
from tts_helpers import VOICE_ID

from va_domain.voices import VoiceSettings
from va_elevenlabs import ElevenLabsApiError
from va_voice.audio.formats import PCM16LE_16000, ULAW_8000
from va_voice.phrase_cache import PhraseCache, PhraseUnavailableError, PhraseVoice

MODEL = "eleven_flash_v2_5"
VOICE = PhraseVoice(VOICE_ID, MODEL, VoiceSettings(), "en")
REQUESTS = "/sim/elevenlabs/tts/requests"


async def test_miss_renders_once_and_the_hit_makes_no_request() -> None:
    text = "One moment."
    async with sim_rest() as sim:
        cache = PhraseCache(sim.client, FakeAsyncRedis())
        first = await cache.get_or_render(VOICE, text, ULAW_8000)
        second = await cache.get_or_render(VOICE, text, ULAW_8000)
        records = await sim.records(REQUESTS, "requests")
    assert len(records) == 1
    assert (records[0]["transport"], records[0]["model_id"], records[0]["output_format"]) == (
        "rest",
        MODEL,
        "ulaw_8000",
    )
    assert (first.cached, second.cached) == (False, True)
    assert first.audio
    assert second.audio == first.audio
    assert first.duration_ms == len(first.audio) // 8
    assert first.alignment is not None
    assert "".join(first.alignment.chars) == text
    assert second.alignment == first.alignment
    assert first.alignment.char_start_ms[-1] + first.alignment.char_duration_ms[-1] <= first.duration_ms


async def test_other_settings_and_formats_are_separate_entries() -> None:
    async with sim_rest() as sim:
        cache = PhraseCache(sim.client, FakeAsyncRedis())
        await cache.get_or_render(VOICE, "Got it.", ULAW_8000)
        faster = PhraseVoice(VOICE_ID, MODEL, VoiceSettings(speed=1.1), "en")
        await cache.get_or_render(faster, "Got it.", ULAW_8000)
        wide = await cache.get_or_render(VOICE, "Got it.", PCM16LE_16000)
        records = await sim.records(REQUESTS, "requests")
    assert [record["output_format"] for record in records] == ["ulaw_8000", "ulaw_8000", "pcm_16000"]
    assert wide.duration_ms == len(wide.audio) // 32


async def test_a_failing_render_raises_phrase_unavailable() -> None:
    async with sim_rest() as sim:
        await sim.send("PUT", "/sim/elevenlabs/tts/fault", {"kind": "error", "times": 2})
        redis = FakeAsyncRedis()
        cache = PhraseCache(sim.client, redis)
        with pytest.raises(PhraseUnavailableError) as caught:
            await cache.get_or_render(VOICE, "Sure.", ULAW_8000)
        assert await redis.keys("*") == []
    assert isinstance(caught.value.__cause__, ElevenLabsApiError)
