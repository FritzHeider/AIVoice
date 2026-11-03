"""Scheduling node handler (specs/11 §4 "Scheduling" row, §3.2.7, Decision P07-T07)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from graph_helpers import node
from handler_helpers import FakeHttp, RecordingSink, handler_ctx, ok, spoken
from jsonschema import Draft202012Validator

from va_engine.handlers import (
    AVAILABLE_SLOTS,
    SCHEDULING_FUNCTIONS,
    SchedulingHandler,
    TurnContext,
    WebhookReport,
    book_slot,
    list_slots,
)
from va_engine.models import Say
from va_engine.ports import HttpOutcome

HANDLER = SchedulingHandler()
NOW: Final = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
NY_SLOT: Final = {"start": "2026-11-04T10:00:00-05:00", "end": "2026-11-04T10:30:00-05:00"}


def scheduling(*, extra: dict[str, Any] | None = None, **config: Any) -> dict[str, Any]:
    body = {
        "availabilityUrl": "https://cal.acme.test/slots?date={{requested_date}}",
        "bookingUrl": "https://cal.acme.test/book",
        "headers": {"X-Key": "{{request_data.account}}"},
        "timezone": "America/New_York",
        "onFailureNodeId": "after",
        **config,
    }
    return node("sched", node_type="Scheduling", scheduling=body, prompt="Offer the slots.", **(extra or {}))


def ctx_with(http: FakeHttp, **kwargs: Any) -> TurnContext:
    builtins = {"user_number": "+14155550123", "call_id": "call-1", "from": "+14155550100"}
    return handler_ctx(
        scheduling(**kwargs.pop("config", {})), http=http, now=NOW, builtins=builtins, **kwargs
    )


async def test_availability_is_fetched_filtered_sorted_and_stored() -> None:
    body = {
        "slots": [
            {"start": "2026-11-05T09:00:00", "end": "2026-11-05T09:30:00"},
            NY_SLOT,
            {"start": "2026-11-03T09:00:00Z", "end": "2026-11-03T09:30:00Z"},
            {"start": "2026-12-30T09:00:00Z", "end": "2026-12-30T09:30:00Z"},
            {"start": "2026-11-06T14:00:00Z"},
            {"start": "2026-11-06T16:00:00Z", "end": "2026-11-06T15:00:00Z"},
            {"start": "not a date"},
            {"end": "2026-11-06T16:00:00Z"},
            "junk",
        ]
    }
    http = FakeHttp([ok(body)])
    ctx = ctx_with(http, variables={"requested_date": "2026-11-04"})

    result = await HANDLER.on_enter(ctx)

    call = http.calls[0]
    assert (call.method, call.url, call.timeout_ms, call.retries) == (
        "GET",
        "https://cal.acme.test/slots?date=2026-11-04",
        10_000,
        1,
    )
    assert call.headers == {"X-Key": "A-7"}
    assert ctx.state.variables[AVAILABLE_SLOTS] == [
        NY_SLOT,
        {"start": "2026-11-05T09:00:00-05:00", "end": "2026-11-05T09:30:00-05:00"},
        {"start": "2026-11-06T09:00:00-05:00", "end": "2026-11-06T09:30:00-05:00"},
        {"start": "2026-11-06T11:00:00-05:00", "end": "2026-11-06T11:30:00-05:00"},
    ]
    assert result.route_to is None
    assert result.problem is None
    report = result.reports[0]
    assert isinstance(report, WebhookReport)
    assert report.event_data()["purpose"] == "availability"
    assert report.event_data()["url"] == "https://cal.acme.test/slots"
    assert report.variables_set == (AVAILABLE_SLOTS,)


async def test_slots_are_capped_at_twenty() -> None:
    slots = [{"start": f"2026-11-04T{hour:02d}:00:00Z"} for hour in range(24)]
    ctx = ctx_with(FakeHttp([ok({"slots": slots})]))
    await HANDLER.on_enter(ctx)
    stored = ctx.state.variables[AVAILABLE_SLOTS]
    assert isinstance(stored, list)
    assert len(stored) == 20


async def test_unknown_timezone_falls_back_to_the_call_timezone() -> None:
    http = FakeHttp([ok({"slots": [{"start": "2026-11-04T10:00:00"}]})])
    ctx = handler_ctx(
        scheduling(timezone="{{tz}}"), http=http, now=NOW, timezone="Europe/Paris", variables={"tz": ""}
    )
    await HANDLER.on_enter(ctx)
    assert ctx.state.variables[AVAILABLE_SLOTS] == [
        {"start": "2026-11-04T10:00:00+01:00", "end": "2026-11-04T10:30:00+01:00"}
    ]


async def test_speech_is_queued_before_the_availability_request() -> None:
    order: list[str] = []
    sink = RecordingSink(order=order)
    http = FakeHttp([ok({"slots": [NY_SLOT]})], order=order)
    ctx = ctx_with(http, sink=sink, config={"speech": "Let me check."})

    await HANDLER.on_enter(ctx)

    assert order == ["sink:Say", "http:GET"]
    assert sink.actions == [Say(text="Let me check.", mark="t3.1", interruptible=None)]


@pytest.mark.parametrize(
    "outcome",
    [
        ok(status=500),
        HttpOutcome(kind="timeout", attempts=2),
        ok({"slots": "nope"}),
        ok(["not", "an", "object"]),
        ok({"slots": []}),
        ok({"slots": [{"start": "2025-01-01T00:00:00Z"}]}),
    ],
)
async def test_failures_route_to_the_failure_node(outcome: HttpOutcome) -> None:
    ctx = ctx_with(FakeHttp([outcome]), variables={AVAILABLE_SLOTS: [NY_SLOT]})

    result = await HANDLER.on_enter(ctx)

    assert result.route_to == "after"
    assert result.problem is None
    assert ctx.state.variables[AVAILABLE_SLOTS] == []
    report = result.reports[0]
    assert isinstance(report, WebhookReport)
    assert report.route_to == "after"


@pytest.mark.parametrize("target", [None, "ghost"])
async def test_failure_without_a_usable_failure_node_is_a_problem(target: str | None) -> None:
    ctx = ctx_with(FakeHttp([ok(status=404)]), config={"onFailureNodeId": target})
    result = await HANDLER.on_enter(ctx)
    assert result.route_to is None
    assert result.problem == "scheduling_unavailable"


async def test_missing_http_port_routes_to_the_failure_node() -> None:
    ctx = handler_ctx(scheduling(), now=NOW)
    result = await HANDLER.on_enter(ctx)
    assert result.route_to == "after"
    assert result.reports == ()


async def test_absent_config_has_a_blank_url() -> None:
    http = FakeHttp()
    ctx = handler_ctx(node("sched", node_type="Scheduling"), http=http, now=NOW)
    result = await HANDLER.on_enter(ctx)
    assert http.calls == []
    assert result.problem == "scheduling_unavailable"


async def test_now_defaults_to_the_millisecond_clock() -> None:
    http = FakeHttp([ok({"slots": [{"start": "1970-01-01T00:00:05Z"}]})])
    ctx = handler_ctx(scheduling(timezone=None), http=http)
    await HANDLER.on_enter(ctx)
    assert ctx.state.variables[AVAILABLE_SLOTS] == [
        {"start": "1970-01-01T00:00:05+00:00", "end": "1970-01-01T00:30:05+00:00"}
    ]


async def test_list_slots_returns_the_stored_slots() -> None:
    ctx = ctx_with(FakeHttp(), variables={AVAILABLE_SLOTS: [NY_SLOT, "junk"]})
    assert list_slots(ctx) == {"slots": [NY_SLOT]}
    empty = ctx_with(FakeHttp(), variables={AVAILABLE_SLOTS: "nope"})
    assert list_slots(empty) == {"slots": []}


async def test_book_slot_posts_the_booking_and_sets_variables() -> None:
    answer = {"booking_id": 981, "start": "2026-11-04T15:00:00Z", "end": "2026-11-04T15:30:00Z"}
    http = FakeHttp([ok(answer)])
    variables = {
        AVAILABLE_SLOTS: [NY_SLOT],
        "first_name": "Ada",
        "last_name": " Lovelace ",
        "email": "ada@acme.test",
    }
    ctx = ctx_with(http, variables=variables)

    outcome = await book_slot(ctx, "2026-11-04T15:00:00+00:00")

    call = http.calls[0]
    assert (call.method, call.url, call.retries) == ("POST", "https://cal.acme.test/book", 0)
    assert call.body == {
        "start": "2026-11-04T10:00:00-05:00",
        "end": "2026-11-04T10:30:00-05:00",
        "name": "Ada Lovelace",
        "phone": "+14155550123",
        "email": "ada@acme.test",
        "notes": None,
        "call_id": "call-1",
    }
    booking = {
        "booking_id": "981",
        "booking_start": "2026-11-04T10:00:00-05:00",
        "booking_end": "2026-11-04T10:30:00-05:00",
    }
    assert outcome.ok is True
    assert outcome.tool_message == {"status": "booked", **booking}
    assert {key: ctx.state.variables[key] for key in booking} == booking
    assert outcome.route_to is None
    assert outcome.report is not None
    assert outcome.report.event_data()["purpose"] == "booking"
    assert outcome.report.variables_set == ("booking_id", "booking_start", "booking_end")


async def test_book_slot_uses_the_slot_times_when_the_answer_omits_them() -> None:
    http = FakeHttp([ok({"booking_id": "B-1"})])
    ctx = ctx_with(http, variables={AVAILABLE_SLOTS: [NY_SLOT], "name": " Grace ", "notes": "VIP"})

    outcome = await book_slot(ctx, "2026-11-04T10:00:00")

    assert http.calls[0].body is not None
    assert isinstance(http.calls[0].body, dict)
    assert http.calls[0].body["name"] == "Grace"
    assert http.calls[0].body["notes"] == "VIP"
    assert outcome.tool_message["booking_start"] == NY_SLOT["start"]


async def test_book_slot_rejects_an_unknown_slot_without_a_request() -> None:
    http = FakeHttp()
    ctx = ctx_with(http, variables={AVAILABLE_SLOTS: [NY_SLOT]})
    for start in ("2026-11-04T11:00:00-05:00", "tomorrow"):
        outcome = await book_slot(ctx, start)
        assert outcome.ok is False
        assert outcome.tool_message == {"error": "slot_unavailable"}
    assert http.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        ok(status=409),
        ok({"booking_id": None}),
        ok({"booking_id": {"x": 1}}),
        ok("text"),
        HttpOutcome(kind="timeout"),
    ],
)
async def test_failed_booking_routes_to_the_failure_node(outcome: HttpOutcome) -> None:
    ctx = ctx_with(FakeHttp([outcome]), variables={AVAILABLE_SLOTS: [NY_SLOT]})

    booked = await book_slot(ctx, NY_SLOT["start"])

    assert booked.ok is False
    assert booked.tool_message == {"error": "booking_failed", "status": outcome.status or 0}
    assert booked.route_to == "after"
    assert booked.report is not None
    assert booked.report.route_to == "after"
    assert "booking_id" not in ctx.state.variables


async def test_booking_without_an_http_port() -> None:
    ctx = handler_ctx(scheduling(), now=NOW, variables={AVAILABLE_SLOTS: [NY_SLOT]})
    booked = await book_slot(ctx, NY_SLOT["start"])
    assert booked.ok is False
    assert booked.tool_message == {"error": "http_unavailable"}
    assert booked.route_to == "after"


def test_scheduling_functions_are_valid_function_schemas() -> None:
    names = []
    parameters: list[dict[str, Any]] = []
    for function in SCHEDULING_FUNCTIONS:
        spec = function["function"]
        assert isinstance(spec, dict)
        schema = spec["parameters"]
        assert isinstance(schema, dict)
        Draft202012Validator.check_schema(schema)
        names.append(spec["name"])
        parameters.append(schema)
    assert names == ["list_slots", "book_slot"]
    validator = Draft202012Validator(parameters[1])
    assert validator.is_valid({"start": NY_SLOT["start"]})
    assert not validator.is_valid({})


async def test_respond_streams_the_dialogue_and_always_waits() -> None:
    ctx = ctx_with(FakeHttp())
    assert await spoken((await HANDLER.respond(ctx)).actions) == [("stream", "Hi there", "t3.1", None)]
    assert HANDLER.waits_for_user(ctx.node) is True
