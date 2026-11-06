"""The media worker's per-turn dynamic data source (specs/13 §7 Decisions P08-T03b and P08-FIX-23)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from media_helpers import call_context
from pydantic import JsonValue
from recorder_helpers import recorder

from va_common.time import SystemClock
from va_engine.templating import TemplateScope
from va_engine.turn_data import commit_refresh
from va_events import EventType
from va_media.call_dynamic_data import TurnDataSource
from va_tools.dynamic_data import cache_key
from va_tools.turn_data import TURN_MAX_ANSWER_BYTES

API = "https://crm.acme.test"
SCOPE = TemplateScope(variables={}, request_data={}, builtins={"to": "+14155550100"})
PLAN: dict[str, Any] = {
    "url": API + "/plan?phone={{to}}",
    "timing": "every_turn",
    "cache": True,
    "response_data": [{"name": "plan", "data": "$.plan", "context": "Their plan is {{plan}}."}],
}
CONTACT: dict[str, Any] = {
    "url": API + "/contact",
    "response_data": [{"name": "name", "data": "$.name", "context": "The customer is {{name}}."}],
}


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def source() -> AsyncIterator[TurnDataSource]:
    redis = FakeAsyncRedis()
    async with httpx.AsyncClient() as http:
        yield TurnDataSource(tools_http=http, allow_http=False, redis=redis, clock=SystemClock())
    await redis.aclose()


def test_a_call_without_every_turn_entries_has_no_turn_data(source: TurnDataSource) -> None:
    assert source.for_call(call_context(uuid.uuid4()), None) is None
    assert source.for_call(call_context(uuid.uuid4(), dynamic_data=[CONTACT]), None) is None


async def test_the_turn_data_starts_from_the_context_and_reports_through_the_recorder(
    source: TurnDataSource, api: respx.MockRouter
) -> None:
    route = api.get("/plan").mock(
        side_effect=[httpx.Response(200, json={"plan": "platinum"}), httpx.Response(503, json={})]
    )
    context = call_context(uuid.uuid4(), dynamic_data=[CONTACT, PLAN]).model_copy(
        update={"context_sections": ["The customer is Ana."], "turn_sections": {1: ["Their plan is gold."]}}
    )
    rec, _turns, events = recorder(context)
    data = source.for_call(context, rec)
    assert data is not None
    assert data.sections() == ("Their plan is gold.",)

    variables: dict[str, JsonValue] = {}
    pending = await data.refresh(SCOPE)
    await rec.drain()
    assert events.events == []  # nothing is published before the turn commits the refresh
    first = commit_refresh(variables, pending)
    cached = commit_refresh(variables, await data.refresh(SCOPE))
    await source.redis.flushall()  # type: ignore[union-attr]
    failed = commit_refresh(variables, await data.refresh(SCOPE))
    await rec.drain()

    assert (first, cached, failed) == (True, False, False)
    assert variables == {"plan": "platinum"}
    assert data.sections() == ("Their plan is platinum.",)
    assert route.call_count == 2
    request = route.calls[0].request
    assert str(request.url) == f"{API}/plan?phone=%2B14155550100"
    assert request.headers["x-va-call-id"] == str(context.call_id)
    assert "x-va-signature" not in request.headers
    assert [(event.type, event.level, event.message) for event in events.events] == [
        (EventType.CALL_DYNAMIC_DATA, "info", "Dynamic data: 1 variable set"),
        (EventType.CALL_DYNAMIC_DATA, "info", "Dynamic data: 1 variable set (cached)"),
        (EventType.CALL_DYNAMIC_DATA, "warning", "Dynamic data failed: http_error (503)"),
    ]
    assert [
        (event.data["index"], event.data["timing"], event.data["url"], event.data["variables_set"])
        for event in events.events
    ] == [
        (1, "every_turn", f"{API}/plan", ["plan"]),
        (1, "every_turn", f"{API}/plan", ["plan"]),
        (1, "every_turn", f"{API}/plan", []),
    ]
    assert {event.call_id for event in events.events} == {context.call_id}


def _gold_call() -> Any:
    """A call whose plan entry (index 0) has the gold line of the pre-call fetch."""
    return call_context(uuid.uuid4(), dynamic_data=[PLAN]).model_copy(
        update={"turn_sections": {0: ["Their plan is gold."]}}
    )


def _padded_plan(plan: str, size: int) -> bytes:
    """A JSON answer of exactly ``size`` bytes that holds ``plan``."""
    head = b'{"plan":"' + plan.encode() + b'","padding":"'
    return head + b"x" * (size - len(head) - 2) + b'"}'


async def test_a_per_turn_answer_above_64_kib_is_cut_and_keeps_the_earlier_lines(
    source: TurnDataSource, api: respx.MockRouter
) -> None:
    """Per-turn answers are parsed on the loop that paces the audio: one byte over the limit is not read."""
    json_type = {"content-type": "application/json"}
    route = api.get("/plan").mock(
        side_effect=[
            httpx.Response(
                200, content=_padded_plan("platinum", TURN_MAX_ANSWER_BYTES + 1), headers=json_type
            ),
            httpx.Response(200, content=_padded_plan("silver", 70_000), headers=json_type),
            httpx.Response(200, content=_padded_plan("diamond", TURN_MAX_ANSWER_BYTES), headers=json_type),
        ]
    )
    context = _gold_call()
    rec, _turns, events = recorder(context)
    data = source.for_call(context, rec)
    assert data is not None
    assert source.redis is not None

    variables: dict[str, JsonValue] = {}
    one_byte_over = commit_refresh(variables, await data.refresh(SCOPE))
    much_larger = commit_refresh(variables, await data.refresh(SCOPE))
    after_the_cut_answers = (dict(variables), data.sections(), await source.redis.keys("dyn:*"))
    at_the_limit = commit_refresh(variables, await data.refresh(SCOPE))
    await rec.drain()

    assert (one_byte_over, much_larger, at_the_limit) == (False, False, True)
    assert after_the_cut_answers == ({}, ("Their plan is gold.",), [])
    assert variables == {"plan": "diamond"}
    assert data.sections() == ("Their plan is diamond.",)
    assert route.call_count == 3
    assert len(await source.redis.keys("dyn:*")) == 1
    assert [(event.level, event.message, event.data["variables_set"]) for event in events.events] == [
        ("info", "Dynamic data: 0 variables set", []),
        ("info", "Dynamic data: 0 variables set", []),
        ("info", "Dynamic data: 1 variable set", ["plan"]),
    ]


async def test_a_stored_answer_above_64_kib_is_a_miss_for_a_per_turn_fetch(
    source: TurnDataSource, api: respx.MockRouter
) -> None:
    """The worker's pre-call fetch may have stored up to 262 144 bytes under the same key."""
    route = api.get("/plan").respond(200, json={"plan": "platinum"})
    context = _gold_call()
    data = source.for_call(context, None)
    assert data is not None
    assert source.redis is not None
    key = cache_key(context.org_id, "GET", f"{API}/plan?phone=%2B14155550100", None)
    await source.redis.set(key, b'{"status":200,"body":' + _padded_plan("stale", 70_000) + b"}")

    variables: dict[str, JsonValue] = {}
    commit_refresh(variables, await data.refresh(SCOPE))

    assert variables == {"plan": "platinum"}
    assert route.call_count == 1


async def test_an_endpoint_that_refuses_twice_is_paused_and_reported(
    source: TurnDataSource, api: respx.MockRouter
) -> None:
    route = api.get("/plan").mock(side_effect=httpx.ConnectError("refused"))
    context = _gold_call()
    rec, _turns, events = recorder(context)
    data = source.for_call(context, rec)
    assert data is not None

    variables: dict[str, JsonValue] = {}
    changed = [commit_refresh(variables, await data.refresh(SCOPE)) for _ in range(3)]
    await rec.drain()

    assert changed == [False, False, False]
    assert (variables, data.sections()) == ({}, ("Their plan is gold.",))
    assert route.call_count == 2
    assert [(event.type, event.level, event.message) for event in events.events] == [
        (EventType.CALL_DYNAMIC_DATA, "warning", "Dynamic data failed: connection"),
        (EventType.CALL_DYNAMIC_DATA, "warning", "Dynamic data failed: connection"),
        (EventType.CALL_DYNAMIC_DATA, "warning", "Dynamic data paused: endpoint not answering"),
    ]
    assert events.events[2].data == {
        "url": f"{API}/plan",
        "method": "GET",
        "status_code": None,
        "duration_ms": 0,
        "variables_set": [],
        "cached": False,
        "outcome": "paused",
        "timing": "every_turn",
        "index": 0,
    }
