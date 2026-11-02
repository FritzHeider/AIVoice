"""Opener pre-render into the phrase cache over FakeElevenLabs REST (specs/07 §11 step 5, specs/09 §9;
Decision P05-T10d)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import ValidationError
from rest_client_helpers import SimRest, sim_rest
from service_helpers import json_log_stream, log_events
from task_helpers import NOW, task_context
from tts_helpers import VOICE_ID

from va_domain.call_context import CallContext
from va_domain.org import OrgSettings, PrivacySettings
from va_domain.voices import VoiceSettings
from va_engine.opener import cache_text, phrase_format, phrase_voice
from va_voice.phrase_cache import (
    PER_CALL_TTL_S,
    SHARED_TTL_S,
    STANDARD_FILLERS,
    PhraseCache,
    phrase_cache_key,
)
from va_workflows.activities.prerender import PRERENDER_BUDGET_S, OpenerPrerender, PrerenderResult

MODEL = "eleven_flash_v2_5"
OPENER = "Hi Ana, this is Maya from Acme calling about your table."
REQUESTS = "/sim/elevenlabs/tts/requests"


@dataclass
class Orgs:
    settings: OrgSettings | None = field(default_factory=OrgSettings)
    error: Exception | None = None

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None:
        del org_id
        if self.error is not None:
            raise self.error
        return self.settings


@dataclass
class Rig:
    sim: SimRest
    redis: FakeAsyncRedis
    cache: PhraseCache
    orgs: Orgs

    def prerender(self, *, budget_s: float = PRERENDER_BUDGET_S) -> OpenerPrerender:
        return OpenerPrerender(cache=self.cache, org_settings=self.orgs, model_id=MODEL, budget_s=budget_s)

    async def texts(self) -> list[str]:
        return [record["text"] for record in await self.sim.records(REQUESTS, "requests")]


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async with sim_rest() as sim:
        redis = FakeAsyncRedis()
        cache = PhraseCache(sim.client, redis)
        try:
            yield Rig(sim, redis, cache, Orgs())
        finally:
            await cache.aclose()
            await redis.aclose()


def context(**config: Any) -> CallContext:
    defaults: dict[str, Any] = {
        "first_sentence": "Hi {{first_name}}, this is Maya from Acme calling about your table.",
        "request_data": {"first_name": "Ana"},
        "provider_voice_id": VOICE_ID,
        "wait_for_greeting": False,
        "language": "en",
    }
    return task_context(**{**defaults, **config})


def with_opener(prerender: OpenerPrerender, ctx: CallContext) -> CallContext:
    return ctx.model_copy(update={"opener_text": prerender.opener_for(ctx, NOW)})


async def test_the_opener_and_fillers_are_cached_under_the_media_key(rig: Rig) -> None:
    prerender = rig.prerender()
    ctx = with_opener(prerender, context())
    assert ctx.opener_text == OPENER

    result = await prerender.render(ctx)

    assert result == PrerenderResult(opener=True, fillers=len(STANDARD_FILLERS))
    voice = phrase_voice(ctx, model_id=MODEL)
    fmt = phrase_format(ctx)
    opener_key = phrase_cache_key(voice, cache_text(ctx, OPENER), fmt)
    assert 0 < await rig.redis.ttl(opener_key) <= PER_CALL_TTL_S
    for filler in STANDARD_FILLERS:
        assert PER_CALL_TTL_S < await rig.redis.ttl(phrase_cache_key(voice, filler, fmt)) <= SHARED_TTL_S
    assert sorted(await rig.texts()) == sorted([OPENER, *STANDARD_FILLERS])


async def test_hd_audio_and_voice_settings_select_the_entry(rig: Rig) -> None:
    prerender = rig.prerender()
    ctx = with_opener(prerender, context(hd_audio=True, voice_settings={"speed": 1.1}))
    await prerender.render(ctx)
    voice = phrase_voice(ctx, model_id=MODEL)
    assert voice.settings == VoiceSettings(speed=1.1)
    assert await rig.redis.exists(phrase_cache_key(voice, OPENER, phrase_format(ctx))) == 1
    records = await rig.sim.records(REQUESTS, "requests")
    assert {record["output_format"] for record in records} == {"pcm_16000"}


async def test_the_pronunciation_guide_shapes_the_key_text(rig: Rig) -> None:
    prerender = rig.prerender()
    ctx = with_opener(prerender, context(pronunciation_guide=[{"word": "Acme", "pronunciation": "Ack-mee"}]))
    assert ctx.opener_text == OPENER
    await prerender.render(ctx)
    pronounced = OPENER.replace("Acme", "Ack-mee")
    key = phrase_cache_key(phrase_voice(ctx, model_id=MODEL), pronounced, phrase_format(ctx))
    assert await rig.redis.exists(key) == 1
    assert pronounced in await rig.texts()


ZERO_RETENTION_ORGS = [
    OrgSettings(privacy=PrivacySettings(zero_retention=True)),
    OrgSettings(hipaa_mode=True),
]


@pytest.mark.parametrize("org", ZERO_RETENTION_ORGS)
async def test_zero_retention_orgs_render_nothing(rig: Rig, org: OrgSettings) -> None:
    rig.orgs.settings = org
    prerender = rig.prerender()
    assert await prerender.render(with_opener(prerender, context())) == PrerenderResult(
        skipped="zero_retention"
    )
    assert await rig.texts() == []
    assert await rig.redis.keys("*") == []


async def test_a_missing_org_renders_nothing(rig: Rig) -> None:
    rig.orgs.settings = None
    prerender = rig.prerender()
    assert await prerender.render(with_opener(prerender, context())) == PrerenderResult(skipped="org_missing")
    assert await rig.texts() == []


def _stale_settings_error() -> Exception:
    try:
        OrgSettings.model_validate({"default_max_duration_min": 0})
    except ValidationError as exc:
        return exc
    raise AssertionError(STALE_SETTINGS_VALIDATED)


STALE_SETTINGS_VALIDATED = "stale settings unexpectedly validated"


@pytest.mark.parametrize(
    ("error", "error_type"),
    [(ConnectionError("redis down"), "ConnectionError"), (_stale_settings_error(), "ValidationError")],
)
async def test_unreadable_org_settings_render_nothing(rig: Rig, error: Exception, error_type: str) -> None:
    stream = json_log_stream()
    rig.orgs.error = error
    prerender = rig.prerender()
    result = await prerender.render(with_opener(prerender, context()))
    assert result == PrerenderResult(skipped="org_settings_unavailable")
    [event] = [e for e in log_events(stream) if e["event"] == "call_context.org_settings_unavailable"]
    assert event["error"] == error_type
    assert await rig.texts() == []


async def test_wait_for_greeting_leaves_the_opener_to_the_call(rig: Rig) -> None:
    prerender = rig.prerender()
    ctx = with_opener(prerender, context(wait_for_greeting=True))
    assert ctx.opener_text is None
    assert await prerender.render(ctx) == PrerenderResult(opener=False, fillers=len(STANDARD_FILLERS))
    assert sorted(await rig.texts()) == sorted(STANDARD_FILLERS)


async def test_a_failing_render_is_logged_and_never_raised(rig: Rig) -> None:
    stream = json_log_stream()
    await rig.sim.send("PUT", "/sim/elevenlabs/tts/fault", {"kind": "error", "times": 100})
    prerender = rig.prerender()
    ctx = with_opener(prerender, context())
    assert await prerender.render(ctx) == PrerenderResult(opener=False, fillers=0)
    [event] = [e for e in log_events(stream) if e["event"] == "call_context.prerender_failed"]
    assert event["entry"] == phrase_cache_key(phrase_voice(ctx, model_id=MODEL), OPENER, phrase_format(ctx))
    assert OPENER not in str(event)
    assert await rig.redis.keys("*") == []


@dataclass
class HangingSynth:
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def tts_with_timestamps(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        self.started.set()
        await asyncio.Event().wait()


async def test_the_budget_bounds_a_hanging_render() -> None:
    stream = json_log_stream()
    cache = PhraseCache(HangingSynth(), FakeAsyncRedis())
    prerender = OpenerPrerender(cache=cache, org_settings=Orgs(), model_id=MODEL, budget_s=0.05)
    try:
        result = await prerender.render(with_opener(prerender, context()))
    finally:
        await cache.aclose()
    assert result == PrerenderResult(skipped="timeout")
    [event] = [e for e in log_events(stream) if e["event"] == "call_context.prerender_failed"]
    assert (event["error"], event["budget_s"]) == ("TimeoutError", 0.05)
