"""``prefetch_call_context`` (specs/01 §2.1 step 2 Decision P05-T06a)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from call_rows import SeededCall, load_call, seed_call
from fakeredis import FakeAsyncRedis
from obs_helpers import memory_provider
from redis_ttl import assert_expires_in
from resolved_configs import resolved_config
from rest_client_helpers import SimRest, sim_rest
from service_helpers import json_log_stream, log_events
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tts_helpers import VOICE_ID

from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.call_context import CallContextStore, build_call_context
from va_db.call_states import TERMINAL_STATUSES
from va_db.models import CallStatus
from va_db.session import Database
from va_domain.call_context import call_context_key
from va_domain.org import OrgSettings
from va_voice.phrase_cache import PhraseCache
from va_workflows.activities.call_context import (
    CALL_NOT_FOUND,
    PREFETCH_RETRY,
    PREFETCH_TIMEOUT,
    CallContextActivities,
    PrefetchResult,
)
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.prerender import OpenerPrerender

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def activities(database: Database, redis: FakeAsyncRedis) -> CallContextActivities:
    return CallContextActivities(database, CallContextStore(redis), FrozenClock(NOW))


async def _seed(database: Database, status: CallStatus = "queued") -> SeededCall:
    return await seed_call(database, status=status, config=resolved_config("09_voicemail_amd.json"))


async def _run(activities: CallContextActivities, call: SeededCall) -> PrefetchResult:
    request = CallActivityInput(call_id=call.call_id, org_id=call.org_id)
    return await ActivityEnvironment().run(activities.prefetch_call_context, request)


async def test_the_context_of_a_queued_call_is_stored_with_its_ttl(
    database: Database, redis: FakeAsyncRedis, activities: CallContextActivities
) -> None:
    stream = json_log_stream()
    call = await _seed(database)

    result = await _run(activities, call)

    assert result == PrefetchResult(stored=True, status="queued", ttl_s=5400)
    stored = await CallContextStore(redis).load(call.call_id)
    assert stored == build_call_context(await load_call(database, call), now=NOW)
    await assert_expires_in(redis, call_context_key(call.call_id), 5400)
    [event] = [line for line in log_events(stream) if line["event"] == "call_context.stored"]
    assert (event["call_id"], event["org_id"], event["ttl_s"]) == (str(call.call_id), str(call.org_id), 5400)


async def test_the_current_trace_context_travels_with_the_context(
    database: Database, redis: FakeAsyncRedis, activities: CallContextActivities
) -> None:
    provider, _ = memory_provider()
    call = await _seed(database, status="scheduled")

    with provider.get_tracer("test").start_as_current_span("CallWorkflow") as span:
        await _run(activities, call)
        trace_id = format(span.get_span_context().trace_id, "032x")

    stored = await CallContextStore(redis).load(call.call_id)
    assert stored is not None
    assert stored.trace_context["traceparent"].split("-")[1] == trace_id


async def test_outside_a_trace_the_trace_context_is_empty(
    database: Database, redis: FakeAsyncRedis, activities: CallContextActivities
) -> None:
    call = await _seed(database)

    await _run(activities, call)

    stored = await CallContextStore(redis).load(call.call_id)
    assert stored is not None
    assert stored.trace_context == {}


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
async def test_a_terminal_call_gets_no_context(
    database: Database, redis: FakeAsyncRedis, activities: CallContextActivities, status: CallStatus
) -> None:
    stream = json_log_stream()
    call = await _seed(database, status=status)

    assert await _run(activities, call) == PrefetchResult(stored=False, status=status)
    assert await redis.exists(call_context_key(call.call_id)) == 0
    assert [line["status"] for line in log_events(stream) if line["event"] == "call_context.skipped"] == [
        status
    ]


async def test_an_unknown_call_fails_without_retries(activities: CallContextActivities) -> None:
    with pytest.raises(ApplicationError) as caught:
        await _run(activities, SeededCall(org_id=new_id(), call_id=new_id()))

    assert (caught.value.type, caught.value.non_retryable) == (CALL_NOT_FOUND, True)


async def test_another_orgs_call_is_not_found(
    database: Database, redis: FakeAsyncRedis, activities: CallContextActivities
) -> None:
    call = await _seed(database)
    other = await _seed(database)

    with pytest.raises(ApplicationError) as caught:
        await _run(activities, SeededCall(org_id=other.org_id, call_id=call.call_id))

    assert caught.value.type == CALL_NOT_FOUND
    assert await redis.exists(call_context_key(call.call_id)) == 0


def test_the_retry_policy_is_bounded_and_skips_missing_calls() -> None:
    assert timedelta(seconds=20) == PREFETCH_TIMEOUT
    assert PREFETCH_RETRY.maximum_attempts == 3
    assert PREFETCH_RETRY.maximum_interval == timedelta(seconds=5)
    assert list(PREFETCH_RETRY.non_retryable_error_types or []) == [CALL_NOT_FOUND]


def _opener_config(**overrides: object) -> dict[str, object]:
    return {
        **resolved_config(),
        "first_sentence": "Hi {{first_name}}, this is Maya.",
        "request_data": {"first_name": "Ana"},
        "wait_for_greeting": False,
        "provider_voice_id": VOICE_ID,
        **overrides,
    }


@dataclass
class _Orgs:
    async def get(self, org_id: uuid.UUID) -> OrgSettings | None:
        del org_id
        return OrgSettings()


@pytest.fixture
async def phrases() -> AsyncIterator[tuple[SimRest, PhraseCache]]:
    async with sim_rest() as sim:
        cache = PhraseCache(sim.client, FakeAsyncRedis())
        try:
            yield sim, cache
        finally:
            await cache.aclose()


def _prerendering(database: Database, redis: FakeAsyncRedis, cache: PhraseCache) -> CallContextActivities:
    prerender = OpenerPrerender(cache=cache, org_settings=_Orgs(), model_id="eleven_flash_v2_5")
    return CallContextActivities(database, CallContextStore(redis), FrozenClock(NOW), prerender=prerender)


async def test_the_prerendered_opener_is_stored_and_cached(
    database: Database, redis: FakeAsyncRedis, phrases: tuple[SimRest, PhraseCache]
) -> None:
    sim, cache = phrases
    call = await seed_call(database, status="queued", config=_opener_config())

    result = await _run(_prerendering(database, redis, cache), call)

    assert result == PrefetchResult(stored=True, status="queued", ttl_s=result.ttl_s, opener_cached=True)
    stored = await CallContextStore(redis).load(call.call_id)
    assert stored is not None
    assert stored.opener_text == "Hi Ana, this is Maya."
    texts = [record["text"] for record in await sim.records("/sim/elevenlabs/tts/requests", "requests")]
    assert "Hi Ana, this is Maya." in texts


async def test_the_context_is_stored_when_the_render_fails(
    database: Database, redis: FakeAsyncRedis, phrases: tuple[SimRest, PhraseCache]
) -> None:
    sim, cache = phrases
    await sim.send("PUT", "/sim/elevenlabs/tts/fault", {"kind": "error", "times": 100})
    call = await seed_call(database, status="queued", config=_opener_config())

    result = await _run(_prerendering(database, redis, cache), call)

    assert (result.stored, result.opener_cached) == (True, False)
    stored = await CallContextStore(redis).load(call.call_id)
    assert stored is not None
    assert stored.opener_text == "Hi Ana, this is Maya."


async def test_a_terminal_call_renders_nothing(
    database: Database, redis: FakeAsyncRedis, phrases: tuple[SimRest, PhraseCache]
) -> None:
    sim, cache = phrases
    call = await seed_call(database, status="canceled", config=_opener_config())

    result = await _run(_prerendering(database, redis, cache), call)

    assert result == PrefetchResult(stored=False, status="canceled")
    assert await sim.records("/sim/elevenlabs/tts/requests", "requests") == []
