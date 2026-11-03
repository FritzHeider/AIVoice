"""``row_for`` / ``event_of``: a ``call_events`` row and the call event it was written from."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from event_helpers import event, row

from va_events import EventType, event_of, row_for


@pytest.mark.parametrize(
    ("event_type", "category"),
    [(EventType.CALL_LATENCY, "latency"), (EventType.CALL_STARTED, "call"), (EventType.CALL_ERROR, "error")],
)
def test_event_of_rebuilds_the_sequenced_call_event(event_type: EventType, category: str) -> None:
    original = event(
        event_type,
        call_id=uuid.uuid4(),
        category=category,  # type: ignore[arg-type]
        level="warning",
        message="E2E: 742ms",
        data={"e2e_ms": 742, "stages": {"stt": 120}},
    ).model_copy(update={"seq": 7})
    stored = row_for(original)
    assert stored is not None
    assert event_of(stored) == original


def test_rows_without_an_event_type_are_not_rebuilt() -> None:
    assert event_of(row(uuid.uuid4(), 1)) is None


def test_rows_with_an_unknown_event_type_are_not_rebuilt() -> None:
    assert event_of(dataclasses.replace(row(uuid.uuid4(), 1), event_type="call.unheard_of")) is None
