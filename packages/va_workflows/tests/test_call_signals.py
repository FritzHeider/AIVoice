"""``CallSignals``: what ``CallWorkflow`` records from its signals (specs/01 §2.1 Decision P05-T06c)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from va_domain.call_workflow import PipelineFinished
from va_domain.carrier import CarrierEvent, CarrierLeg
from va_workflows.calls.state import CallSignals

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
CALL_ID = uuid.uuid4()


def event(
    event_type: str, *, leg: CarrierLeg = "primary", event_id: str | None = None, **payload: Any
) -> CarrierEvent:
    return CarrierEvent(
        type=event_type,
        leg=leg,
        call_id=CALL_ID,
        org_id=CALL_ID,
        telnyx_event_id=event_id or str(uuid.uuid4()),
        occurred_at=NOW,
        payload=payload,
    )


def test_answer_and_hangup_are_recorded() -> None:
    signals = CallSignals()
    assert signals.record(event("call.answered"), NOW) == "recorded"
    assert signals.record(event("call.hangup"), NOW) == "recorded"
    assert (signals.answered, signals.hung_up) == (True, True)


def test_redeliveries_count_once() -> None:
    signals = CallSignals()
    assert signals.record(event("call.answered", leg="transfer_target", event_id="e-1"), NOW) == "other_leg"
    assert signals.record(event("call.answered", event_id="e-1"), NOW) == "duplicate"
    assert not signals.answered


def test_only_a_premium_fax_result_is_a_fax() -> None:
    signals = CallSignals()
    assert signals.record(event("call.machine.premium.detection.ended", result="machine"), NOW) == "ignored"
    assert signals.record(event("call.machine.detection.ended", result="fax_detected"), NOW) == "ignored"
    assert not signals.fax
    assert (
        signals.record(event("call.machine.premium.detection.ended", result="fax_detected"), NOW)
        == "recorded"
    )
    assert signals.fax


def test_a_stream_failure_counts_only_once_answered_and_keeps_the_first_time() -> None:
    signals = CallSignals()
    assert signals.record(event("streaming.failed"), NOW) == "ignored"
    signals.record(event("call.answered"), NOW)
    assert signals.record(event("streaming.failed"), NOW) == "recorded"
    assert signals.record(event("streaming.failed"), NOW + timedelta(seconds=2)) == "ignored"
    assert signals.stream_failed_at == NOW


def test_recording_and_other_events_are_ignored() -> None:
    signals = CallSignals()
    assert signals.record(event("call.recording.saved"), NOW) == "ignored"
    assert signals == CallSignals(seen=signals.seen)


def test_a_cancel_counts_only_before_the_answer() -> None:
    signals = CallSignals()
    assert signals.request_cancel()
    assert signals.cancel_requested
    answered = CallSignals()
    answered.record(event("call.answered"), NOW)
    assert not answered.request_cancel()
    assert not answered.cancel_requested


def test_the_first_pipeline_end_is_kept() -> None:
    signals = CallSignals()
    first = PipelineFinished(end_reason="caller_hangup", ended_by="user")
    signals.finish_pipeline(first)
    signals.finish_pipeline(PipelineFinished(end_reason="error", ended_by="system"))
    assert signals.pipeline_finished == first
