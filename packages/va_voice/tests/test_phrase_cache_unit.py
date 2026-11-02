"""PhraseCache against fakeredis and a scripted synthesizer: key stability, entry encoding, TTLs, size cap,
fail-open Redis, REST failures, single flight, metrics, and prefetch (specs/09-tts-elevenlabs.md §9, §14)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import orjson
import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from prometheus_client import CollectorRegistry
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, build_metrics, new_registry
from va_domain.voices import VoiceSettings
from va_voice.audio.formats import L16_16000, PCM16LE_16000, ULAW_8000
from va_voice.phrase_cache import (
    MAX_VALUE_BYTES,
    PER_CALL_TTL_S,
    SHARED_TTL_S,
    STANDARD_FILLERS,
    CachedPhrase,
    PhraseAlignment,
    PhraseCache,
    PhraseCacheMetrics,
    PhraseUnavailableError,
    PhraseVoice,
    decode_entry,
    encode_entry,
    phrase_cache_key,
    read_phrase,
)
from va_voice.text.alignment import AlignmentTimeline

SPEC_09 = Path(__file__).resolve().parents[3] / "specs" / "09-tts-elevenlabs.md"
VOICE = PhraseVoice("voice-1", "eleven_flash_v2_5", VoiceSettings(), "en")
TEXT = "Sure."


@dataclass(frozen=True)
class Alignment:
    characters: list[str]
    character_start_times_seconds: list[float]
    character_end_times_seconds: list[float]


@dataclass(frozen=True)
class Timestamped:
    audio: bytes
    alignment: Alignment | None


def aligned(text: str, char_s: float) -> Alignment:
    return Alignment(
        list(text),
        [i * char_s for i in range(len(text))],
        [(i + 1) * char_s for i in range(len(text))],
    )


@dataclass
class FakeSynth:
    """Renders ``ms_per_char`` ms of audio per character; optionally gated or failing."""

    ms_per_char: int = 62
    gate: asyncio.Event | None = None
    error: Exception | None = None
    with_alignment: bool = True
    char_s: float | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def tts_with_timestamps(  # noqa: PLR0913 - mirrors ElevenLabsRest
        self,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        output_format: str,
        voice_settings: VoiceSettings | None = None,
        language_code: str | None = None,
        apply_text_normalization: Literal["auto", "on", "off"] = "auto",
    ) -> Timestamped:
        self.calls.append(
            {
                "voice_id": voice_id,
                "text": text,
                "model_id": model_id,
                "output_format": output_format,
                "voice_settings": voice_settings,
                "language_code": language_code,
                "apply_text_normalization": apply_text_normalization,
            }
        )
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        bytes_per_ms = 8 if output_format == "ulaw_8000" else 32
        audio = bytes([0x7F]) * (len(text) * self.ms_per_char * bytes_per_ms)
        char_s = self.ms_per_char / 1000 if self.char_s is None else self.char_s
        return Timestamped(audio, aligned(text, char_s) if self.with_alignment else None)


def make_cache(synth: FakeSynth, redis: FakeAsyncRedis | None = None) -> PhraseCache:
    return PhraseCache(synth, redis if redis is not None else FakeAsyncRedis())


def metered(synth: FakeSynth, redis: FakeAsyncRedis | None = None) -> tuple[PhraseCache, CollectorRegistry]:
    registry = new_registry(process_collectors=False)
    metrics = PhraseCacheMetrics(build_metrics(Service.MEDIA_WORKER, registry))
    return PhraseCache(synth, redis if redis is not None else FakeAsyncRedis(), metrics=metrics), registry


async def stored(redis: FakeAsyncRedis, key: str) -> bytes:
    raw = await redis.get(key)
    assert isinstance(raw, bytes)
    return raw


def counts(registry: CollectorRegistry) -> tuple[float | None, float | None]:
    return (
        registry.get_sample_value("va_tts_cache_hits_total"),
        registry.get_sample_value("va_tts_cache_misses_total"),
    )


# --- key ---


def test_key_is_sha256_of_the_documented_material() -> None:
    settings_json = (
        '{"similarity_boost":0.75,"speed":1.0,"stability":0.5,"style":0.0,"use_speaker_boost":true}'
    )
    material = f"eleven_flash_v2_5|ulaw_8000|en|{settings_json}|Sure."
    expected = "tts:cache:voice-1:" + hashlib.sha256(material.encode()).hexdigest()
    assert phrase_cache_key(VOICE, TEXT, ULAW_8000) == expected


def test_missing_language_is_an_empty_segment() -> None:
    voice = replace(VOICE, language_code=None)
    settings_json = orjson.dumps(
        VoiceSettings().model_dump(mode="json"), option=orjson.OPT_SORT_KEYS
    ).decode()
    material = f"eleven_flash_v2_5|pcm_16000||{settings_json}|Hi"
    assert phrase_cache_key(voice, "Hi", PCM16LE_16000).endswith(
        hashlib.sha256(material.encode()).hexdigest()
    )


def test_partial_and_explicit_default_settings_share_a_key() -> None:
    explicit = replace(
        VOICE,
        settings=VoiceSettings(
            stability=0.5, similarity_boost=0.75, style=0.0, use_speaker_boost=True, speed=1.0
        ),
    )
    assert phrase_cache_key(explicit, TEXT, ULAW_8000) == phrase_cache_key(VOICE, TEXT, ULAW_8000)


@pytest.mark.parametrize(
    "voice",
    [
        replace(VOICE, voice_id="voice-2"),
        replace(VOICE, model_id="eleven_turbo_v2_5"),
        replace(VOICE, language_code="de"),
        replace(VOICE, language_code=None),
        replace(VOICE, settings=VoiceSettings(stability=0.4)),
        replace(VOICE, settings=VoiceSettings(similarity_boost=0.7)),
        replace(VOICE, settings=VoiceSettings(style=0.1)),
        replace(VOICE, settings=VoiceSettings(use_speaker_boost=False)),
        replace(VOICE, settings=VoiceSettings(speed=1.1)),
    ],
)
def test_every_voice_field_changes_the_key(voice: PhraseVoice) -> None:
    assert phrase_cache_key(voice, TEXT, ULAW_8000) != phrase_cache_key(VOICE, TEXT, ULAW_8000)


def test_text_and_format_change_the_key() -> None:
    base = phrase_cache_key(VOICE, TEXT, ULAW_8000)
    assert phrase_cache_key(VOICE, "Sure!", ULAW_8000) != base
    assert phrase_cache_key(VOICE, TEXT, PCM16LE_16000) != base
    assert phrase_cache_key(VOICE, TEXT, ULAW_8000) == base


# --- entries ---


def phrase(alignment: PhraseAlignment | None = None) -> CachedPhrase:
    return CachedPhrase(TEXT, ULAW_8000, b"\x01\x02\x03", alignment, 12, cached=False)


def test_entry_round_trips_with_and_without_alignment() -> None:
    alignment = PhraseAlignment(("S", "u"), (0, 63), (63, 62))
    for item in (phrase(alignment), phrase(None)):
        decoded = decode_entry(encode_entry(item), TEXT, ULAW_8000)
        assert decoded == replace(item, cached=True)


def test_entry_is_the_documented_json() -> None:
    document = orjson.loads(encode_entry(phrase(PhraseAlignment(("S",), (0,), (63,)))))
    assert document == {
        "audio_b64": base64.b64encode(b"\x01\x02\x03").decode(),
        "alignment": {"chars": ["S"], "char_start_ms": [0], "char_duration_ms": [63]},
        "duration_ms": 12,
    }


GOOD_ALIGNMENT = {"chars": ["a"], "char_start_ms": [0], "char_duration_ms": [5]}


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        orjson.dumps({"audio_b64": 3, "alignment": None, "duration_ms": 1}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": None, "duration_ms": "1"}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": None, "duration_ms": True}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": None, "duration_ms": -1}),
        orjson.dumps({"audio_b64": "*not base64*", "alignment": None, "duration_ms": 1}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": [], "duration_ms": 1}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": {**GOOD_ALIGNMENT, "chars": "a"}, "duration_ms": 1}),
        orjson.dumps({"audio_b64": "AAE=", "alignment": {**GOOD_ALIGNMENT, "chars": [1]}, "duration_ms": 1}),
        orjson.dumps(
            {"audio_b64": "AAE=", "alignment": {**GOOD_ALIGNMENT, "char_start_ms": [0.5]}, "duration_ms": 1}
        ),
        orjson.dumps(
            {"audio_b64": "AAE=", "alignment": {**GOOD_ALIGNMENT, "char_duration_ms": [-1]}, "duration_ms": 1}
        ),
        orjson.dumps(
            {"audio_b64": "AAE=", "alignment": {**GOOD_ALIGNMENT, "char_start_ms": [0, 1]}, "duration_ms": 1}
        ),
    ],
)
def test_malformed_entries_decode_to_none(raw: bytes) -> None:
    assert decode_entry(raw, TEXT, ULAW_8000) is None


def test_well_formed_entry_with_alignment_decodes() -> None:
    raw = orjson.dumps({"audio_b64": "AAE=", "alignment": GOOD_ALIGNMENT, "duration_ms": 1})
    decoded = decode_entry(raw.decode(), TEXT, ULAW_8000)
    assert decoded is not None
    assert decoded.alignment == PhraseAlignment(("a",), (0,), (5,))


# --- get_or_render ---


async def test_miss_renders_and_stores_then_hit_skips_the_synthesizer() -> None:
    synth = FakeSynth()
    redis = FakeAsyncRedis()
    cache, registry = metered(synth, redis)
    first = await cache.get_or_render(VOICE, TEXT, ULAW_8000)
    second = await cache.get_or_render(VOICE, TEXT, ULAW_8000)
    assert len(synth.calls) == 1
    assert synth.calls[0] == {
        "voice_id": "voice-1",
        "text": TEXT,
        "model_id": "eleven_flash_v2_5",
        "output_format": "ulaw_8000",
        "voice_settings": VoiceSettings(),
        "language_code": "en",
        "apply_text_normalization": "auto",
    }
    assert (first.cached, second.cached) == (False, True)
    assert (second.audio, second.alignment, second.duration_ms) == (
        first.audio,
        first.alignment,
        first.duration_ms,
    )
    key = phrase_cache_key(VOICE, TEXT, ULAW_8000)
    assert SHARED_TTL_S - 1 <= await redis.ttl(key) <= SHARED_TTL_S
    assert counts(registry) == (1.0, 1.0)


async def test_per_call_ttl() -> None:
    redis = FakeAsyncRedis()
    await make_cache(FakeSynth(), redis).get_or_render(VOICE, TEXT, ULAW_8000, ttl_s=PER_CALL_TTL_S)
    assert PER_CALL_TTL_S - 1 <= await redis.ttl(phrase_cache_key(VOICE, TEXT, ULAW_8000)) <= PER_CALL_TTL_S


async def test_alignment_is_converted_to_integer_ms_rounding_half_up() -> None:
    result = await make_cache(FakeSynth(char_s=0.0625)).get_or_render(VOICE, "abc", ULAW_8000)
    # 0.0625 s per character: starts 0, 62.5, 125 -> 0, 63, 125; ends 62.5, 125, 187.5 -> 63, 125, 188.
    assert result.alignment == PhraseAlignment(("a", "b", "c"), (0, 63, 125), (63, 62, 63))


async def test_reversed_vendor_times_give_zero_duration() -> None:
    @dataclass
    class Reversed(FakeSynth):
        async def tts_with_timestamps(self, voice_id: str, text: str, **kwargs: Any) -> Timestamped:
            del voice_id, kwargs
            return Timestamped(b"\x7f" * 8, Alignment(list(text), [0.01], [0.005]))

    result = await make_cache(Reversed()).get_or_render(VOICE, "a", ULAW_8000)
    assert result.alignment == PhraseAlignment(("a",), (10,), (0,))


async def test_missing_alignment_is_stored_as_null() -> None:
    redis = FakeAsyncRedis()
    cache = make_cache(FakeSynth(with_alignment=False), redis)
    assert (await cache.get_or_render(VOICE, TEXT, ULAW_8000)).alignment is None
    document = orjson.loads(await stored(redis, phrase_cache_key(VOICE, TEXT, ULAW_8000)))
    assert document["alignment"] is None
    assert (await cache.get_or_render(VOICE, TEXT, ULAW_8000)).cached


@pytest.mark.parametrize(("fmt", "bytes_per_ms"), [(ULAW_8000, 8), (PCM16LE_16000, 32)])
async def test_duration_follows_the_format(fmt: Any, bytes_per_ms: int) -> None:
    synth = FakeSynth()
    result = await make_cache(synth).get_or_render(VOICE, "Hello", fmt)
    assert len(result.audio) == 5 * 62 * bytes_per_ms
    assert result.duration_ms == 5 * 62
    assert result.fmt == fmt
    assert synth.calls[0]["output_format"] == fmt.name


async def test_unsupported_format_empty_text_and_bad_ttl_are_rejected() -> None:
    cache = make_cache(FakeSynth())
    with pytest.raises(ValueError, match="unsupported phrase format 'l16_16000'"):
        await cache.get_or_render(VOICE, TEXT, L16_16000)
    with pytest.raises(ValueError, match="empty"):
        await cache.get_or_render(VOICE, "  ", ULAW_8000)
    with pytest.raises(ValueError, match="ttl_s"):
        await cache.get_or_render(VOICE, TEXT, ULAW_8000, ttl_s=0)


async def test_malformed_cached_value_is_a_miss_and_is_overwritten() -> None:
    redis = FakeAsyncRedis()
    key = phrase_cache_key(VOICE, TEXT, ULAW_8000)
    await redis.set(key, b"garbage")
    synth = FakeSynth()
    stream = json_log_stream()
    result = await make_cache(synth, redis).get_or_render(VOICE, TEXT, ULAW_8000)
    assert not result.cached
    assert len(synth.calls) == 1
    assert decode_entry(await stored(redis, key), TEXT, ULAW_8000) == replace(result, cached=True)
    assert [e["entry"] for e in log_events(stream) if e["event"] == "tts.phrase_cache_malformed"] == [key]


async def test_oversize_value_is_returned_but_not_stored() -> None:
    redis = FakeAsyncRedis()
    # base64 grows 4/3: 1.6 MB of audio encodes to more than 2 MB.
    synth = FakeSynth(ms_per_char=1_600_000 // (8 * len(TEXT)))
    stream = json_log_stream()
    result = await make_cache(synth, redis).get_or_render(VOICE, TEXT, ULAW_8000)
    assert len(result.audio) >= 1_590_000
    assert await redis.get(phrase_cache_key(VOICE, TEXT, ULAW_8000)) is None
    [warning] = [e for e in log_events(stream) if e["event"] == "tts.phrase_cache_oversize"]
    assert warning["limit"] == MAX_VALUE_BYTES
    assert warning["bytes"] > MAX_VALUE_BYTES


async def test_redis_outage_fails_open() -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    server.connected = False
    synth = FakeSynth()
    stream = json_log_stream()
    cache, registry = metered(synth, redis)
    first = await cache.get_or_render(VOICE, TEXT, ULAW_8000)
    second = await cache.get_or_render(VOICE, TEXT, ULAW_8000)
    assert (first.cached, second.cached) == (False, False)
    assert len(synth.calls) == 2
    operations = [e["operation"] for e in log_events(stream) if e["event"] == "tts.phrase_cache_unavailable"]
    assert operations == ["get", "set", "get", "set"]
    assert counts(registry) == (0.0, 2.0)


async def test_rest_failure_raises_phrase_unavailable_and_stores_nothing() -> None:
    redis = FakeAsyncRedis()
    cause = ConnectionError("vendor down")
    cache = make_cache(FakeSynth(error=cause), redis)
    with pytest.raises(PhraseUnavailableError) as caught:
        await cache.get_or_render(VOICE, TEXT, ULAW_8000)
    assert caught.value.text == TEXT
    assert caught.value.__cause__ is cause
    assert str(caught.value) == "phrase synthesis failed: ConnectionError"
    assert await redis.keys("*") == []


async def test_concurrent_misses_share_one_render() -> None:
    synth = FakeSynth(gate=asyncio.Event())
    cache, registry = metered(synth)
    waiters = [asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000)) for _ in range(10)]
    await synth.started.wait()
    release(synth)
    results = await asyncio.gather(*waiters)
    assert len(synth.calls) == 1
    assert len({(r.audio, r.alignment, r.duration_ms) for r in results}) == 1
    assert counts(registry) == (0.0, 10.0)
    assert cache._flights == {}


async def test_a_cancelled_first_caller_does_not_cancel_the_others() -> None:
    synth = FakeSynth(gate=asyncio.Event())
    redis = FakeAsyncRedis()
    cache = make_cache(synth, redis)
    first = asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000))
    await synth.started.wait()
    second = asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release(synth)
    result = await second
    assert not result.cached
    assert len(synth.calls) == 1
    assert await redis.get(phrase_cache_key(VOICE, TEXT, ULAW_8000)) is not None


async def render_in_flight(cache: PhraseCache, synth: FakeSynth) -> asyncio.Task[CachedPhrase]:
    """Start a render held by ``synth.gate`` whose only waiter has already been cancelled."""
    waiter = asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000))
    await synth.started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    [flight] = cache._flights.values()
    return flight


def release(synth: FakeSynth) -> None:
    assert synth.gate is not None
    synth.gate.set()


async def test_a_render_whose_only_waiter_left_still_stores_the_phrase() -> None:
    synth = FakeSynth(gate=asyncio.Event())
    cache = make_cache(synth)
    flight = await render_in_flight(cache, synth)
    release(synth)
    await flight
    assert (await cache.get_or_render(VOICE, TEXT, ULAW_8000)).cached


async def test_a_failure_reaches_every_waiter_and_is_retrieved() -> None:
    synth = FakeSynth(gate=asyncio.Event(), error=TimeoutError())
    cache = make_cache(synth)
    waiters = [asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000)) for _ in range(3)]
    await synth.started.wait()
    release(synth)
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(r, PhraseUnavailableError) for r in results)
    assert len(synth.calls) == 1


async def test_a_failed_render_with_no_waiters_is_retrieved_silently() -> None:
    synth = FakeSynth(gate=asyncio.Event(), error=TimeoutError())
    cache = make_cache(synth)
    flight = await render_in_flight(cache, synth)
    release(synth)
    with pytest.raises(PhraseUnavailableError):
        await flight
    assert cache._flights == {}


async def test_aclose_cancels_renders_in_flight() -> None:
    synth = FakeSynth(gate=asyncio.Event())
    redis = FakeAsyncRedis()
    cache = make_cache(synth, redis)
    flight = await render_in_flight(cache, synth)
    joined = asyncio.create_task(cache.get_or_render(VOICE, TEXT, ULAW_8000))
    await asyncio.sleep(0)
    await cache.aclose()
    assert flight.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await joined
    assert cache._flights == {}
    assert await redis.keys("*") == []


async def test_cached_alignment_drives_the_alignment_timeline() -> None:
    cache = make_cache(FakeSynth())
    await cache.get_or_render(VOICE, "Got it.", ULAW_8000)
    result = await cache.get_or_render(VOICE, "Got it.", ULAW_8000)
    assert result.cached
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause(result.text)
    timeline.add_chunk(result.audio, result.alignment)
    assert timeline.text_until(result.duration_ms).text == "Got it."
    assert timeline.text_until(200).text == "Got"


# --- prefetch ---


def test_standard_fillers_match_the_spec() -> None:
    section = SPEC_09.read_text(encoding="utf-8").split("## 9. Phrase cache", 1)[1]
    listed = re.search(r"fillers/backchannels \(([^)]*)\)", section, re.DOTALL)
    assert listed is not None
    assert tuple(re.findall(r'"([^"]+)"', listed.group(1))) == STANDARD_FILLERS


async def test_prefetch_renders_every_filler_once() -> None:
    synth = FakeSynth()
    cache = make_cache(synth)
    phrases = await cache.prefetch(VOICE, ULAW_8000)
    assert list(phrases) == list(STANDARD_FILLERS)
    assert all(p is not None and not p.cached for p in phrases.values())
    again = await cache.prefetch(VOICE, ULAW_8000, [*STANDARD_FILLERS, "Sure."], ttl_s=PER_CALL_TTL_S)
    assert all(p is not None and p.cached for p in again.values())
    assert len(synth.calls) == len(STANDARD_FILLERS)


async def test_prefetch_maps_failures_to_none_and_logs_them() -> None:
    stream = json_log_stream()
    phrases = await make_cache(FakeSynth(error=OSError("down"))).prefetch(VOICE, ULAW_8000, ["Hi."])
    assert phrases == {"Hi.": None}
    [warning] = [e for e in log_events(stream) if e["event"] == "tts.phrase_prefetch_failed"]
    assert (warning["entry"], warning["error"]) == (phrase_cache_key(VOICE, "Hi.", ULAW_8000), "OSError")
    assert "Hi." not in stream.getvalue()


async def test_prefetch_reraises_programming_errors() -> None:
    with pytest.raises(ValueError, match="empty"):
        await make_cache(FakeSynth()).prefetch(VOICE, ULAW_8000, [""])


async def test_read_phrase_serves_a_stored_entry_without_rendering() -> None:
    redis = FakeAsyncRedis()
    synth = FakeSynth()
    rendered = await make_cache(synth, redis).get_or_render(VOICE, TEXT, ULAW_8000, ttl_s=PER_CALL_TTL_S)
    assert await read_phrase(redis, VOICE, TEXT, ULAW_8000) == replace(rendered, cached=True)
    assert len(synth.calls) == 1


async def test_read_phrase_misses_absent_entries_and_other_formats() -> None:
    redis = FakeAsyncRedis()
    await make_cache(FakeSynth(), redis).get_or_render(VOICE, TEXT, ULAW_8000)
    assert await read_phrase(redis, VOICE, "Got it.", ULAW_8000) is None
    assert await read_phrase(redis, VOICE, TEXT, PCM16LE_16000) is None


async def test_read_phrase_treats_a_malformed_entry_as_a_miss() -> None:
    redis = FakeAsyncRedis()
    key = phrase_cache_key(VOICE, TEXT, ULAW_8000)
    await redis.set(key, b"garbage")
    stream = json_log_stream()
    assert await read_phrase(redis, VOICE, TEXT, ULAW_8000) is None
    assert [e["entry"] for e in log_events(stream) if e["event"] == "tts.phrase_cache_malformed"] == [key]
    assert await redis.get(key) == b"garbage"


async def test_read_phrase_fails_open_when_redis_is_down() -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    server.connected = False
    stream = json_log_stream()
    assert await read_phrase(redis, VOICE, TEXT, ULAW_8000) is None
    [event] = [e for e in log_events(stream) if e["event"] == "tts.phrase_cache_unavailable"]
    assert (event["operation"], event["entry"]) == ("get", phrase_cache_key(VOICE, TEXT, ULAW_8000))


async def test_read_phrase_rejects_unsupported_formats() -> None:
    with pytest.raises(ValueError, match="unsupported phrase format"):
        await read_phrase(FakeAsyncRedis(), VOICE, TEXT, L16_16000)
