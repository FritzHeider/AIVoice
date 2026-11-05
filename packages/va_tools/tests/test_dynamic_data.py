"""Dynamic data fetches (specs/13 §7 Decision P08-T03a) over respx and fakeredis."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from clock_helpers import ManualClock, RecordingClock
from fakeredis import FakeAsyncRedis, FakeServer
from redis_ttl import assert_expires_in
from service_helpers import json_log_stream, log_events
from tools_helpers import API, plain_executor

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
from va_tools.http import USER_AGENT, HttpExecutor

ORG = uuid.UUID("0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71")
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


async def _fetch(
    spec: DynamicDataSpec,
    *,
    redis: FakeAsyncRedis | None = None,
    org_id: uuid.UUID = ORG,
    index: int = 0,
    clock: RecordingClock | None = None,
) -> DynamicDataResult:
    async with plain_executor(clock=clock) as (executor, recording):
        fetcher = DynamicDataFetcher(executor, redis=redis, org_id=org_id, clock=recording)
        return await fetcher.fetch(spec, index, SCOPE, timeout_ms=5_000)


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


async def test_a_text_answer_is_ok_and_sets_nothing(api: respx.MockRouter) -> None:
    api.get("/contacts").respond(200, text="not json")

    result = await _fetch(_spec())

    assert (result.outcome, result.variables, result.sections) == ("ok", {}, {})
    assert result.message() == "Dynamic data: 0 variables set"


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
    ],
    ids=["text", "list", "text-status", "bool-status", "no-body", "too-deep"],
)
async def test_an_unreadable_cache_value_is_a_miss(
    api: respx.MockRouter, redis: FakeAsyncRedis, stored: str
) -> None:
    stream = json_log_stream()
    route = api.get("/contacts").respond(200, json=CONTACT)
    key = cache_key(ORG, "GET", f"{API}/contacts?phone=%2B14155550123", None)
    await redis.set(key, stored)

    result = await _fetch(_spec(cache=True), redis=redis)

    assert (result.outcome, result.cached, route.call_count) == ("ok", False, 1)
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


class _Held:
    """A respx side effect that answers once ``gate`` is set and records what arrived."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.gate = asyncio.Event()
        self.arrived = asyncio.Event()
        self.cancelled = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        del request
        self.arrived.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return httpx.Response(200, json=self.body)


@asynccontextmanager
async def _fetching_all(
    specs: list[tuple[int, DynamicDataSpec]],
) -> AsyncIterator[tuple[ManualClock, asyncio.Task[list[DynamicDataResult]]]]:
    """``fetch_all`` of ``specs`` running over the real executor on a manual clock (budget ``BUDGET_S``)."""
    clock = ManualClock()
    async with httpx.AsyncClient() as client:
        executor = HttpExecutor(client, clock=clock, allow_http=False, call_id="call-7")
        fetcher = DynamicDataFetcher(executor, redis=None, org_id=ORG, clock=clock)
        running = asyncio.create_task(fetcher.fetch_all(specs, SCOPE, timeout_ms=5_000, budget_s=BUDGET_S))
        try:
            yield clock, running
        finally:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running


async def test_fetch_all_runs_concurrently_in_one_scope_and_keeps_spec_order(api: respx.MockRouter) -> None:
    contacts = _Held(CONTACT)
    orders = _Held({"last": {"id": "A-7"}})
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
    slow = _Held({"last": {"id": "A-7"}})
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


async def test_fetch_all_without_specs_touches_nothing() -> None:
    class Unused:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            raise AssertionError(call.url)

    clock = RecordingClock()
    fetcher = DynamicDataFetcher(Unused(), redis=None, org_id=ORG, clock=clock)

    assert await fetcher.fetch_all([], SCOPE, timeout_ms=5_000, budget_s=6.0) == []
    assert clock.sleeps == []
