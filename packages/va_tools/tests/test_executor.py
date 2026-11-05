"""The in-call tool executor (specs/13 §5, Decision P08-T02b1) over respx."""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import respx
from service_helpers import json_log_stream, log_events
from tools_helpers import API, plain_executor

from va_engine.templating import TemplateScope
from va_llm.models import ToolCall
from va_tools.executor import CallToolExecutor, SystemOutcome, merged_tools
from va_tools.models import ToolSpec
from va_tools.runner import ToolRunner

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order": {"type": "string"}},
    "required": ["order"],
}
SCOPE = TemplateScope(variables={"city": "Rome"})


def _spec(name: str = "lookup_order", *, tool_id: str | None = "TL-1", **fields: Any) -> ToolSpec:
    values: dict[str, Any] = {
        "tool_id": tool_id,
        "name": name,
        "description": f"Runs {name}.",
        "speech": None,
        "url": f"{API}/{name}",
        "method": "POST",
        "body": {"order": "{{input.order}}", "city": "{{city}}"},
        "input_schema": SCHEMA,
        "response_map": {"status": "$.status", "eta": "$.eta"},
    }
    return ToolSpec(**{**values, **fields})


@dataclass(slots=True)
class _Events:
    tools: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def tool(self, data: dict[str, Any]) -> None:
        self.tools.append(data)

    def tool_failed(self, detail: str) -> None:
        self.failures.append(detail)

    def sms_sent(self, data: dict[str, Any]) -> None:
        self.tools.append({"sms_sent": data})


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def _execute(specs: list[ToolSpec], name: str, arguments: str, events: _Events | None = None) -> Any:
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor(specs, runner=ToolRunner(http, clock=clock), events=events)
        return await executor.execute(ToolCall(id="c1", name=name, arguments_json=arguments), SCOPE)


async def test_a_successful_run_answers_reports_and_extracts(api: respx.MockRouter) -> None:
    route = api.post("/lookup_order").respond(200, json={"status": "shipped", "eta": None})
    events = _Events()
    answer = await _execute([_spec()], "lookup_order", '{"order": "A7"}', events)

    assert json.loads(route.calls.last.request.content) == {"order": "A7", "city": "Rome"}
    assert json.loads(answer.content) == {
        "status": 200,
        "data": {"status": "shipped", "eta": None},
        "extracted": {"status": "shipped", "eta": None},
    }
    assert dict(answer.variables) == {"status": "shipped"}
    [data] = events.tools
    assert (data["tool_name"], data["tool_id"], data["outcome"], data["status_code"]) == (
        "lookup_order",
        "TL-1",
        "ok",
        200,
    )
    assert events.failures == []


async def test_a_failing_run_reports_tool_failed(api: respx.MockRouter) -> None:
    api.post("/lookup_order").respond(500, json={"status": "down"})
    events = _Events()
    answer = await _execute([_spec()], "lookup_order", '{"order": "A7"}', events)
    assert json.loads(answer.content) == {"status": 500, "error": "http_error"}
    assert dict(answer.variables) == {}
    assert [d["outcome"] for d in events.tools] == ["http_error"]
    assert events.failures == ["lookup_order: http_error (500)"]


async def test_invalid_arguments_report_the_run_but_no_failure(api: respx.MockRouter) -> None:
    route = api.post("/lookup_order").respond(200)
    events = _Events()
    answer = await _execute([_spec()], "lookup_order", '{"nope": 1}', events)
    assert json.loads(answer.content)["error"] == "invalid_arguments"
    assert not route.called
    assert [d["outcome"] for d in events.tools] == ["invalid_arguments"]
    assert events.failures == []


async def test_inline_header_values_are_masked_in_the_event(api: respx.MockRouter) -> None:
    route = api.post("/lookup_order").respond(200, json={})
    events = _Events()
    inline = _spec(tool_id=None, headers={"X-Token": "inline-value"})
    await _execute([inline], "lookup_order", '{"order": "A7"}', events)
    assert route.calls.last.request.headers["X-Token"] == "inline-value"
    assert events.tools[0]["request"]["headers"]["X-Token"] == "••••"
    assert "inline-value" not in json.dumps(events.tools)


async def test_runs_without_an_event_sink_and_logs_no_arguments(api: respx.MockRouter) -> None:
    api.post("/lookup_order").respond(200, json={"status": "ok"})
    stream = json_log_stream()
    answer = await _execute([_spec()], "lookup_order", '{"order": "SECRET-ORDER"}')
    assert json.loads(answer.content)["status"] == 200
    [ran] = [e for e in log_events(stream) if e["event"] == "tools.call_ran"]
    assert (ran["tool"], ran["outcome"], ran["status"], ran["attempts"]) == ("lookup_order", "ok", 200, 1)
    assert "SECRET-ORDER" not in json.dumps(log_events(stream))


async def test_definitions_speech_and_handles() -> None:
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor(
            [_spec("a"), _spec("b", speech="Checking {{input.order}}."), _spec("c", speech="Other.")],
            runner=ToolRunner(http, clock=clock),
        )
    assert [d.name for d in executor.definitions()] == ["a", "b", "c"]
    assert executor.definitions()[0].parameters == SCHEMA
    assert executor.tool_speech == "Checking {{input.order}}."
    assert (executor.speech("a"), executor.speech("b"), executor.speech("zzz")) == (
        None,
        "Checking {{input.order}}.",
        None,
    )
    assert (executor.handles("c"), executor.handles("end_call")) == (True, False)


async def test_blank_speech_counts_as_none() -> None:
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([_spec(speech="   ")], runner=ToolRunner(http, clock=clock))
    assert (executor.tool_speech, executor.speech("lookup_order")) == (None, None)


def test_inline_tools_replace_stored_tools_of_the_same_name() -> None:
    stored = [_spec("a", tool_id="TL-a"), _spec("b", tool_id="TL-b"), _spec("c", tool_id="TL-c")]
    inline = [_spec("b", tool_id=None, url="https://inline.acme.test/b"), _spec("d", tool_id=None)]
    merged = merged_tools(stored, inline)
    assert [(s.name, s.tool_id) for s in merged] == [("a", "TL-a"), ("c", "TL-c"), ("b", None), ("d", None)]
    assert merged[2].url == "https://inline.acme.test/b"


def test_reserved_names_and_duplicates_are_dropped() -> None:
    stream = json_log_stream()
    stored = [_spec("end_call", tool_id="TL-x"), _spec("a", tool_id="TL-a"), _spec("a", tool_id="TL-a2")]
    merged = merged_tools(stored, [_spec("transfer_call", tool_id=None)])
    assert [(s.name, s.tool_id) for s in merged] == [("a", "TL-a")]
    skipped = [(e["tool"], e["reason"]) for e in log_events(stream) if e["event"] == "tools.tool_skipped"]
    assert skipped == [("end_call", "reserved"), ("a", "duplicate"), ("transfer_call", "reserved")]


@dataclass(slots=True)
class _System:
    name: str
    outcome: SystemOutcome
    calls: list[ToolCall] = field(default_factory=list)

    async def run(self, call: ToolCall) -> SystemOutcome:
        self.calls.append(call)
        return self.outcome


async def test_system_tools_are_handled_but_not_defined_and_have_no_speech() -> None:
    sms = _System("send_sms", SystemOutcome('{"status":"sent"}'))
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([_spec()], runner=ToolRunner(http, clock=clock), system=[sms])
    assert [tool.name for tool in executor.definitions()] == ["lookup_order"]
    assert executor.handles("send_sms")
    assert not executor.handles("schedule_callback")
    assert executor.speech("send_sms") is None


async def test_a_system_tool_answers_without_a_tool_event() -> None:
    stream = json_log_stream()
    sms = _System("send_sms", SystemOutcome('{"status":"sent","segments":1}'))
    events = _Events()
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([], runner=ToolRunner(http, clock=clock), events=events, system=[sms])
        call = ToolCall(id="c9", name="send_sms", arguments_json='{"message": "secret text"}')
        answer = await executor.execute(call, SCOPE)
    assert answer.content == '{"status":"sent","segments":1}'
    assert dict(answer.variables) == {}
    assert sms.calls == [call]
    assert (events.tools, events.failures) == ([], [])
    [ran] = [e for e in log_events(stream) if e["event"] == "tools.system_tool_ran"]
    assert (ran["tool"], ran["failure"]) == ("send_sms", None)
    assert "secret text" not in stream.getvalue()


async def test_a_failed_system_tool_reports_tool_failed() -> None:
    callback = _System("schedule_callback", SystemOutcome('{"error":"callback_failed"}', "workflow_start"))
    events = _Events()
    async with plain_executor() as (http, clock):
        runner = ToolRunner(http, clock=clock)
        executor = CallToolExecutor([], runner=runner, events=events, system=[callback])
        answer = await executor.execute(
            ToolCall(id="c1", name="schedule_callback", arguments_json='{"datetime_iso": "x"}'), SCOPE
        )
    assert answer.content == '{"error":"callback_failed"}'
    assert events.failures == ["schedule_callback: workflow_start"]
    assert events.tools == []


@dataclass(slots=True)
class _GatedSystem:
    """A system tool whose run waits for ``gate`` (``outcome`` ``None`` = never finishes; an exception is
    raised)."""

    name: str
    outcome: SystemOutcome | Exception | None
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: list[str] = field(default_factory=list)
    cancelled: bool = False

    async def run(self, call: ToolCall) -> SystemOutcome:
        self.started.set()
        try:
            await self.gate.wait()
            if self.outcome is None:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished.append(call.id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        assert self.outcome is not None
        return self.outcome


async def _cancel_mid_run(tool: _GatedSystem, events: _Events) -> tuple[CallToolExecutor, asyncio.Task[Any]]:
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([], runner=ToolRunner(http, clock=clock), events=events, system=[tool])
    waiting = asyncio.create_task(
        executor.execute(ToolCall(id="c1", name=tool.name, arguments_json="{}"), SCOPE)
    )
    await tool.started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    return executor, waiting


async def test_a_cancelled_wait_lets_the_system_tool_finish_and_report() -> None:
    tool = _GatedSystem("send_sms", SystemOutcome('{"error":"sms_failed"}', "telnyx_error"))
    events = _Events()
    executor, _ = await _cancel_mid_run(tool, events)
    assert not tool.cancelled
    assert events.failures == []
    tool.gate.set()
    await executor.aclose()
    assert tool.finished == ["c1"]
    assert not tool.cancelled
    assert events.failures == ["send_sms: telnyx_error"]


async def test_aclose_cancels_a_run_that_outlives_the_drain() -> None:
    stream = json_log_stream()
    tool = _GatedSystem("schedule_callback", None)
    tool.gate.set()
    executor, _ = await _cancel_mid_run(tool, _Events())
    await executor.aclose(timeout_s=0.01)
    assert tool.cancelled
    [abandoned] = [e for e in log_events(stream) if e["event"] == "tools.system_tools_abandoned"]
    assert abandoned["count"] == 1
    await executor.aclose()


async def test_aclose_without_runs_returns_at_once() -> None:
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([], runner=ToolRunner(http, clock=clock))
    await executor.aclose(timeout_s=0)


async def test_a_crash_nobody_awaits_is_reported_and_retrieved() -> None:
    stream = json_log_stream()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        tool = _GatedSystem("send_sms", RuntimeError("boom"))
        events = _Events()
        executor, _ = await _cancel_mid_run(tool, events)
        tool.gate.set()
        await executor.aclose()
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert unhandled == []
    assert events.failures == ["send_sms: crashed"]
    [crashed] = [e for e in log_events(stream) if e["event"] == "tools.system_tool_crashed"]
    assert crashed["error_type"] == "RuntimeError"


async def test_an_awaited_crash_propagates_to_the_caller() -> None:
    tool = _GatedSystem("send_sms", RuntimeError("boom"))
    tool.gate.set()
    events = _Events()
    async with plain_executor() as (http, clock):
        executor = CallToolExecutor([], runner=ToolRunner(http, clock=clock), events=events, system=[tool])
        with pytest.raises(RuntimeError):
            await executor.execute(ToolCall(id="c1", name="send_sms", arguments_json="{}"), SCOPE)
    assert events.failures == ["send_sms: crashed"]
