"""Per-attempt call log entries of call webhooks (specs/19-events-and-webhooks.md §8).

Every attempt of a delivery that carries ``call_id`` becomes a ``call.webhook_delivery`` event (category
``webhook``): ``info`` on success, ``warning`` when a retry is scheduled, ``error`` when the delivery is dead.
The message names the host only, never the URL (it may carry tokens).
"""

from __future__ import annotations

from typing import Final

from va_common.time import Clock
from va_db.models import CallEventLevel
from va_events import DomainEvent, EventType, new_event
from va_events.webhook_delivery import HTTP_STATUS, Outcome, Outgoing, SendResult

__all__ = ["attempt_event", "attempt_level", "attempt_message", "human_delay"]

_LEVELS: Final[dict[str, CallEventLevel]] = {"succeeded": "info", "retry": "warning", "dead": "error"}
_MINUTE: Final = 60
_HOUR: Final = 3600


def human_delay(seconds: float) -> str:
    """``42s``, ``10m``, ``6h`` (rounded to the largest whole unit below the next one)."""
    whole = round(seconds)
    if whole < _MINUTE:
        return f"{whole}s"
    if whole < _HOUR:
        return f"{round(whole / _MINUTE)}m"
    return f"{round(whole / _HOUR)}h"


def _failure_label(result: SendResult) -> str:
    if result.error == HTTP_STATUS and result.status_code is not None:
        return str(result.status_code)
    return result.error or ""


def attempt_message(delivery: Outgoing, result: SendResult, outcome: Outcome) -> str:
    """The call log line of one attempt."""
    host = delivery.host
    if outcome.status == "succeeded":
        return f"Webhook delivered to {host}: {result.status_code} ({result.response_ms}ms)"
    label = _failure_label(result)
    if outcome.status == "retry" and outcome.delay_s is not None:
        return (
            f"Webhook to {host} failed: {label} (attempt {delivery.attempt}), "
            f"retrying in {human_delay(outcome.delay_s)}"
        )
    return f"Webhook to {host} failed: {label}, giving up"


def attempt_level(outcome: Outcome) -> CallEventLevel:
    """``info`` / ``warning`` / ``error`` for succeeded / retry / dead."""
    return _LEVELS[outcome.status]


def attempt_event(
    delivery: Outgoing, result: SendResult, outcome: Outcome, *, clock: Clock
) -> DomainEvent | None:
    """The unsequenced ``call.webhook_delivery`` event of an attempt; ``None`` for non-call deliveries."""
    if delivery.call_id is None:
        return None
    return new_event(
        EventType.CALL_WEBHOOK_DELIVERY,
        org_id=delivery.org_id,
        clock=clock,
        call_id=delivery.call_id,
        category="webhook",
        level=attempt_level(outcome),
        message=attempt_message(delivery, result, outcome),
        data={
            "delivery_id": str(delivery.id),
            "event_id": delivery.event_id,
            "event_type": delivery.event_type,
            "attempt": delivery.attempt,
            "status_code": result.status_code,
            "error": result.error,
            "response_ms": result.response_ms,
            "host": delivery.host,
        },
    )
