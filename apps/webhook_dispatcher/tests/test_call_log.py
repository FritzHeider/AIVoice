"""Call log entries of delivery attempts and the next-state decision (specs/19 §8)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from va_common.time import FrozenClock
from va_events import EventType
from va_events.webhook_delivery import Outcome, Outgoing, SendResult, blocked, decide
from va_webhooks.call_log import attempt_event, attempt_message, human_delay

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class Mid(random.Random):
    def random(self) -> float:
        return 0.5


def outgoing(
    *, attempts: int = 0, call_id: uuid.UUID | None = None, url: str = "https://Hooks.Example.com/x?t=1"
) -> Outgoing:
    return Outgoing(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        event_id="evt_1",
        event_type="call.completed",
        url=url,
        endpoint_id=None,
        payload={},
        format="bland",
        attempts=attempts,
        call_id=call_id,
    )


@pytest.mark.parametrize(
    ("result", "attempt", "expected"),
    [
        (SendResult(200, None, 12), 1, Outcome("succeeded")),
        (SendResult(204, None, 12), 9, Outcome("succeeded")),
        (SendResult(410, "gone", 5), 1, Outcome("dead")),
        (SendResult(None, "endpoint_disabled", None), 1, Outcome("dead")),
        (SendResult(None, blocked("scheme"), None), 1, Outcome("dead")),
        (SendResult(500, "http_status", 5), 9, Outcome("dead")),
        (SendResult(None, "timeout", 500), 1, Outcome("retry", NOW + timedelta(seconds=10), 10.0)),
        (SendResult(302, "redirect", 5), 3, Outcome("retry", NOW + timedelta(seconds=120), 120.0)),
        (
            SendResult(None, blocked("address"), None),
            8,
            Outcome("retry", NOW + timedelta(seconds=54000), 54000.0),
        ),
        (
            SendResult(None, "secret_unavailable", None),
            2,
            Outcome("retry", NOW + timedelta(seconds=30), 30.0),
        ),
    ],
)
def test_decide(result: SendResult, attempt: int, expected: Outcome) -> None:
    assert decide(attempt, result, NOW, Mid()) == expected


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(9.6, "10s"), (59.4, "59s"), (60, "1m"), (118, "2m"), (3599, "60m"), (21600, "6h"), (59400, "16h")],
)
def test_human_delay(seconds: float, text: str) -> None:
    assert human_delay(seconds) == text


def test_messages_name_the_host_only() -> None:
    first = outgoing()
    assert attempt_message(first, SendResult(200, None, 123), Outcome("succeeded")) == (
        "Webhook delivered to hooks.example.com: 200 (123ms)"
    )
    assert attempt_message(first, SendResult(500, "http_status", 9), Outcome("retry", NOW, 30.4)) == (
        "Webhook to hooks.example.com failed: 500 (attempt 1), retrying in 30s"
    )
    assert attempt_message(outgoing(attempts=8), SendResult(None, "timeout", 15000), Outcome("dead")) == (
        "Webhook to hooks.example.com failed: timeout, giving up"
    )
    assert outgoing(url="not a url").host == ""
    assert outgoing(url="http://[::1").host == ""


def test_attempt_event_only_for_call_deliveries() -> None:
    clock = FrozenClock(NOW)
    assert attempt_event(outgoing(), SendResult(200, None, 1), Outcome("succeeded"), clock=clock) is None
    call_id = uuid.uuid4()
    delivery = outgoing(attempts=1, call_id=call_id)
    event = attempt_event(
        delivery, SendResult(503, "http_status", 40), Outcome("retry", NOW, 30.0), clock=clock
    )
    assert event is not None
    assert (event.type, event.call_id, event.org_id, event.category, event.level) == (
        EventType.CALL_WEBHOOK_DELIVERY,
        call_id,
        delivery.org_id,
        "webhook",
        "warning",
    )
    assert event.data == {
        "delivery_id": str(delivery.id),
        "event_id": "evt_1",
        "event_type": "call.completed",
        "attempt": 2,
        "status_code": 503,
        "error": "http_status",
        "response_ms": 40,
        "host": "hooks.example.com",
    }
    dead = attempt_event(delivery, SendResult(410, "gone", 3), Outcome("dead"), clock=clock)
    assert dead is not None
    assert dead.level == "error"
    assert "?t=1" not in dead.message
