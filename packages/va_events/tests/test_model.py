"""``DomainEvent`` validation, ``new_event``, and the wire encoding (specs/19-events-and-webhooks.md §1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import orjson
import pytest
from event_helpers import ORG, event
from pydantic import ValidationError

from va_common.time import FrozenClock
from va_events import DomainEvent, EventType, decode, encode, event_uuid, is_call_event, new_event


def test_new_event_uses_the_clock_and_a_uuid7_id() -> None:
    clock = FrozenClock(datetime(2026, 11, 1, 9, 30, tzinfo=UTC))
    created = new_event(EventType.CALL_QUEUED, org_id=ORG, clock=clock, message="queued")
    assert created.occurred_at == clock.now()
    assert created.id.startswith("evt_")
    assert event_uuid(created).version == 7
    assert created.seq is None
    assert created.level == "info"
    assert created.data == {}


def test_ids_are_unique() -> None:
    assert len({event(EventType.KB_READY).id for _ in range(50)}) == 50


def test_offset_datetimes_are_converted_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    parsed = DomainEvent(
        id="evt_" + "a" * 32,
        type=EventType.KB_READY,
        org_id=ORG,
        occurred_at=datetime(2026, 11, 1, 7, tzinfo=eastern),
    )
    assert parsed.occurred_at == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert parsed.occurred_at.tzinfo is UTC


@pytest.mark.parametrize(
    "overrides",
    [
        {"occurred_at": datetime(2026, 11, 1, 12)},  # noqa: DTZ001 - the naive datetime under test
        {"id": "evt_123"},
        {"id": "ev_" + "a" * 32},
        {"type": "call.unknown"},
        {"level": "fatal"},
        {"category": "nonsense"},
        {"unexpected": 1},
    ],
)
def test_invalid_events_are_rejected(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "id": "evt_" + "b" * 32,
        "type": "call.turn",
        "org_id": str(ORG),
        "occurred_at": datetime(2026, 11, 1, tzinfo=UTC),
    }
    with pytest.raises(ValidationError):
        DomainEvent.model_validate(values | overrides)


def test_events_are_frozen() -> None:
    created = event(EventType.CALL_TURN)
    with pytest.raises(ValidationError):
        created.message = "changed"


def test_encode_decode_round_trip() -> None:
    call_id = uuid.uuid4()
    original = event(
        EventType.CALL_TURN,
        call_id=call_id,
        category="call",
        data={"role": "user", "nested": {"n": [1, 2.5, None]}},
    ).model_copy(update={"seq": 4})
    payload = encode(original)
    assert orjson.loads(payload)["type"] == "call.turn"
    assert orjson.loads(payload)["seq"] == 4
    assert decode(payload) == original
    assert decode(payload.decode()) == original


def test_decode_rejects_non_events() -> None:
    with pytest.raises(ValidationError):
        decode(b'{"type": "call.turn"}')
    with pytest.raises(ValidationError):
        decode(b"not json")


def test_call_events_are_events_with_a_call_id() -> None:
    assert is_call_event(event(EventType.CITATIONS_READY, call_id=uuid.uuid4()))
    assert not is_call_event(event(EventType.BILLING_RECHARGED))
