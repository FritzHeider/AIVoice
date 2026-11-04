"""Messages of the pathway engine's call events (specs/19 §2; specs/11 §10 Decision P07-T12b)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import JsonValue

from va_events import EventType
from va_media.pathway_events import DEFAULT_MESSAGE, PATHWAY_EVENT_TYPES, event_message


@pytest.mark.parametrize(
    ("event_type", "data", "message"),
    [
        (EventType.CALL_ROUTE, {"from_node": "a", "to_node": "b"}, "Pathway routed from node a to node b"),
        (EventType.CALL_ROUTE, {"from_node": "a", "to_node": "a"}, "Pathway stayed on node a"),
        (EventType.CALL_ROUTE, {"to_node": "b"}, "Pathway stayed on node b"),
        (EventType.CALL_ROUTE, {"from_node": "a", "to_node": None}, "Pathway route decided"),
        (
            EventType.CALL_EXTRACTION,
            {"node_id": "a", "variables": {"x": 1}},
            "Extracted 1 variable at node a",
        ),
        (EventType.CALL_EXTRACTION, {"variables": {"x": 1, "y": 2}}, "Extracted 2 variables"),
        (EventType.CALL_EXTRACTION, {"variables": None}, "Extracted 0 variables"),
        (EventType.CALL_LOOP_CONDITION, {"score": 8, "met": True}, "Loop condition met (score 8/10)"),
        (EventType.CALL_LOOP_CONDITION, {"met": False}, "Loop condition not met"),
        (EventType.CALL_WEBHOOK_NODE, {"node_id": "h", "status_code": 500}, "Webhook node h: status 500"),
        (EventType.CALL_WEBHOOK_NODE, {"node_id": "h", "outcome": "timeout"}, "Webhook node h: timeout"),
        (EventType.CALL_WEBHOOK_NODE, {}, "Webhook node unknown: no response"),
        (EventType.CALL_SMS_SENT, {"sent": True}, "SMS node message sent"),
        (EventType.CALL_SMS_SENT, {"sent": False, "error": "telnyx_error"}, "SMS node message not sent"),
        (EventType.CALL_TURN, {}, DEFAULT_MESSAGE),
    ],
)
def test_messages_carry_node_ids_and_numbers_only(
    event_type: EventType, data: dict[str, Any], message: str
) -> None:
    values: dict[str, JsonValue] = data
    assert event_message(event_type, values) == message


def test_the_engine_types_are_the_spec_rows() -> None:
    assert {member.value for member in PATHWAY_EVENT_TYPES} == {
        "call.route",
        "call.extraction",
        "call.loop_condition",
        "call.webhook_node",
        "call.sms_sent",
    }
