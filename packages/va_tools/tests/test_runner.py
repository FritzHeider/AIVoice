"""The tool runner (specs/13 §5 steps 1, 3-8 and 10, Decision P08-T02a) over respx."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from clock_helpers import RecordingClock
from tools_helpers import API, plain_executor

from va_db.models import Tool
from va_domain.calls.tools import InlineTool
from va_engine.templating import TemplateScope
from va_tools.models import ToolSpec, function_def
from va_tools.runner import (
    DATA_LIMIT,
    MAX_ARGUMENT_ERRORS,
    ToolRunner,
    argument_errors,
    failure_detail,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["date", "party_size"],
    "additionalProperties": False,
}
SCOPE = TemplateScope(variables={"city": "Rome"}, builtins={"from": "+14155550100"})


def _spec(**fields: Any) -> ToolSpec:
    values: dict[str, Any] = {
        "tool_id": "TL-0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71",
        "name": "check_availability",
        "description": "Checks availability.",
        "speech": "One sec.",
        "url": f"{API}/availability",
        "method": "POST",
        "body": {"date": "{{input.date}}", "party": "{{input.party_size}}", "city": "{{city}}"},
        "input_schema": SCHEMA,
        "response_map": {"available": "$.available", "times": "$.alternatives[*].time", "nope": "$.nope"},
    }
    return ToolSpec(**{**values, **fields})


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def _run(spec: ToolSpec, arguments: Any, *, clock: RecordingClock | None = None) -> Any:
    async with plain_executor(clock=clock) as (executor, recording):
        return await ToolRunner(executor, clock=recording).run(spec, arguments, SCOPE)


async def test_success_renders_sends_extracts_and_answers(api: respx.MockRouter) -> None:
    body = {"available": False, "alternatives": [{"time": "19:00"}, {"time": "20:30"}]}
    route = api.post("/availability").respond(200, json=body)
    invocation = await _run(_spec(), '{"date": "2026-11-06", "party_size": 4}')

    assert json.loads(route.calls.last.request.content) == {"date": "2026-11-06", "party": 4, "city": "Rome"}
    assert invocation.outcome == "ok"
    assert not invocation.failed
    assert json.loads(invocation.content) == {
        "status": 200,
        "data": body,
        "extracted": {"available": False, "times": ["19:00", "20:30"], "nope": None},
    }
    assert invocation.extracted == {"available": False, "times": ["19:00", "20:30"], "nope": None}
    assert invocation.variables == {"available": False, "times": ["19:00", "20:30"]}
    assert invocation.request == {
        "url": f"{API}/availability",
        "method": "POST",
        "headers": {},
        "body": {"date": "2026-11-06", "party": 4, "city": "Rome"},
    }
    assert (invocation.status_code, invocation.attempts, invocation.response) == (200, 1, body)


async def test_single_match_is_a_value_and_mapping_arguments_work(api: respx.MockRouter) -> None:
    api.post("/availability").respond(200, json={"available": True, "alternatives": [{"time": "18:00"}]})
    invocation = await _run(_spec(), {"date": "2026-11-06", "party_size": 2})
    assert invocation.extracted == {"available": True, "times": "18:00", "nope": None}


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', "3"])
async def test_arguments_that_are_not_an_object(raw: str, api: respx.MockRouter) -> None:
    route = api.post("/availability").respond(200)
    invocation = await _run(_spec(), raw)
    assert not route.called
    assert invocation.outcome == "invalid_arguments"
    assert not invocation.failed
    assert json.loads(invocation.content) == {
        "error": "invalid_arguments",
        "details": [{"path": "", "message": "arguments are not a JSON object"}],
    }
    assert invocation.request is None
    assert invocation.event_data()["request"] is None


async def test_schema_violations_are_sorted_details(api: respx.MockRouter) -> None:
    route = api.post("/availability").respond(200)
    invocation = await _run(_spec(), {"party_size": 50, "tags": ["a", 7], "extra": 1})
    assert not route.called
    content = json.loads(invocation.content)
    assert content["error"] == "invalid_arguments"
    paths = [detail["path"] for detail in content["details"]]
    assert paths == ["", "", "party_size", "tags[1]"]
    messages = {detail["message"] for detail in content["details"]}
    assert "'date' is a required property" in messages
    assert "50 is greater than the maximum of 20" in messages
    assert list(invocation.details) == content["details"]


def test_argument_errors_cap_and_lenient_schemas() -> None:
    many: dict[str, Any] = {"type": "object", "properties": {f"p{i}": {"type": "integer"} for i in range(15)}}
    errors = argument_errors(many, {f"p{i}": "x" for i in range(15)})
    assert len(errors) == MAX_ARGUMENT_ERRORS
    assert argument_errors({"type": "nonsense-type"}, {"a": 1}) == []
    assert argument_errors({"$ref": "https://schemas.test/x.json"}, {"a": 1}) == []
    nested: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}},
    }
    assert argument_errors(nested, {"a": {"b": 1}}) == [
        {"path": "a.b", "message": "1 is not of type 'string'"}
    ]


async def test_blank_arguments_are_an_empty_object(api: respx.MockRouter) -> None:
    api.get("/ping").respond(200, text="pong")
    spec = _spec(url=f"{API}/ping", method="GET", body=None, input_schema={"type": "object"}, response_map={})
    invocation = await _run(spec, "  ")
    assert json.loads(invocation.content) == {"status": 200, "data": "pong", "extracted": {}}


async def test_large_data_is_cut_to_an_excerpt_and_event_excerpt_is_short(api: respx.MockRouter) -> None:
    big = {"rows": ["x" * 100 for _ in range(100)]}
    api.post("/availability").respond(200, json=big)
    invocation = await _run(_spec(), {"date": "d", "party_size": 1})
    data = json.loads(invocation.content)["data"]
    assert isinstance(data, str)
    assert len(data) == DATA_LIMIT
    assert data.endswith("…")
    excerpt = invocation.event_data()["response_excerpt"]
    assert isinstance(excerpt, str)
    assert len(excerpt) == 2_000
    assert invocation.response == big


async def test_secret_headers_are_sent_but_never_shown(api: respx.MockRouter) -> None:
    secret = f"Bearer {secrets.token_hex(16)}"
    route = api.post("/availability").respond(200, json={})
    spec = _spec(
        headers={"X-Org": "acme"},
        secret_headers={"Authorization": secret},
        url=f"{API}/availability?key={{{{input.date}}}}",
    )
    invocation = await _run(spec, {"date": "d1", "party_size": 1})
    assert route.calls.last.request.headers["authorization"] == secret
    assert invocation.request is not None
    assert invocation.request["headers"] == {"X-Org": "acme", "Authorization": "••••"}
    event = invocation.event_data()
    assert secret not in json.dumps(event)
    assert secret not in repr(spec)
    assert event["request"] == {
        "url": f"{API}/availability",
        "method": "POST",
        "headers": {"X-Org": "acme", "Authorization": "••••"},
        "body": {"date": "d1", "party": 1, "city": "Rome"},
    }
    assert invocation.request["url"] == f"{API}/availability?key=d1"


async def test_retry_after_5xx_counts_attempts(api: respx.MockRouter) -> None:
    api.post("/availability").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"available": 1})]
    )
    invocation = await _run(_spec(retries=1), {"date": "d", "party_size": 1})
    assert (invocation.outcome, invocation.attempts, invocation.status_code) == ("ok", 2, 200)


async def test_timeout_after_retries_is_a_failure(api: respx.MockRouter) -> None:
    api.post("/availability").mock(side_effect=httpx.ReadTimeout("slow"))
    clock = RecordingClock()
    invocation = await _run(_spec(retries=1), {"date": "d", "party_size": 1}, clock=clock)
    assert json.loads(invocation.content) == {"status": 0, "error": "timeout"}
    assert invocation.failed
    assert invocation.attempts == 2
    assert invocation.variables == {}
    assert failure_detail(invocation) == "check_availability: timeout"
    assert invocation.event_data()["outcome"] == "timeout"


async def test_http_error_reports_the_status_without_data(api: respx.MockRouter) -> None:
    api.post("/availability").respond(404, json={"available": True})
    invocation = await _run(_spec(), {"date": "d", "party_size": 1})
    assert json.loads(invocation.content) == {"status": 404, "error": "http_error"}
    assert invocation.failed
    assert invocation.variables == {}
    assert invocation.extracted["available"] is True
    assert failure_detail(invocation) == "check_availability: http_error (404)"


async def test_blocked_scheme_sends_nothing() -> None:
    invocation = await _run(_spec(url="ftp://files.test/x"), {"date": "d", "party_size": 1})
    assert json.loads(invocation.content) == {"status": 0, "error": "blocked"}
    assert invocation.failed
    assert invocation.attempts == 0


async def test_render_error_is_reported_and_sends_nothing(api: respx.MockRouter) -> None:
    route = api.post("/availability").respond(200)
    invocation = await _run(_spec(body={"a": "{{"}), {"date": "d", "party_size": 1})
    assert not route.called
    assert invocation.outcome == "render_error"
    assert invocation.failed
    assert json.loads(invocation.content) == {"status": 0, "error": "render_error"}
    assert invocation.request is None


async def test_large_request_body_is_capped_in_the_view(api: respx.MockRouter) -> None:
    api.post("/availability").respond(200, json={})
    spec = _spec(body={"blob": "{{input.date}}"})
    invocation = await _run(spec, {"date": "y" * 5000, "party_size": 1})
    assert invocation.request is not None
    shown = invocation.request["body"]
    assert isinstance(shown, str)
    assert len(shown) == DATA_LIMIT


def test_spec_from_row_and_inline_and_function_def() -> None:
    tool_id = uuid.uuid4()
    row = SimpleNamespace(
        id=tool_id,
        name="lookup",
        description="Looks up.",
        speech=None,
        url="https://a.test/x",
        method="GET",
        headers={"Accept": "application/json"},
        query={"page": "1"},
        body=None,
        input_schema={"type": "object", "properties": {}},
        response_map={"v": "$.v"},
        timeout_ms=3000,
        retries=2,
    )
    spec = ToolSpec.from_row(cast("Tool", row), {"Authorization": "x"})
    assert spec.tool_id == f"TL-{tool_id}"
    assert not spec.inline
    assert spec.source_id == spec.tool_id
    assert spec.masked_names() == frozenset({"authorization"})
    assert (spec.query, spec.response_map, spec.timeout_ms, spec.retries) == (
        {"page": "1"},
        {"v": "$.v"},
        3000,
        2,
    )

    inline = ToolSpec.from_inline(
        InlineTool(name="hook", description="Hooks.", url="https://a.test/h", headers={"X-A": "1"})
    )
    assert inline.inline
    assert inline.source_id == "hook"
    assert inline.masked_names() == frozenset({"x-a"})
    definition = function_def(inline)
    assert definition.wire() == {
        "type": "function",
        "function": {
            "name": "hook",
            "description": "Hooks.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
