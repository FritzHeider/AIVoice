"""The text-run recorder: log/action mapping, excerpts, header masking, the simulated SMS port, action
consumption (specs/12 §7 Decision P07-T10), and unit-test tool mocks (specs/12 §8 Decision P07-T11a)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from pydantic import JsonValue, ValidationError

from va_engine.models.results import (
    Action,
    EndCallAction,
    HumanTakeover,
    Say,
    SayStream,
    SendDtmfAction,
    SwitchGraph,
    TransferAction,
    Wait,
)
from va_engine.ports import HttpCall, HttpOutcome, SmsRequest
from va_pathways.recording import (
    EXCERPT_LIMIT,
    MockResponse,
    TextRecorder,
    action_item,
    excerpt,
    log_item,
    masked_headers,
    mocked_outcome,
)


def test_log_items_of_every_shown_event() -> None:
    route: dict[str, JsonValue] = {
        "from_node": "greet",
        "to_node": "order",
        "label": "Wants to order",
        "alternatives": [{"path_id": "e_hours", "confidence": 2}],
        "global": False,
        "router_ms": 12,
        "turn_index": 1,
        "tag_color": None,
    }
    assert log_item("call.route", route) == {
        "type": "route",
        "from": "greet",
        "to": "order",
        "label": "Wants to order",
        "alternatives": [{"path_id": "e_hours", "confidence": 2}],
        "global": False,
    }
    extraction: dict[str, JsonValue] = {
        "node_id": "greet",
        "variables": {"first_name": "Mike"},
        "turn_index": 1,
    }
    assert log_item("call.extraction", extraction) == {
        "type": "extraction",
        "node_id": "greet",
        "variables": {"first_name": "Mike"},
    }
    condition: dict[str, JsonValue] = {
        "node_id": "order",
        "score": 4,
        "met": False,
        "message": "Likely 4/10",
        "turn_index": 2,
    }
    assert log_item("call.loop_condition", condition) == {
        "type": "loop_condition",
        "node_id": "order",
        "score": 4,
        "met": False,
    }
    webhook: dict[str, JsonValue] = {
        "node_id": "hook",
        "node_name": "Status",
        "purpose": "webhook",
        "method": "POST",
        "url": "https://api.acme.test/status",
        "outcome": "http_error",
        "status_code": 503,
        "attempts": 2,
        "duration_ms": 40,
        "variables_set": [],
        "route_to": None,
    }
    assert log_item("call.webhook_node", webhook) == {
        "type": "webhook",
        "node_id": "hook",
        "node_name": "Status",
        "method": "POST",
        "url": "https://api.acme.test/status",
        "status": 503,
        "ms": 40,
        "outcome": "http_error",
    }
    sms: dict[str, JsonValue] = {
        "node_id": "sms",
        "source": "sms_node",
        "sent": True,
        "message_id": "sim-1",
        "error": None,
    }
    assert log_item("call.sms_sent", sms) == {"type": "sms", "node_id": "sms", "sent": True}
    assert log_item("call.transfer_started", {}) is None


def test_action_items_of_every_simulated_action() -> None:
    assert action_item(Wait(seconds=1.5)) == {"type": "wait", "seconds": 1.5}
    transfer = TransferAction(mode="cold", target="+14155550100", caller_id_mode="original_caller")
    assert action_item(transfer) == {
        "type": "transfer",
        "mode": "cold",
        "target": "+14155550100",
        "caller_id_mode": "original_caller",
    }
    assert action_item(EndCallAction(reason="loop_protection", ended_by="agent")) == {
        "type": "end_call",
        "reason": "loop_protection",
        "ended_by": "agent",
    }
    assert action_item(HumanTakeover()) == {"type": "human_takeover"}
    assert action_item(SendDtmfAction(digits="12#", duration_ms=250)) == {
        "type": "press_button",
        "digits": "12#",
        "duration_ms": 250,
    }
    assert action_item(SwitchGraph(pathway_id="p2", version="production", node_id=None)) == {
        "type": "switch_pathway",
        "pathway_id": "p2",
        "version": "production",
        "node_id": None,
    }
    assert action_item(Say(text="Hi", mark="t0.1")) is None


def test_excerpts_are_capped_and_json_encoded() -> None:
    assert excerpt(None) is None
    assert excerpt("plain") == "plain"
    assert excerpt({"a": [1, 2]}) == '{"a":[1,2]}'
    exact = "x" * EXCERPT_LIMIT
    assert excerpt(exact) == exact
    cut = excerpt("y" * (EXCERPT_LIMIT + 5))
    assert cut is not None
    assert len(cut) == EXCERPT_LIMIT
    assert cut.endswith("…")


def test_secret_headers_are_masked() -> None:
    headers = {
        "Authorization": "Bearer s3cr3t",
        "X-Api-Key": "k",
        "Cookie": "a=b",
        "Content-Type": "application/json",
        "X-Order": "42",
    }
    assert masked_headers(headers) == {
        "Authorization": "***",
        "X-Api-Key": "***",
        "Cookie": "***",
        "Content-Type": "application/json",
        "X-Order": "42",
    }


class _Http:
    def __init__(self, outcome: HttpOutcome) -> None:
        self.outcome = outcome
        self.calls: list[HttpCall] = []

    async def execute(self, call: HttpCall) -> HttpOutcome:
        self.calls.append(call)
        return self.outcome


async def test_recorder_logs_tool_calls_and_simulates_sms() -> None:
    http = _Http(HttpOutcome(kind="timeout", attempts=2, duration_ms=10_000))
    recorder = TextRecorder(http)
    call = HttpCall(
        method="GET", url="https://user:pw@api.acme.test/x?token=abc", headers={"X-Token": "t"}, body=None
    )
    assert await recorder.execute(call) is http.outcome
    assert http.calls == [call]
    assert recorder.logs == [
        {
            "type": "tool",
            "method": "GET",
            "url": "https://api.acme.test/x",
            "request_headers": {"X-Token": "***"},
            "request_body": None,
            "status": None,
            "response_body": None,
            "ms": 10_000,
            "outcome": "timeout",
        }
    ]
    first = await recorder.send(SmsRequest(to="+14155550123", from_=None, body="One", node_id="a"))
    second = await recorder.send(SmsRequest(to="+14155550123", from_="+14155550100", body="Two", node_id="b"))
    assert (first.sent, first.message_id, second.message_id) == (True, "sim-1", "sim-2")
    assert [(item["node_id"], item["from"], item["message_id"]) for item in recorder.actions] == [
        ("a", None, "sim-1"),
        ("b", "+14155550100", "sim-2"),
    ]
    recorder.emit("call.route", {"from_node": "a", "to_node": "b"})
    recorder.emit("call.unknown", {})
    assert [item["type"] for item in recorder.logs] == ["tool", "route"]


async def _tokens(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def _actions(*actions: Action) -> AsyncGenerator[Action]:
    for action in actions:
        yield action


async def test_consume_collects_speech_actions_and_terminal_state() -> None:
    recorder = TextRecorder(_Http(HttpOutcome(kind="ok", status=200)))
    await recorder.consume(
        _actions(
            Say(text="  Hello there. ", mark="t0.1"),
            SayStream(tokens=_tokens("Great", ", ", "what size?"), mark="t0.2"),
            SayStream(tokens=_tokens(" "), mark="t0.3"),
            Wait(seconds=2),
            EndCallAction(reason="agent_end_call"),
        )
    )
    assert recorder.messages == ["Hello there.", "Great, what size?"]
    assert recorder.actions == [
        {"type": "wait", "seconds": 2},
        {"type": "end_call", "reason": "agent_end_call", "ended_by": "agent"},
    ]
    assert recorder.terminal is True


async def test_mocked_sources_answer_without_the_executor() -> None:
    http = _Http(HttpOutcome(kind="ok", status=200, body={"real": True}))
    recorder = TextRecorder(
        http,
        mocks={
            "hook": MockResponse(status=200, body={"eta": 25, "items": [{"id": 1}, {"id": 2}]}),
            "TL-down": MockResponse(status=503, body="maintenance"),
        },
    )
    hook = HttpCall(
        method="POST",
        url="https://api.acme.test/status",
        headers={"Authorization": "Bearer x"},
        body={"q": 1},
        extract=(("eta", "$.eta"), ("ids", "$.items[*].id"), ("none", "$.missing")),
        source_id="hook",
    )
    outcome = await recorder.execute(hook)
    assert (outcome.kind, outcome.status, outcome.succeeded) == ("ok", 200, True)
    assert dict(outcome.extracted) == {"eta": 25, "ids": [1, 2], "none": None}
    down = await recorder.execute(HttpCall(method="GET", url="https://x.test", source_id="TL-down"))
    assert (down.kind, down.status, down.body, dict(down.extracted)) == ("http_error", 503, "maintenance", {})
    real = HttpCall(method="GET", url="https://x.test/real", source_id="other")
    assert await recorder.execute(real) is http.outcome
    assert await recorder.execute(HttpCall(method="GET", url="https://x.test/anon")) is http.outcome
    assert [call.url for call in http.calls] == ["https://x.test/real", "https://x.test/anon"]
    assert [item.get("mocked") for item in recorder.logs] == [True, True, None, None]
    first = recorder.logs[0]
    assert (first["request_headers"], first["response_body"], first["status"]) == (
        {"Authorization": "***"},
        '{"eta":25,"items":[{"id":1},{"id":2}]}',
        200,
    )


def test_mock_defaults_and_bounds() -> None:
    empty = mocked_outcome(
        HttpCall(method="GET", url="https://x.test", extract=(("a", "$.a"),)), MockResponse()
    )
    assert (empty.kind, empty.status, empty.body, dict(empty.extracted)) == ("ok", 200, None, {})
    assert mocked_outcome(HttpCall(method="GET", url="https://x.test"), MockResponse(status=302)).kind == (
        "http_error"
    )
    for status in (99, 600):
        with pytest.raises(ValidationError):
            MockResponse(status=status)
    with pytest.raises(ValidationError):
        MockResponse.model_validate({"status": 200, "headers": {}})
