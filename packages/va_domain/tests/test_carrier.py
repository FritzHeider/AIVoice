"""Carrier event contract and per-call key formats (specs/06 §5 Decision P05-T02; specs/03 §11)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from va_domain.carrier import (
    CARRIER_SIGNAL,
    CARRIER_STATE_TTL_S,
    CarrierEvent,
    call_workflow_id,
    carrier_channel,
    carrier_state_key,
    owner_key,
)

CALL_ID = uuid.UUID("0190f5c2-0000-7000-8000-000000000001")


def _event(**values: object) -> CarrierEvent:
    fields: dict[str, object] = {
        "type": "call.answered",
        "leg": "primary",
        "call_id": CALL_ID,
        "org_id": uuid.UUID(int=7),
        "telnyx_event_id": "evt-1",
        "occurred_at": datetime(2026, 11, 1, 12, tzinfo=UTC),
        "payload": {"call_control_id": "v3:abc", "nested": {"a": [1, None]}},
    }
    return CarrierEvent.model_validate({**fields, **values})


def test_round_trips_through_json() -> None:
    event = _event()
    assert CarrierEvent.model_validate_json(event.model_dump_json()) == event


def test_rejects_unknown_fields_and_legs() -> None:
    with pytest.raises(ValidationError):
        _event(extra=1)
    with pytest.raises(ValidationError):
        _event(leg="caller")


def test_payload_defaults_to_empty() -> None:
    event = CarrierEvent(
        type="streaming.started",
        leg="primary",
        call_id=CALL_ID,
        org_id=CALL_ID,
        telnyx_event_id="e",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert event.payload == {}


def test_keys_use_the_hash_tag_form() -> None:
    tag = "{0190f5c2-0000-7000-8000-000000000001}"
    assert carrier_state_key(CALL_ID) == f"call:{tag}:carrier_state"
    assert carrier_channel(CALL_ID) == f"call:{tag}:carrier"
    assert owner_key(CALL_ID) == f"call:{tag}:owner"


def test_workflow_id_signal_and_ttl() -> None:
    assert call_workflow_id(CALL_ID) == "call-0190f5c2-0000-7000-8000-000000000001"
    assert CARRIER_SIGNAL == "signal_carrier_event"
    assert CARRIER_STATE_TTL_S == 2 * 3600
