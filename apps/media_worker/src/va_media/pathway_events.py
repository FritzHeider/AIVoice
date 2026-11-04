"""Messages of the pathway engine's call events (specs/19 §2 ``call.route``, ``call.extraction``,
``call.loop_condition``, ``call.webhook_node``, ``call.sms_sent``; specs/11 §10 Decision P07-T12b).

The engine emits ``(event_type, data)``; the media worker publishes each as a call event whose ``message`` is
fixed text plus node ids and numbers only (never caller text, variable values, URLs or phone numbers: those
stay in ``data``, which the Call Object's ``pathway_logs`` reads).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue

from va_events import EventType

__all__ = ["DEFAULT_MESSAGE", "PATHWAY_EVENT_TYPES", "event_message"]

PATHWAY_EVENT_TYPES: Final = frozenset(
    {
        EventType.CALL_ROUTE,
        EventType.CALL_EXTRACTION,
        EventType.CALL_LOOP_CONDITION,
        EventType.CALL_WEBHOOK_NODE,
        EventType.CALL_SMS_SENT,
    }
)
"""The call event types a pathway engine emits."""

DEFAULT_MESSAGE: Final = "Pathway event"


def _text(data: Mapping[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _route(data: Mapping[str, JsonValue]) -> str:
    target = _text(data, "to_node")
    source = _text(data, "from_node")
    if target is None:
        return "Pathway route decided"
    if source is None or source == target:
        return f"Pathway stayed on node {target}"
    return f"Pathway routed from node {source} to node {target}"


def _extraction(data: Mapping[str, JsonValue]) -> str:
    variables = data.get("variables")
    count = len(variables) if isinstance(variables, dict) else 0
    node = _text(data, "node_id")
    where = f" at node {node}" if node is not None else ""
    return f"Extracted {count} variable{'s' if count != 1 else ''}{where}"


def _condition(data: Mapping[str, JsonValue]) -> str:
    met = "met" if data.get("met") is True else "not met"
    score = data.get("score")
    scored = f" (score {score}/10)" if isinstance(score, int) else ""
    return f"Loop condition {met}{scored}"


def _webhook(data: Mapping[str, JsonValue]) -> str:
    node = _text(data, "node_id") or "unknown"
    status = data.get("status_code")
    outcome = f"status {status}" if isinstance(status, int) else (_text(data, "outcome") or "no response")
    return f"Webhook node {node}: {outcome}"


def _sms(data: Mapping[str, JsonValue]) -> str:
    return "SMS node message sent" if data.get("sent") is True else "SMS node message not sent"


def event_message(event_type: EventType, data: Mapping[str, JsonValue]) -> str:
    """The ``message`` of a pathway event of ``event_type`` with ``data``."""
    if event_type is EventType.CALL_ROUTE:
        return _route(data)
    if event_type is EventType.CALL_EXTRACTION:
        return _extraction(data)
    if event_type is EventType.CALL_LOOP_CONDITION:
        return _condition(data)
    if event_type is EventType.CALL_WEBHOOK_NODE:
        return _webhook(data)
    if event_type is EventType.CALL_SMS_SENT:
        return _sms(data)
    return DEFAULT_MESSAGE
