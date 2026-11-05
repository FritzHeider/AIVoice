"""Pre-call dynamic data in ``prefetch_call_context`` (specs/13 §7 Decision P08-T03a).

The clock is the wall clock: ``call_events`` partitions exist only around the migration month, and the
fetch budget waits on the clock.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from call_rows import SeededCall, load_call, seed_call
from fakeredis import FakeAsyncRedis
from prefetch_rig import opener_config, opener_prerender, phrase_cache, run_prefetch
from service_helpers import json_log_stream, log_events
from sqlalchemy import select
from telephony_rig import broken_redis, call_event_rows

from va_common.time import SystemClock
from va_db.call_context import CallContextStore
from va_db.models import WebhookDelivery
from va_db.session import Database
from va_domain.call_context import CallContext
from va_events import DbWebhookRouter, EventPublisher
from va_workflows.activities.call_context import CallContextActivities
from va_workflows.activities.dynamic_data import PreCallDynamicData
from va_workflows.activities.prerender import OpenerPrerender

API = "https://crm.acme.test"
CONTACT = {"contact": {"name": "Ana", "plan": "Pro"}}
FETCH: dict[str, Any] = {
    "url": API + "/contacts?phone={{to}}",
    "headers": {"Authorization": "Bearer {{request_data.crm_token}}"},
    "response_data": [
        {"name": "customer_name", "data": "$.contact.name", "context": "The customer is {{customer_name}}."},
        {"name": "plan", "data": "$.contact.plan", "context": "Their plan is {{plan}}."},
    ],
}


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _config(*fetches: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return opener_config(
        first_sentence="Hi {{customer_name}}, this is Maya.",
        request_data={"crm_token": "tok-123"},
        dynamic_data=list(fetches),
        **overrides,
    )


def _activities(  # noqa: PLR0913 - one keyword per collaborator a test replaces
    database: Database,
    redis: FakeAsyncRedis,
    http: httpx.AsyncClient,
    *,
    events_redis: FakeAsyncRedis | None = None,
    store: CallContextStore | None = None,
    prerender: OpenerPrerender | None = None,
) -> CallContextActivities:
    clock = SystemClock()
    publisher = EventPublisher(
        redis if events_redis is None else events_redis, call_events=None, router=DbWebhookRouter(database)
    )
    dynamic = PreCallDynamicData(
        database=database, redis=redis, http=http, publisher=publisher, clock=clock, allow_http=False
    )
    return CallContextActivities(
        database,
        store if store is not None else CallContextStore(redis),
        clock,
        prerender=prerender,
        dynamic=dynamic,
    )


async def _stored(redis: FakeAsyncRedis, call: SeededCall) -> CallContext:
    context = await CallContextStore(redis).load(call.call_id)
    assert context is not None
    return context


async def _events(database: Database, call: SeededCall) -> list[tuple[str | None, str, str, dict[str, Any]]]:
    return [
        (row.event_type, row.level, row.message, row.data) for row in await call_event_rows(database, call)
    ]


async def test_pre_call_data_lands_in_the_context_the_opener_and_calls_variables(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    call = await seed_call(database, status="queued", config=_config(FETCH), variables={"kept": 1})

    async with phrase_cache() as (sim, cache):
        activities = _activities(database, redis, http, prerender=opener_prerender(cache))
        result = await run_prefetch(activities, call)
        spoken = [record["text"] for record in await sim.records("/sim/elevenlabs/tts/requests", "requests")]

    request = route.calls.last.request
    assert str(request.url) == f"{API}/contacts?phone=%2B14155550100"
    assert request.headers["authorization"] == "Bearer tok-123"
    assert request.headers["x-va-call-id"] == str(call.call_id)
    assert "x-va-signature" not in request.headers
    assert (result.stored, result.dynamic_data, result.opener_cached) == (True, 1, True)
    stored = await _stored(redis, call)
    assert stored.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert stored.context_sections == ["The customer is Ana.", "Their plan is Pro."]
    assert stored.opener_text == "Hi Ana, this is Maya."
    assert "Hi Ana, this is Maya." in spoken
    assert (await load_call(database, call)).variables == {"kept": 1, "customer_name": "Ana", "plan": "Pro"}
    [(event_type, level, message, data)] = await _events(database, call)
    assert (event_type, level, message) == ("call.dynamic_data", "info", "Dynamic data: 2 variables set")
    assert {**data, "duration_ms": 0} == {
        "url": f"{API}/contacts",
        "method": "GET",
        "status_code": 200,
        "duration_ms": 0,
        "variables_set": ["customer_name", "plan"],
        "cached": False,
        "outcome": "ok",
        "timing": "pre_call",
        "index": 0,
    }


async def test_a_failed_fetch_stores_the_context_and_a_warning_event(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    api.get("/contacts").respond(500, json=CONTACT)
    api.get("/orders").respond(200, json={"last": "A-7"})
    orders = {"url": f"{API}/orders", "response_data": [{"name": "order", "data": "$.last"}]}
    call = await seed_call(database, status="queued", config=_config(FETCH, orders))

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (result.stored, result.dynamic_data) == (True, 2)
    stored = await _stored(redis, call)
    assert (stored.variables, stored.context_sections) == ({"order": "A-7"}, [])
    assert (await load_call(database, call)).variables == {"order": "A-7"}
    events = await _events(database, call)
    assert [(level, message) for _, level, message, _ in events] == [
        ("warning", "Dynamic data failed: http_error (500)"),
        ("info", "Dynamic data: 1 variable set"),
    ]
    assert [(data["index"], data["outcome"], data["variables_set"]) for *_, data in events] == [
        (0, "http_error", []),
        (1, "ok", ["order"]),
    ]


@pytest.mark.parametrize(
    "value", ["[" * 300 + "]" * 300, '"a\\u0000b"'], ids=["nested_300_deep", "nul_character"]
)
async def test_a_value_the_stores_cannot_hold_costs_only_that_variable(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter, value: str
) -> None:
    api.get("/contacts").respond(200, json=CONTACT)
    api.get("/blob").respond(
        200,
        content=f'{{"blob": {value}, "tier": "gold"}}'.encode(),
        headers={"content-type": "application/json"},
    )
    blob = {
        "url": f"{API}/blob",
        "response_data": [
            {"name": "blob", "data": "$.blob", "context": "Blob known."},
            {"name": "tier", "data": "$.tier"},
        ],
    }
    call = await seed_call(database, status="queued", config=_config(FETCH, blob))

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (result.stored, result.dynamic_data) == (True, 2)
    expected = {"customer_name": "Ana", "plan": "Pro", "tier": "gold"}
    stored = await _stored(redis, call)
    assert stored.variables == expected
    assert stored.context_sections == ["The customer is Ana.", "Their plan is Pro."]
    assert (await load_call(database, call)).variables == expected
    events = await _events(database, call)
    assert [
        (level, data["index"], data["outcome"], data["variables_set"]) for _, level, _, data in events
    ] == [
        ("info", 0, "ok", ["customer_name", "plan"]),
        ("info", 1, "ok", ["tier"]),
    ]


async def test_when_every_fetch_fails_the_call_keeps_its_variables(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    api.get("/contacts").mock(side_effect=httpx.ConnectError("refused"))
    call = await seed_call(database, status="queued", config=_config(FETCH), variables={"kept": 1})

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (result.stored, result.dynamic_data) == (True, 1)
    assert (await _stored(redis, call)).variables == {}
    assert (await load_call(database, call)).variables == {"kept": 1}
    [(_, level, message, _)] = await _events(database, call)
    assert (level, message) == ("warning", "Dynamic data failed: connection")


async def test_every_turn_specs_are_not_fetched_before_the_call(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    contacts = api.get("/contacts").respond(200, json=CONTACT)
    orders = api.get("/orders").respond(200, json={"last": "A-7"})
    per_turn = {**FETCH, "timing": "every_turn"}
    pre_call = {"url": f"{API}/orders", "response_data": [{"name": "order", "data": "$.last"}]}
    call = await seed_call(database, status="queued", config=_config(per_turn, pre_call))

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (contacts.call_count, orders.call_count, result.dynamic_data) == (0, 1, 1)
    assert (await _stored(redis, call)).variables == {"order": "A-7"}
    [(*_, data)] = await _events(database, call)
    assert (data["index"], data["timing"]) == (1, "pre_call")


async def test_no_dynamic_data_changes_nothing(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    call = await seed_call(database, status="queued", config=_config())

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (result.stored, result.dynamic_data) == (True, 0)
    stored = await _stored(redis, call)
    assert (stored.variables, stored.context_sections) == ({}, [])
    assert await _events(database, call) == []
    assert await redis.keys("dyn:*") == []
    assert api.calls.call_count == 0


async def test_a_terminal_call_fetches_nothing(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    call = await seed_call(database, status="canceled", config=_config(FETCH))

    result = await run_prefetch(_activities(database, redis, http), call)

    assert (result.stored, result.dynamic_data, route.call_count) == (False, 0, 0)
    assert await _events(database, call) == []


async def test_a_second_call_uses_the_cache(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    config = _config({**FETCH, "cache": True})
    first = await seed_call(database, status="queued", config=config)
    second = await seed_call(database, status="queued", config=config, org_id=first.org_id)
    stranger = await seed_call(database, status="queued", config=config)
    activities = _activities(database, redis, http)

    for call in (first, second, stranger):
        await run_prefetch(activities, call)

    assert route.call_count == 2
    assert (await _stored(redis, second)).variables == {"customer_name": "Ana", "plan": "Pro"}
    cached = [
        data["cached"] for call in (first, second, stranger) for *_, data in await _events(database, call)
    ]
    assert cached == [False, True, False]
    [(_, _, message, _)] = await _events(database, second)
    assert message == "Dynamic data: 2 variables set (cached)"
    assert len(await redis.keys("dyn:*")) == 2


async def test_recording_failures_never_fail_the_activity(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    stream = json_log_stream()
    api.get("/contacts").respond(200, json=CONTACT)
    call = await seed_call(database, status="queued", config=_config(FETCH))
    events_redis = broken_redis()

    result = await run_prefetch(_activities(database, redis, http, events_redis=events_redis), call)

    assert (result.stored, result.dynamic_data) == (True, 1)
    assert (await _stored(redis, call)).variables == {"customer_name": "Ana", "plan": "Pro"}
    assert await _events(database, call) == []
    assert (await load_call(database, call)).variables == {}
    [failure] = [
        line for line in log_events(stream) if line["event"] == "call_context.dynamic_data_record_failed"
    ]
    assert (failure["call_id"], failure["error_type"]) == (str(call.call_id), "ApplicationError")
    await events_redis.aclose()


class _FailingOnce(CallContextStore):
    """A store whose first save fails (the Redis connection dropped)."""

    def __init__(self, redis: FakeAsyncRedis) -> None:
        super().__init__(redis)
        self.failures = 1

    async def save(self, context: CallContext) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError
        await super().save(context)


async def test_events_are_recorded_after_the_context_is_saved(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    call = await seed_call(database, status="queued", config=_config(FETCH))
    activities = _activities(database, redis, http, store=_FailingOnce(redis))

    with pytest.raises(ConnectionError):
        await run_prefetch(activities, call)
    assert await _events(database, call) == []
    assert (await load_call(database, call)).variables == {}
    result = await run_prefetch(activities, call)

    assert (result.stored, route.call_count) == (True, 2)
    assert [message for _, _, message, _ in await _events(database, call)] == [
        "Dynamic data: 2 variables set"
    ]
    assert (await load_call(database, call)).variables == {"customer_name": "Ana", "plan": "Pro"}


async def test_the_webhook_outbox_gets_the_event_only_for_a_call_that_asked_for_it(
    database: Database, redis: FakeAsyncRedis, http: httpx.AsyncClient, api: respx.MockRouter
) -> None:
    api.get("/contacts").respond(200, json=CONTACT)
    url = f"https://calls-{secrets.token_hex(4)}.hooks.test/live"
    asked = await seed_call(
        database, status="queued", config=_config(FETCH, webhook=url, webhook_events=["dynamic_data"])
    )
    other = await seed_call(
        database, status="queued", config=_config(FETCH, webhook=url, webhook_events=["call"])
    )
    activities = _activities(database, redis, http)

    deliveries = []
    for call in (asked, other):
        await run_prefetch(activities, call)
        async with database.tenant_session(call.org_id) as session:
            rows = await session.execute(
                select(WebhookDelivery.event_type, WebhookDelivery.url).where(
                    WebhookDelivery.call_id == call.call_id
                )
            )
            deliveries.append([tuple(row) for row in rows])

    assert deliveries == [[("call.dynamic_data", url)], []]
