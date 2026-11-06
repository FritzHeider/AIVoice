"""Dynamic data fetches (specs/13 §7 Decision P08-T03a) over respx and fakeredis."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
import pytest
import respx
from clock_helpers import ManualClock, RecordingClock
from fakeredis import FakeAsyncRedis, FakeServer
from redis_ttl import assert_expires_in
from service_helpers import json_log_stream, log_events
from tools_helpers import (
    API,
    ORG,
    HeldAnswer,
    held_reader,
    manual_fetcher,
    plain_executor,
    read_waits,
    thread_recorder,
)

import va_tools.dynamic_data
import va_tools.http
from va_domain.calls.tools import DynamicDataSpec
from va_engine.ports import HttpCall, HttpOutcome
from va_engine.templating import TemplateScope
from va_tools.dynamic_data import (
    CONTEXT_LIMIT,
    MAX_CACHED_BYTES,
    MAX_VARIABLE_BYTES,
    MAX_VARIABLE_DEPTH,
    DynamicDataFetcher,
    DynamicDataResult,
    cache_key,
    merge_results,
)
from va_tools.http import USER_AGENT
from va_tools.offload import READER_THREAD

OTHER_ORG = uuid.UUID("0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d72")
BUDGET_S = 6.0
SCOPE = TemplateScope(
    variables={"tier": "gold"},
    request_data={"crm_token": "tok-123", "limit": 3},
    builtins={"to": "+14155550123", "from": "+14155550100"},
)
CONTACT: dict[str, Any] = {"contact": {"name": "Ana", "plan": "Pro", "notes": None}}
ITEMS = [
    {
        "name": "customer_name",
        "data": "$.contact.name",
        "context": "The customer's name is {{customer_name}}.",
    },
    {"name": "plan", "data": "$.contact.plan", "context": "Their plan is {{plan}} ({{tier}})."},
]


def _spec(**fields: Any) -> DynamicDataSpec:
    values: dict[str, Any] = {"url": f"{API}/contacts?phone={{{{to}}}}", "response_data": ITEMS}
    return DynamicDataSpec.model_validate({**values, **fields})


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def redis(server: FakeServer) -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(server=server)
    yield client
    await client.aclose()


async def _stored(redis: FakeAsyncRedis, key: str) -> Any:
    raw = await redis.get(key)
    assert raw is not None
    return json.loads(raw)


async def _fetch(  # noqa: PLR0913 - one keyword per fetcher knob keeps the tests explicit
    spec: DynamicDataSpec,
    *,
    redis: FakeAsyncRedis | None = None,
    org_id: uuid.UUID = ORG,
    index: int = 0,
    clock: RecordingClock | None = None,
    max_cached_bytes: int = MAX_CACHED_BYTES,
) -> DynamicDataResult:
    async with plain_executor(clock=clock) as (executor, recording):
        fetcher = DynamicDataFetcher(
            executor, redis=redis, org_id=org_id, clock=recording, max_cached_bytes=max_cached_bytes
        )
        return await fetcher.fetch(spec, index, SCOPE, timeout_ms=5_000)


async def _fetch_waiting(spec: DynamicDataSpec, *, redis: FakeAsyncRedis | None = None) -> DynamicDataResult:
    """One fetch on a manual clock, for answers read on the reader thread: the executor waits for the
    read (``_fetch``'s frozen clock would pass the read's deadline at once)."""
    async with manual_fetcher(redis=redis) as (_clock, fetcher):
        return await fetcher.fetch(spec, 0, SCOPE, timeout_ms=5_000)


async def test_a_templated_request_renders_url_query_headers_and_body(api: respx.MockRouter) -> None:
    route = api.post("/contacts").respond(200, json=CONTACT)
    spec = _spec(
        method="POST",
        headers={"Authorization": "Bearer {{request_data.crm_token}}"},
        query={"tier": "{{tier}}"},
        body={"phone": "{{to}}", "limit": "{{limit}}", "note": "for {{tier}}"},
    )

    result = await _fetch(spec)

    request = route.calls.last.request
    assert str(request.url) == f"{API}/contacts?phone=%2B14155550123&tier=gold"
    assert request.headers["authorization"] == "Bearer tok-123"
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["x-va-call-id"] == "call-7"
    assert json.loads(request.content) == {"phone": "+14155550123", "limit": 3, "note": "for gold"}
    assert (result.outcome, result.status_code, result.method, result.cached) == ("ok", 200, "POST", False)


async def test_response_data_sets_variables_and_renders_context_after_extraction(
    api: respx.MockRouter,
) -> None:
    api.get("/contacts").respond(200, json=CONTACT)

    result = await _fetch(_spec(), index=2)

    assert result.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert result.sections == {
        "customer_name": "The customer's name is Ana.",
        "plan": "Their plan is Pro (gold).",
    }
    assert (result.index, result.timing, result.failed, result.level) == (2, "pre_call", False, "info")


async def test_a_null_or_missing_value_sets_no_variable_and_no_context(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(200, json=CONTACT)
    items = [
        {"name": "notes", "data": "$.contact.notes", "context": "Notes: {{notes}}"},
        {"name": "missing", "data": "$.contact.nope", "context": "Missing: {{missing}}"},
        {"name": "customer_name", "data": "$.contact.name"},
        {"name": "plan", "data": "$.contact.plan", "context": "   "},
    ]

    result = await _fetch(_spec(response_data=items))

    assert result.outcome == "ok"
    assert result.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert result.sections == {}


async def test_of_two_items_with_one_name_the_later_counts(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(200, json=CONTACT)
    items = [
        {"name": "who", "data": "$.contact.name", "context": "First: {{who}}"},
        {"name": "who", "data": "$.contact.plan", "context": "Second: {{who}}"},
    ]

    result = await _fetch(_spec(response_data=items))

    assert result.variables == {"who": "Pro"}
    assert result.sections == {"who": "Second: Pro"}


@pytest.mark.parametrize(
    "content",
    [b"not json", b"<html>Bad gateway</html>", b"", b'"gold"', b"null"],
    ids=["text", "html", "empty", "json-string", "json-null"],
)
async def test_a_2xx_answer_that_is_no_json_document_is_ok_unreadable_and_sets_nothing(
    api: respx.MockRouter, content: bytes
) -> None:
    api.get("/contacts").respond(200, content=content)

    result = await _fetch(_spec())

    assert (result.outcome, result.readable, result.variables, result.sections) == ("ok", False, {}, {})
    assert (result.level, result.message()) == ("info", "Dynamic data: 0 variables set")


@pytest.mark.parametrize("content", [b"{}", b"[]", b"0", b"false"], ids=["object", "array", "zero", "false"])
async def test_any_whole_json_document_is_readable(api: respx.MockRouter, content: bytes) -> None:
    api.get("/contacts").respond(200, content=content, headers={"content-type": "application/json"})

    result = await _fetch(_spec())

    assert (result.outcome, result.readable, result.variables) == ("ok", True, {})


async def test_a_failure_is_never_readable(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(500, json=CONTACT)

    result = await _fetch(_spec())

    assert (result.outcome, result.readable, result.variables) == ("http_error", False, {})


async def test_an_answer_cut_at_the_size_cap_sets_nothing_even_when_it_parses() -> None:
    class Cut:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            del call
            return HttpOutcome(
                kind="ok",
                status=200,
                body={"contact": {"name": "An"}},
                truncated=True,
                extracted={"customer_name": "An", "plan": None},
            )

    fetcher = DynamicDataFetcher(Cut(), redis=None, org_id=ORG, clock=RecordingClock())

    result = await fetcher.fetch(_spec(), 0, SCOPE, timeout_ms=5_000)

    assert (result.outcome, result.readable, result.variables, result.sections) == ("ok", False, {}, {})


# The cache.


async def test_a_cached_fetch_sends_no_request_and_reports_cached(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis)
    second = await _fetch(spec, redis=redis)

    assert route.call_count == 1
    assert (first.cached, second.cached) == (False, True)
    assert (second.outcome, second.status_code) == ("ok", 200)
    assert (second.variables, second.sections) == (first.variables, first.sections)
    assert second.message() == "Dynamic data: 2 variables set (cached)"
    assert (first.readable, second.readable) == (True, True)


@pytest.mark.parametrize(
    ("path", "on_the_reader"),
    [
        ("$..plan", True),
        ("$.contact.plan", False),
        ("$.*.plan", False),
        ("$.*.`this`.plan", True),
        ("$.contact.$.contact.plan", True),
    ],
    ids=["descendant", "direct", "descending-small", "named-operator", "root-again"],
)
async def test_a_cached_answer_is_read_under_the_same_rule(
    api: respx.MockRouter,
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    *,
    on_the_reader: bool,
) -> None:
    """specs/13 §9 Decision P08-FIX-25: a stored value is parsed and searched where a fetched answer
    with these paths would be, and gives what the fetch gave."""
    route = api.get("/contacts").respond(200, json=CONTACT)
    spec = _spec(cache=True, response_data=[{"name": "plan", "data": path, "context": "Plan: {{plan}}."}])
    first = await _fetch_waiting(spec, redis=redis)
    parsed = thread_recorder(monkeypatch, va_tools.dynamic_data, "_cached_answer")
    searched = thread_recorder(monkeypatch, va_tools.dynamic_data, "extract_all")

    second = await _fetch_waiting(spec, redis=redis)

    assert (route.call_count, first.cached, second.cached) == (1, False, True)
    assert (second.outcome, second.status_code, second.readable) == ("ok", 200, True)
    assert (second.variables, second.sections) == ({"plan": "Pro"}, {"plan": "Plan: Pro."})
    assert (second.variables, second.sections) == (first.variables, first.sections)
    assert (len(parsed), len(searched)) == (1, 1)
    if on_the_reader:
        assert all(name.startswith(READER_THREAD) for name in parsed + searched)
    else:
        assert parsed + searched == [threading.current_thread().name] * 2


@pytest.mark.parametrize("stored", ["not json", "[1, 2]"], ids=["text", "list"])
async def test_a_stored_value_that_is_no_answer_is_a_miss_on_the_reader_too(
    api: respx.MockRouter, redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    stream = json_log_stream()
    route = api.get("/contacts").respond(200, json=CONTACT)
    key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)
    await redis.set(key, stored)
    parsed = thread_recorder(monkeypatch, va_tools.dynamic_data, "_cached_answer")

    spec = _spec(cache=True, response_data=[{"name": "plan", "data": "$..plan"}])
    result = await _fetch_waiting(spec, redis=redis)

    assert (result.outcome, result.cached, route.call_count) == ("ok", False, 1)
    assert result.variables == {"plan": "Pro"}
    assert len(parsed) == 1
    assert parsed[0].startswith(READER_THREAD)
    assert await _stored(redis, key) == {"status": 200, "body": CONTACT}
    assert "tools.dynamic_data_cache_invalid" in [line["event"] for line in log_events(stream)]


UNREADABLE = [b"<html>Bad gateway</html>", b"", b'"maintenance"', b"null"]
UNREADABLE_IDS = ["html", "empty", "json-string", "json-null"]


@pytest.mark.parametrize("content", UNREADABLE, ids=UNREADABLE_IDS)
async def test_a_2xx_answer_that_is_no_json_document_is_not_cached(
    api: respx.MockRouter, redis: FakeAsyncRedis, content: bytes
) -> None:
    """Such an answer says nothing about the data: like a 500, it must not keep the endpoint from being
    asked again for the whole TTL."""
    route = api.get("/contacts").mock(
        side_effect=[httpx.Response(200, content=content), httpx.Response(200, json=CONTACT)]
    )
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis)
    assert await redis.keys("dyn:*") == []
    second = await _fetch(spec, redis=redis)
    third = await _fetch(spec, redis=redis)

    assert (first.outcome, first.readable, first.cached, first.variables) == ("ok", False, False, {})
    assert (second.readable, second.cached, second.variables) == (
        True,
        False,
        {"customer_name": "Ana", "plan": "Pro"},
    )
    assert (third.cached, third.variables, route.call_count) == (True, second.variables, 2)


async def test_a_no_content_answer_is_not_cached(api: respx.MockRouter, redis: FakeAsyncRedis) -> None:
    route = api.get("/contacts").respond(204)
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis)
    second = await _fetch(spec, redis=redis)

    assert (first.outcome, first.status_code, second.cached, route.call_count) == ("ok", 204, False, 2)
    assert await redis.keys("dyn:*") == []


async def test_a_stored_answer_that_is_no_json_document_stays_a_miss_while_the_endpoint_is_unreadable(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    route = api.get("/contacts").respond(200, text="still down")
    key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)
    await redis.set(key, '{"status":200,"body":"not json"}', ex=300)

    first = await _fetch(_spec(cache=True), redis=redis)
    second = await _fetch(_spec(cache=True), redis=redis)

    assert [(r.outcome, r.cached, r.readable, r.variables) for r in (first, second)] == [
        ("ok", False, False, {})
    ] * 2
    assert route.call_count == 2


async def test_the_cache_entry_expires_with_cache_ttl_seconds(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    api.get("/contacts").respond(200, json=CONTACT)

    await _fetch(_spec(cache=True), redis=redis)
    await _fetch(_spec(cache=True, cache_ttl_seconds=7200, query={"v": "2"}), redis=redis)

    default_key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)
    long_key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123&v=2", None)
    assert sorted(await redis.keys("dyn:*")) == sorted([default_key.encode(), long_key.encode()])
    await assert_expires_in(redis, default_key, 300)
    await assert_expires_in(redis, long_key, 7200)
    assert await _stored(redis, default_key) == {"status": 200, "body": CONTACT}


@pytest.mark.parametrize("fields", [{"cache": False}, {"cache": True, "cache_ttl_seconds": 0}])
async def test_cache_false_or_ttl_zero_never_touches_redis(
    api: respx.MockRouter, server: FakeServer, redis: FakeAsyncRedis, fields: dict[str, Any]
) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)
    await redis.set("unrelated", "1")
    server.connected = False

    first = await _fetch(_spec(**fields), redis=redis)
    second = await _fetch(_spec(**fields), redis=redis)

    assert route.call_count == 2
    assert (first.outcome, second.outcome, second.cached) == ("ok", "ok", False)
    server.connected = True
    assert await redis.keys("*") == [b"unrelated"]


@pytest.mark.parametrize(
    ("change", "shared"),
    [
        ({}, True),
        ({"headers": {"Authorization": "Bearer other"}}, True),
        ({"org_id": OTHER_ORG}, False),
        ({"method": "POST"}, False),
        ({"query": {"page": "2"}}, False),
        ({"method": "POST", "body": {"a": 1}}, False),
    ],
    ids=["same", "headers", "org", "method", "url", "body"],
)
async def test_the_cache_key_is_per_org_method_url_and_body(
    api: respx.MockRouter, redis: FakeAsyncRedis, change: dict[str, Any], shared: bool
) -> None:
    route = api.route(path="/contacts").respond(200, json=CONTACT)
    org_id = change.get("org_id", ORG)
    fields = {name: value for name, value in change.items() if name != "org_id"}

    await _fetch(_spec(cache=True), redis=redis)
    result = await _fetch(_spec(cache=True, **fields), redis=redis, org_id=org_id)

    assert result.cached is shared
    assert route.call_count == (1 if shared else 2)


def test_the_cache_key_is_the_documented_hash() -> None:
    key = cache_key(ORG, "post", "https://x.test/a?b=1", {"b": 2, "a": [1, "é"]})
    again = cache_key(ORG, "POST", "https://x.test/a?b=1", {"a": [1, "é"], "b": 2})

    assert key == again
    assert key.startswith(f"dyn:{ORG}:")
    assert len(key.rsplit(":", 1)[1]) == 64
    assert cache_key(ORG, "GET", "https://x.test/", None) != cache_key(ORG, "GET", "https://x.test/", "")
    assert cache_key(ORG, "GET", "https://x.test/", {"n": 2**70}).startswith("dyn:")


async def test_a_failed_fetch_is_not_cached(api: respx.MockRouter, redis: FakeAsyncRedis) -> None:
    route = api.get("/contacts").mock(
        side_effect=[httpx.Response(500, json=CONTACT), httpx.Response(200, json=CONTACT)]
    )
    spec = _spec(cache=True)

    failed = await _fetch(spec, redis=redis)
    assert await redis.keys("dyn:*") == []
    fetched = await _fetch(spec, redis=redis)

    assert (failed.outcome, failed.status_code, failed.variables) == ("http_error", 500, {})
    assert (fetched.outcome, fetched.cached, route.call_count) == ("ok", False, 2)


async def test_an_oversized_body_is_not_cached(api: respx.MockRouter, redis: FakeAsyncRedis) -> None:
    body = {**CONTACT, "padding": "x" * MAX_CACHED_BYTES}
    route = api.get("/contacts").respond(200, json=body)
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis)
    second = await _fetch(spec, redis=redis)

    assert (first.variables, second.cached, route.call_count) == (
        {"customer_name": "Ana", "plan": "Pro"},
        False,
        2,
    )
    assert await redis.keys("dyn:*") == []


def _padded(size: int) -> dict[str, Any]:
    """The contact answer padded so that its compact JSON is exactly ``size`` bytes."""
    body = {**CONTACT, "padding": ""}
    room = size - len(json.dumps(body, separators=(",", ":")).encode())
    assert room >= 0
    return {**CONTACT, "padding": "x" * room}


TURN_LIMIT = 65_536
CONTACTS_KEY = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)


async def test_a_body_of_exactly_the_fetcher_s_limit_is_stored_and_hit_by_it(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    route = api.get("/contacts").respond(200, json=_padded(TURN_LIMIT))
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis, max_cached_bytes=TURN_LIMIT)
    second = await _fetch(spec, redis=redis, max_cached_bytes=TURN_LIMIT)

    assert (first.cached, second.cached, route.call_count) == (False, True, 1)
    assert second.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert await _stored(redis, CONTACTS_KEY) == {"status": 200, "body": _padded(TURN_LIMIT)}


async def test_an_answer_above_the_fetcher_s_limit_is_not_stored(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    """The limit is the fetcher's: the default one stores the same answer."""
    route = api.get("/contacts").respond(200, json=_padded(TURN_LIMIT + 1))
    spec = _spec(cache=True)

    first = await _fetch(spec, redis=redis, max_cached_bytes=TURN_LIMIT)
    stored_by_the_smaller_limit = await redis.keys("dyn:*")
    second = await _fetch(spec, redis=redis, max_cached_bytes=TURN_LIMIT)
    await _fetch(spec, redis=redis)

    assert first.variables == second.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert (first.cached, second.cached, stored_by_the_smaller_limit) == (False, False, [])
    assert route.call_count == 3
    assert await redis.keys("dyn:*") == [CONTACTS_KEY.encode()]


async def test_a_stored_answer_above_the_fetcher_s_limit_is_a_miss(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    """What the worker's pre-call fetch stored (up to 262 144 bytes) is no hit for a fetcher that reads
    less: it asks the endpoint, and a fetcher with the default limit still hits the stored answer."""
    stream = json_log_stream()
    route = api.get("/contacts").respond(200, json=CONTACT)
    stored = {"status": 200, "body": _padded(70_000)}
    await redis.set(CONTACTS_KEY, json.dumps(stored, separators=(",", ":")))
    spec = _spec(cache=True)

    by_default = await _fetch(spec, redis=redis)
    sent_by_default = route.call_count
    limited = await _fetch(spec, redis=redis, max_cached_bytes=TURN_LIMIT)

    assert (by_default.cached, by_default.readable, sent_by_default) == (True, True, 0)
    assert (limited.outcome, limited.cached, limited.readable, route.call_count) == ("ok", False, True, 1)
    assert await _stored(redis, CONTACTS_KEY) == {"status": 200, "body": CONTACT}
    events = [line["event"] for line in log_events(stream)]
    assert events.count("tools.dynamic_data_cache_oversized") == 1
    assert "tools.dynamic_data_cache_invalid" not in events


async def test_the_stored_size_is_counted_in_bytes_for_a_client_that_returns_text(
    api: respx.MockRouter, server: FakeServer
) -> None:
    """A ``decode_responses`` client hands the value back as ``str``: 40 000 two-byte characters are
    80 000 bytes, above the limit, although they are fewer characters than it."""
    route = api.get("/contacts").respond(200, json=CONTACT)
    text_client = FakeAsyncRedis(server=server, decode_responses=True)
    stored = {"status": 200, "body": {**CONTACT, "padding": "é" * 40_000}}
    await text_client.set(CONTACTS_KEY, json.dumps(stored, separators=(",", ":"), ensure_ascii=False))

    limited = await _fetch(_spec(cache=True), redis=text_client, max_cached_bytes=TURN_LIMIT)
    await text_client.aclose()

    assert (limited.cached, route.call_count) == (False, 1)


async def test_a_truncated_body_is_not_cached(redis: FakeAsyncRedis) -> None:
    class Truncating:
        calls = 0

        async def execute(self, call: HttpCall) -> HttpOutcome:
            del call
            self.calls += 1
            return HttpOutcome(kind="ok", status=200, body="{", truncated=True)

    port = Truncating()
    fetcher = DynamicDataFetcher(port, redis=redis, org_id=ORG, clock=RecordingClock())

    first = await fetcher.fetch(_spec(cache=True), 0, SCOPE, timeout_ms=5_000)
    second = await fetcher.fetch(_spec(cache=True), 0, SCOPE, timeout_ms=5_000)

    assert (first.outcome, second.cached, port.calls) == ("ok", False, 2)
    assert await redis.keys("dyn:*") == []


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "[1, 2]",
        '{"status": "200", "body": {}}',
        '{"status": true, "body": 1}',
        '{"status": 200}',
        "[" * 100_000,
        '{"status": 200, "body": "<html>Bad gateway</html>"}',
        '{"status": 204, "body": null}',
    ],
    ids=["text", "list", "text-status", "bool-status", "no-body", "too-deep", "text-body", "null-body"],
)
async def test_a_stored_value_that_is_no_usable_answer_is_a_miss(
    api: respx.MockRouter, redis: FakeAsyncRedis, stored: str
) -> None:
    """Not the stored shape, or (as a build before 2026-11-06 could store) an answer that is no JSON
    document: the endpoint is asked and its readable answer replaces the entry."""
    stream = json_log_stream()
    route = api.get("/contacts").respond(200, json=CONTACT)
    key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)
    await redis.set(key, stored)

    result = await _fetch(_spec(cache=True), redis=redis)

    assert (result.outcome, result.cached, result.readable, route.call_count) == ("ok", False, True, 1)
    assert result.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert await _stored(redis, key) == {"status": 200, "body": CONTACT}
    assert "tools.dynamic_data_cache_invalid" in [line["event"] for line in log_events(stream)]


async def test_a_redis_outage_fetches_without_the_cache(
    api: respx.MockRouter, server: FakeServer, redis: FakeAsyncRedis
) -> None:
    stream = json_log_stream()
    route = api.get("/contacts").respond(200, json=CONTACT)
    server.connected = False

    result = await _fetch(_spec(cache=True), redis=redis)

    assert (result.outcome, result.cached, route.call_count) == ("ok", False, 1)
    assert result.variables == {"customer_name": "Ana", "plan": "Pro"}
    events = [line["event"] for line in log_events(stream)]
    assert "tools.dynamic_data_cache_unavailable" in events
    assert "tools.dynamic_data_cache_write_failed" in events


# Failures.


@pytest.mark.parametrize(
    ("answer", "url", "expected"),
    [
        (
            httpx.Response(500, json=CONTACT),
            None,
            ("http_error", 500, "Dynamic data failed: http_error (500)"),
        ),
        (
            httpx.Response(404, json=CONTACT),
            None,
            ("http_error", 404, "Dynamic data failed: http_error (404)"),
        ),
        (httpx.ReadTimeout("slow"), None, ("timeout", None, "Dynamic data failed: timeout")),
        (httpx.ConnectError("refused"), None, ("connection", None, "Dynamic data failed: connection")),
        (None, "http://api.acme.test/contacts", ("blocked", None, "Dynamic data failed: blocked")),
        (None, "https:///contacts", ("invalid_url", None, "Dynamic data failed: invalid_url")),
    ],
    ids=["500", "404", "timeout", "connection", "blocked", "invalid_url"],
)
async def test_failures_set_nothing_and_are_warnings(
    api: respx.MockRouter,
    answer: httpx.Response | Exception | None,
    url: str | None,
    expected: tuple[str, int | None, str],
) -> None:
    route = api.get("/contacts").mock(side_effect=None if answer is None else [answer])
    clock = RecordingClock()

    result = await _fetch(_spec(**({} if url is None else {"url": url})), clock=clock)

    assert (result.outcome, result.status_code, result.message()) == expected
    assert (result.variables, result.sections, result.cached) == ({}, {}, False)
    assert (result.failed, result.level) == (True, "warning")
    assert route.call_count == (0 if answer is None else 1)
    assert clock.sleeps == []


async def test_a_template_that_does_not_parse_is_a_render_error(api: respx.MockRouter) -> None:
    route = api.get("/contacts").respond(200, json=CONTACT)

    result = await _fetch(_spec(headers={"X-Token": "{{request_data.crm_token"}))

    assert (result.outcome, result.url, result.status_code) == ("render_error", "", None)
    assert (result.level, result.variables) == ("warning", {})
    assert route.call_count == 0


async def test_a_port_that_raises_is_an_error_outcome(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    route = api.post("/contacts").respond(200, json=CONTACT)

    class Broken:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            raise RuntimeError(call.url)

    beyond_64_bits = await _fetch(_spec(method="POST", body={"n": 2**70}))
    fetcher = DynamicDataFetcher(Broken(), redis=None, org_id=ORG, clock=RecordingClock())
    broken = await fetcher.fetch(_spec(), 1, SCOPE, timeout_ms=5_000)

    assert route.call_count == 0
    for result in (beyond_64_bits, broken):
        assert (result.outcome, result.level, result.variables, result.url) == ("error", "warning", {}, "")
        assert result.message() == "Dynamic data failed: error"
    failures = [line for line in log_events(stream) if line["event"] == "tools.dynamic_data_failed"]
    assert [(line["index"], line["error_type"]) for line in failures] == [
        (0, "TypeError"),
        (1, "RuntimeError"),
    ]
    assert "api.acme.test/contacts" not in stream.getvalue()


async def test_a_context_template_that_does_not_parse_gives_no_line(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(200, json=CONTACT)
    items = [
        {"name": "customer_name", "data": "$.contact.name", "context": "Name: {{customer_name"},
        {"name": "plan", "data": "$.contact.plan", "context": "Plan: {{plan}}"},
    ]

    result = await _fetch(_spec(response_data=items))

    assert result.variables == {"customer_name": "Ana", "plan": "Pro"}
    assert result.sections == {"plan": "Plan: Pro"}


async def test_a_value_over_the_limit_is_not_set(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    fits = "x" * (MAX_VARIABLE_BYTES - 2)
    api.get("/contacts").respond(
        200, json={"fits": fits, "big": fits + "x", "wide": "é" * (MAX_VARIABLE_BYTES // 2)}
    )
    items = [
        {"name": "fits", "data": "$.fits"},
        {"name": "big", "data": "$.big", "context": "Big: {{big}}"},
        {"name": "wide", "data": "$.wide"},
    ]

    result = await _fetch(_spec(response_data=items))

    assert result.variables == {"fits": fits}
    assert result.sections == {}
    too_large = [line for line in log_events(stream) if line["event"] == "tools.dynamic_data_value_too_large"]
    assert [line["name"] for line in too_large] == ["big", "wide"]


def _nested(levels: int) -> str:
    """JSON text of ``levels`` arrays inside one another."""
    return "[" * levels + "]" * levels


def _answers_blob(api: respx.MockRouter, value: str) -> respx.Route:
    """``/contacts`` answers ``blob`` (the JSON text ``value``) next to a plan."""
    body = f'{{"blob": {value}, "plan": "Pro"}}'
    return api.get("/contacts").respond(
        200, content=body.encode(), headers={"content-type": "application/json"}
    )


BLOB_ITEMS = [
    {"name": "blob", "data": "$.blob", "context": "Blob known."},
    {"name": "plan", "data": "$.plan", "context": "Plan: {{plan}}"},
]
UNSTORABLE = {
    "nested_arrays": (_nested(MAX_VARIABLE_DEPTH + 1), "too_deep"),
    "nested_objects": ('{"a":' * (MAX_VARIABLE_DEPTH + 1) + "1" + "}" * (MAX_VARIABLE_DEPTH + 1), "too_deep"),
    "nul_in_text": ('"a\\u0000b"', "nul_character"),
    "nul_in_nested_text": ('{"notes": ["fine", "a\\u0000b"]}', "nul_character"),
    "nul_in_nested_name": ('[{"a\\u0000b": 1}]', "nul_character"),
}


@pytest.mark.parametrize("case", sorted(UNSTORABLE))
async def test_a_value_the_stores_cannot_hold_is_not_set_fetched_or_cached(
    api: respx.MockRouter, redis: FakeAsyncRedis, case: str
) -> None:
    stream = json_log_stream()
    value, reason = UNSTORABLE[case]
    route = _answers_blob(api, value)
    spec = _spec(response_data=BLOB_ITEMS, cache=True)

    first = await _fetch(spec, redis=redis)
    second = await _fetch(spec, redis=redis)

    assert route.call_count == 1
    assert (first.cached, second.cached) == (False, True)
    for result in (first, second):
        assert result.outcome == "ok"
        assert result.variables == {"plan": "Pro"}
        assert result.sections == {"plan": "Plan: Pro"}
        assert result.event_data()["variables_set"] == ["plan"]
    skipped = [line for line in log_events(stream) if line["event"] == "tools.dynamic_data_value_unstorable"]
    assert [(line["name"], line["reason"], line["level"]) for line in skipped] == [
        ("blob", reason, "warning")
    ] * 2
    assert "a\\u0000b" not in stream.getvalue()


async def test_a_value_nested_to_the_limit_is_set(api: respx.MockRouter) -> None:
    inner = '{"count": 1, "ok": true, "none": null, "text": "x"}'
    value = "[" * (MAX_VARIABLE_DEPTH - 1) + inner + "]" * (MAX_VARIABLE_DEPTH - 1)
    _answers_blob(api, value)

    result = await _fetch(_spec(response_data=BLOB_ITEMS))

    assert result.variables == {"blob": json.loads(value), "plan": "Pro"}
    assert result.sections == {"blob": "Blob known.", "plan": "Plan: Pro"}


async def test_an_answer_nested_as_deep_as_the_parser_allows_keeps_its_other_variables(
    api: respx.MockRouter, redis: FakeAsyncRedis
) -> None:
    _answers_blob(api, _nested(1_000))

    result = await _fetch(_spec(response_data=BLOB_ITEMS, cache=True), redis=redis)

    assert (result.outcome, result.variables) == ("ok", {"plan": "Pro"})


async def test_a_long_context_line_is_cut(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(200, json={"bio": "b" * 3_000})
    items = [{"name": "bio", "data": "$.bio", "context": "  Bio: {{bio}}  "}]

    result = await _fetch(_spec(response_data=items))

    line = result.sections["bio"]
    assert len(line) == CONTEXT_LIMIT
    assert line.startswith("Bio: bbb")
    assert line.endswith("b…")


# Reporting.


async def test_event_data_has_no_query_and_no_header_values(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    api.get("/contacts").respond(200, json=CONTACT)
    spec = _spec(
        url="https://user:pw-9@api.acme.test/contacts?api_key=sk-query-1#frag",
        headers={"Authorization": "Bearer {{request_data.crm_token}}"},
        query={"phone": "{{to}}"},
    )
    clock = RecordingClock()

    result = await _fetch(spec, index=3, clock=clock)

    assert result.event_data() == {
        "url": f"{API}/contacts",
        "method": "GET",
        "status_code": 200,
        "duration_ms": 0,
        "variables_set": ["customer_name", "plan"],
        "cached": False,
        "outcome": "ok",
        "timing": "pre_call",
        "index": 3,
    }
    shown = json.dumps([result.event_data(), result.message()]) + stream.getvalue()
    for secret in ("sk-query-1", "tok-123", "pw-9", "14155550123", "Ana", "Pro", "/contacts?"):
        assert secret not in shown
    [line] = [line for line in log_events(stream) if line["event"] == "tools.dynamic_data"]
    assert {name: line[name] for name in ("index", "timing", "outcome", "status", "cached", "variables")} == {
        "index": 3,
        "timing": "pre_call",
        "outcome": "ok",
        "status": 200,
        "cached": False,
        "variables": 2,
    }


async def test_the_duration_is_measured_on_the_clock() -> None:
    clock = RecordingClock()

    class Slow:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            del call
            await clock.sleep(0.25)
            return HttpOutcome(kind="ok", status=200, body=CONTACT, extracted={"customer_name": "Ana"})

    fetcher = DynamicDataFetcher(Slow(), redis=None, org_id=ORG, clock=clock)

    result = await fetcher.fetch(_spec(timing="every_turn"), 0, SCOPE, timeout_ms=800)

    assert (result.duration_ms, result.timing) == (250, "every_turn")
    assert result.message() == "Dynamic data: 1 variable set"


def _result(index: int, variables: dict[str, Any], sections: dict[str, str]) -> DynamicDataResult:
    return DynamicDataResult(
        index=index,
        timing="pre_call",
        outcome="ok",
        url=f"{API}/contacts",
        method="GET",
        status_code=200,
        variables=variables,
        sections=sections,
    )


def test_merge_results_later_fetch_wins_names_and_lines() -> None:
    late = _result(2, {"plan": "Max", "name": "Ana B."}, {"plan": "Plan is Max."})
    early = _result(
        0, {"name": "Ana", "plan": "Pro", "city": "Rome"}, {"name": "Name is Ana.", "city": "In Rome."}
    )
    failed = DynamicDataResult(index=1, timing="pre_call", outcome="timeout", url="", method="GET")

    variables, lines = merge_results([late, early, failed])

    assert variables == {"name": "Ana B.", "plan": "Max", "city": "Rome"}
    assert lines == ["In Rome.", "Plan is Max."]
    assert merge_results([]) == ({}, [])


# Several fetches.


@asynccontextmanager
async def _fetching_all(
    specs: list[tuple[int, DynamicDataSpec]], *, timeout_ms: int = 5_000
) -> AsyncIterator[tuple[ManualClock, asyncio.Task[list[DynamicDataResult]]]]:
    """``fetch_all`` of ``specs`` running over the real executor on a manual clock (budget ``BUDGET_S``)."""
    async with manual_fetcher() as (clock, fetcher):
        running = asyncio.create_task(
            fetcher.fetch_all(specs, SCOPE, timeout_ms=timeout_ms, budget_s=BUDGET_S)
        )
        try:
            yield clock, running
        finally:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running


async def test_fetch_all_runs_concurrently_in_one_scope_and_keeps_spec_order(api: respx.MockRouter) -> None:
    contacts = HeldAnswer(CONTACT)
    orders = HeldAnswer({"last": {"id": "A-7"}})
    api.get("/contacts").mock(side_effect=contacts)
    orders_route = api.get("/orders").mock(side_effect=orders)
    specs = [
        (
            4,
            _spec(
                url=f"{API}/orders?name={{{{customer_name}}}}",
                response_data=[{"name": "order", "data": "$.last.id"}],
            ),
        ),
        (1, _spec()),
    ]
    async with _fetching_all(specs) as (_clock, running):
        async with asyncio.timeout(5):
            await contacts.arrived.wait()
            await orders.arrived.wait()
        orders.gate.set()
        contacts.gate.set()
        results = await running

    assert [(result.index, result.outcome) for result in results] == [(4, "ok"), (1, "ok")]
    assert [result.variables for result in results] == [
        {"order": "A-7"},
        {"customer_name": "Ana", "plan": "Pro"},
    ]
    assert str(orders_route.calls.last.request.url) == f"{API}/orders?name="


async def test_fetch_all_reports_what_missed_the_budget_as_timeout(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    slow = HeldAnswer({"last": {"id": "A-7"}})
    api.get("/contacts").respond(200, json=CONTACT)
    api.get("/orders").mock(side_effect=slow)
    specs = [
        (0, _spec()),
        (1, _spec(url=f"{API}/orders?token=sk-9", response_data=[{"name": "order", "data": "$.last.id"}])),
        (2, _spec(url=f"{API}/orders?x={{{{to", response_data=[])),
    ]
    async with _fetching_all(specs) as (clock, running):
        async with asyncio.timeout(5):
            await slow.arrived.wait()
        await clock.wait_for_sleep(BUDGET_S)
        await clock.run_for(BUDGET_S)
        results = await running

    assert [(result.index, result.outcome) for result in results] == [
        (0, "ok"),
        (1, "timeout"),
        (2, "render_error"),
    ]
    assert results[0].variables == {"customer_name": "Ana", "plan": "Pro"}
    timed_out = results[1]
    assert (timed_out.url, timed_out.duration_ms, timed_out.cached) == (f"{API}/orders", 6000, False)
    assert (timed_out.variables, timed_out.level, timed_out.message()) == (
        {},
        "warning",
        "Dynamic data failed: timeout",
    )
    assert slow.cancelled
    logged = [
        (line["index"], line["outcome"])
        for line in log_events(stream)
        if line["event"] == "tools.dynamic_data"
    ]
    assert sorted(logged) == [(0, "ok"), (1, "timeout"), (2, "render_error")]
    assert "sk-9" not in stream.getvalue()


@pytest.mark.parametrize(
    ("timeout_ms", "ends_s"), [(8_000, BUDGET_S), (2_000, 2.0)], ids=["budget", "request-timeout"]
)
async def test_a_fetch_whose_read_is_held_is_a_timeout_at_the_earlier_limit(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, timeout_ms: int, ends_s: float
) -> None:
    """specs/13 §9 Decision P08-FIX-25: the wait for the reader thread ends at the fetch budget or at the
    request's timeout, whichever comes first, so a turn never waits for a busy reader."""
    api.get("/contacts").respond(200, json=CONTACT)
    entered = read_waits(monkeypatch, va_tools.http)
    searched = thread_recorder(monkeypatch, va_tools.http, "extract_all")
    specs = [(0, _spec(response_data=[{"name": "plan", "data": "$..plan"}]))]
    async with held_reader() as gate, _fetching_all(specs, timeout_ms=timeout_ms) as (clock, running):
        async with asyncio.timeout(5):
            await entered.wait()
        await clock.wait_for_sleep(ends_s)
        await clock.run_for(ends_s)
        results = await running
        still_held = not gate.is_set()

    assert still_held
    assert [(result.outcome, result.variables, result.duration_ms) for result in results] == [
        ("timeout", {}, round(ends_s * 1000))
    ]
    assert searched == []


class _NeverAsked:
    """A port no request may reach."""

    async def execute(self, call: HttpCall) -> HttpOutcome:
        raise AssertionError(call.url)


@pytest.mark.parametrize("outcome", ["paused", "timeout"])
def test_an_unfetched_result_carries_the_redacted_url_and_no_answer(
    outcome: Literal["paused", "timeout"],
) -> None:
    stream = json_log_stream()
    fetcher = DynamicDataFetcher(_NeverAsked(), redis=None, org_id=ORG, clock=RecordingClock())
    spec = _spec(url=f"{API}/orders?token=sk-9&phone={{{{to}}}}", method="POST", timing="every_turn")

    result = fetcher.unfetched(spec, 3, SCOPE, outcome)

    assert result == DynamicDataResult(
        index=3, timing="every_turn", outcome=outcome, url=f"{API}/orders", method="POST"
    )
    assert (result.failed, result.level, result.readable) == (True, "warning", False)
    assert result.event_data() == {
        "url": f"{API}/orders",
        "method": "POST",
        "status_code": None,
        "duration_ms": 0,
        "variables_set": [],
        "cached": False,
        "outcome": outcome,
        "timing": "every_turn",
        "index": 3,
    }
    logged = [line for line in log_events(stream) if line["event"] == "tools.dynamic_data"]
    assert [(line["index"], line["outcome"], line["duration_ms"]) for line in logged] == [(3, outcome, 0)]
    assert "sk-9" not in stream.getvalue()


def test_an_unfetched_result_of_a_url_that_does_not_render_has_no_url() -> None:
    fetcher = DynamicDataFetcher(_NeverAsked(), redis=None, org_id=ORG, clock=RecordingClock())

    result = fetcher.unfetched(_spec(url=f"{API}/orders?x={{{{to"), 1, SCOPE, "paused", duration_ms=7)

    assert (result.url, result.outcome, result.duration_ms, result.timing) == ("", "paused", 7, "pre_call")
    assert result.message() == "Dynamic data paused: endpoint not answering"


def test_the_fetcher_exposes_its_clock() -> None:
    clock = RecordingClock()

    assert DynamicDataFetcher(_NeverAsked(), redis=None, org_id=ORG, clock=clock).clock is clock


async def test_fetch_all_without_specs_touches_nothing() -> None:
    class Unused:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            raise AssertionError(call.url)

    clock = RecordingClock()
    fetcher = DynamicDataFetcher(Unused(), redis=None, org_id=ORG, clock=clock)

    assert await fetcher.fetch_all([], SCOPE, timeout_ms=5_000, budget_s=6.0) == []
    assert clock.sleeps == []
