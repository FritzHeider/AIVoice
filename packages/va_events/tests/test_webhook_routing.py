"""The routing matrix of specs/19-events-and-webhooks.md §3 (pure planner)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from event_helpers import event
from pydantic import JsonValue

from va_events import BatchFacts, EventType, live_body
from va_events.webhook_routing import (
    CallFacts,
    DeliveryPolicy,
    EndpointTarget,
    PlannedDelivery,
    RoutingFacts,
    matches,
    plan_deliveries,
)

CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000aa")
BATCH = uuid.UUID("0193c5d2-0000-7000-8000-0000000000bb")
CONVERSATION = uuid.UUID("0193c5d2-0000-7000-8000-0000000000cc")
WIDGET = uuid.UUID("0193c5d2-0000-7000-8000-0000000000dd")
ENDPOINT = uuid.UUID("0193c5d2-0000-7000-8000-0000000000ee")
CALL_URL = "https://hooks.acme.test/call"
ENDPOINT_URL = "https://hooks.acme.test/org"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
CALL_OBJECT: dict[str, JsonValue] = {
    "call_id": str(CALL),
    "transcripts": [{"text": "hi"}],
    "concatenated_transcript": "user: hi\n",
}


def _call(*keys: str, webhook: str | None = CALL_URL) -> RoutingFacts:
    return RoutingFacts(call=CallFacts(webhook=webhook, webhook_events=frozenset(keys), metadata={"m": 1}))


def _endpoint(*patterns: str, url: str = ENDPOINT_URL, phi_allowed: bool = False) -> EndpointTarget:
    return EndpointTarget(id=ENDPOINT, url=url, event_types=patterns, phi_allowed=phi_allowed)


def _urls(deliveries: list[PlannedDelivery]) -> list[tuple[str, str]]:
    return [(d.url, d.format) for d in deliveries]


def test_call_completed_always_reaches_the_call_webhook_as_the_bare_call_object() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=CALL_OBJECT)
    (delivery,) = plan_deliveries(completed, _call())
    assert delivery == PlannedDelivery(CALL_URL, "bland", CALL_OBJECT, None)


@pytest.mark.parametrize(
    ("event_type", "keys", "delivered"),
    [
        (EventType.CALL_LATENCY, ("latency",), True),
        (EventType.CALL_LATENCY, ("call",), False),
        (EventType.CALL_LATENCY, (), False),
        (EventType.CALL_AMD, ("call",), True),
        (EventType.CALL_QUEUED, ("queue",), True),
        (EventType.CALL_TOOL, ("tool", "call"), True),
        (EventType.CALL_WEBHOOK_NODE, ("webhook",), True),
        (EventType.CALL_POST_TRANSFER_TRANSCRIPT, ("post_transfer_transcript",), True),
        (EventType.CITATIONS_READY, ("citations",), True),
        (EventType.EVALS_COMPLETED, ("call",), False),
        (EventType.CALL_ANALYSIS_READY, ("call", "queue", "latency"), False),
    ],
)
def test_live_events_follow_webhook_events(
    event_type: EventType, keys: tuple[str, ...], delivered: bool
) -> None:
    live = event(event_type, call_id=CALL, data={"k": 1}).model_copy(update={"seq": 3})
    deliveries = plan_deliveries(live, _call(*keys))
    if not delivered:
        assert deliveries == []
        return
    (delivery,) = deliveries
    assert (delivery.url, delivery.format) == (CALL_URL, "bland")
    assert delivery.payload == live_body(live, {"m": 1})


def test_no_call_webhook_means_no_call_delivery() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=CALL_OBJECT)
    assert plan_deliveries(completed, _call(webhook=None)) == []
    assert plan_deliveries(completed, RoutingFacts()) == []


def test_org_webhook_format_envelope_wraps_the_per_call_webhook() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=CALL_OBJECT)
    (delivery,) = plan_deliveries(completed, _call(), DeliveryPolicy(call_format="envelope"))
    assert delivery.format == "envelope"
    assert delivery.payload["id"] == completed.id
    assert delivery.payload["data"] == CALL_OBJECT


def test_batch_phases_reach_the_status_webhook() -> None:
    facts = RoutingFacts(
        batch=BatchFacts(BATCH, 10, 5, 4, 3, 1, NOW, NOW), batch_url="https://hooks.acme.test/batch"
    )
    phase = event(EventType.BATCH_IN_PROGRESS, batch_id=BATCH)
    (delivery,) = plan_deliveries(phase, facts)
    assert delivery.format == "bland"
    assert delivery.payload["status"] == "in_progress"
    assert delivery.payload["calls_total"] == 10
    assert plan_deliveries(event(EventType.BATCH_IN_PROGRESS), facts) == []
    no_url = RoutingFacts(batch=facts.batch)
    assert plan_deliveries(phase, no_url) == []


def test_conversation_and_widget_webhooks_receive_envelopes() -> None:
    facts = RoutingFacts(
        conversation_url="https://hooks.acme.test/conv", widget_url="https://hooks.acme.test/widget"
    )
    sms = event(EventType.SMS_RECEIVED, conversation_id=CONVERSATION, data={"text": "hi"})
    assert _urls(plan_deliveries(sms, facts)) == [("https://hooks.acme.test/conv", "envelope")]
    message = event(EventType.WIDGET_MESSAGE, widget_id=WIDGET, data={"text": "hi"})
    (widget,) = plan_deliveries(message, facts)
    assert (widget.url, widget.format) == ("https://hooks.acme.test/widget", "envelope")
    assert widget.payload["data"] == {"text": "hi"}
    both = event(EventType.CONVERSATION_MESSAGE, conversation_id=CONVERSATION, widget_id=WIDGET)
    assert _urls(plan_deliveries(both, facts)) == [
        ("https://hooks.acme.test/conv", "envelope"),
        ("https://hooks.acme.test/widget", "envelope"),
    ]
    assert plan_deliveries(event(EventType.SMS_RECEIVED), facts) == []
    assert plan_deliveries(event(EventType.BILLING_LOW_BALANCE, conversation_id=CONVERSATION), facts) == []


@pytest.mark.parametrize(
    ("patterns", "event_type", "matched"),
    [
        (("*",), EventType.KB_READY, True),
        (("call.*",), EventType.CALL_COMPLETED, True),
        (("call.*",), EventType.CITATIONS_READY, False),
        (("sms.received",), EventType.SMS_RECEIVED, True),
        (("sms.received",), EventType.SMS_SENT, False),
        (("billing.*", "kb.*"), EventType.KB_FAILED, True),
        ((), EventType.CALL_COMPLETED, False),
        (("CALL.*",), EventType.CALL_COMPLETED, False),
    ],
)
def test_endpoint_globs(patterns: tuple[str, ...], event_type: EventType, matched: bool) -> None:
    assert matches(event_type, patterns) is matched
    facts = RoutingFacts(endpoints=(_endpoint(*patterns),))
    deliveries = plan_deliveries(event(event_type), facts)
    assert _urls(deliveries) == ([(ENDPOINT_URL, "envelope")] if matched else [])
    assert all(d.endpoint_id == ENDPOINT for d in deliveries)


def test_endpoint_envelope_of_a_live_call_event_wraps_the_live_body() -> None:
    turn = event(EventType.CALL_TURN, call_id=CALL, data={"text": "hi"})
    facts = RoutingFacts(call=CallFacts(webhook=None, metadata={"m": 1}), endpoints=(_endpoint("call.*"),))
    (delivery,) = plan_deliveries(turn, facts)
    assert delivery.payload["data"] == live_body(turn, {"m": 1})


def test_a_url_shared_by_the_call_webhook_and_an_endpoint_is_delivered_once_in_bland_format() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=CALL_OBJECT)
    facts = RoutingFacts(
        call=CallFacts(webhook=CALL_URL),
        endpoints=(_endpoint("*", url=CALL_URL), _endpoint("*", url="https://other.test/h")),
    )
    assert _urls(plan_deliveries(completed, facts)) == [
        (CALL_URL, "bland"),
        ("https://other.test/h", "envelope"),
    ]


def test_hipaa_strips_transcripts_unless_the_endpoint_allows_phi() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=CALL_OBJECT)
    facts = RoutingFacts(
        call=CallFacts(webhook=CALL_URL),
        endpoints=(
            _endpoint("*", url="https://phi.test/h", phi_allowed=True),
            _endpoint("*", url="https://nophi.test/h"),
        ),
    )
    call_hook, phi, no_phi = plan_deliveries(completed, facts, DeliveryPolicy(hipaa=True))
    assert call_hook.payload["transcripts"] == []
    assert phi.payload["data"] == CALL_OBJECT
    no_phi_data = no_phi.payload["data"]
    assert isinstance(no_phi_data, dict)
    assert no_phi_data["concatenated_transcript"] == ""
    untouched = plan_deliveries(completed, facts)
    assert untouched[0].payload == CALL_OBJECT
    assert untouched[2].payload["data"] == CALL_OBJECT


@pytest.mark.parametrize("patterns", [("*",), ("billing.*",), ("billing.usage",)])
def test_internal_types_reach_no_webhook(patterns: tuple[str, ...]) -> None:
    call = CallFacts(webhook=CALL_URL, webhook_events=frozenset({"call"}), metadata={})
    facts = RoutingFacts(call=call, endpoints=(_endpoint(*patterns),))
    assert plan_deliveries(event(EventType.BILLING_USAGE, call_id=CALL), facts) == []


def test_other_billing_events_still_reach_a_star_endpoint() -> None:
    facts = RoutingFacts(endpoints=(_endpoint("*"),))
    assert _urls(plan_deliveries(event(EventType.BILLING_LOW_BALANCE), facts)) == [(ENDPOINT_URL, "envelope")]
