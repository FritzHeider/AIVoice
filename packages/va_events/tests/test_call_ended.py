"""``call_ended_event`` (specs/19 §2 ``call.ended``; specs/07 §18 Decision P05-T11b)."""

from __future__ import annotations

import uuid

from va_common.time import FrozenClock
from va_events import ENDED_MESSAGE, EventType, call_ended_event, row_for


def test_call_ended_carries_the_reason_and_who_ended_it() -> None:
    clock = FrozenClock()
    org_id, call_id = uuid.uuid4(), uuid.uuid4()
    event = call_ended_event(clock, org_id, call_id, "no_input_timeout")
    assert event.type is EventType.CALL_ENDED
    assert (event.org_id, event.call_id, event.occurred_at) == (org_id, call_id, clock.now())
    assert event.message == ENDED_MESSAGE
    assert event.level == "info"
    assert event.data == {"end_reason": "no_input_timeout", "ended_by": "system"}
    assert event.seq is None
    assert row_for(event) is None, "unsequenced events have no call_events row yet"
