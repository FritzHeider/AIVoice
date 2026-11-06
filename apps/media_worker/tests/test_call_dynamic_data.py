"""The media worker's per-turn dynamic data source (specs/13 §7 Decision P08-T03b)."""

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
