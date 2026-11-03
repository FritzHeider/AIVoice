"""Webhook bodies: bland-live (specs/19 §5), batch status (§6), envelope (§7), HIPAA transcript strip."""

from __future__ import annotations

import copy
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta, timezone

from event_helpers import ORG, event
from pydantic import JsonValue

from va_db.models import CallEvent
from va_events import (
    BatchFacts,
    EventType,
    batch_body,
    envelope_body,
    iso_ms,
    live_body,
    row_body,
    row_for,
    strip_phi,
)
from va_events.webhook_payloads import content_body

CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000aa")
BATCH = uuid.UUID("0193c5d2-0000-7000-8000-0000000000bb")
CONVERSATION = uuid.UUID("0193c5d2-0000-7000-8000-0000000000cc")
CREATED = datetime(2026, 10, 24, 18, 0, 0, 5000, tzinfo=UTC)
UPDATED = datetime(2026, 10, 24, 18, 22, 31, 412999, tzinfo=UTC)


def _batch(**over: object) -> BatchFacts:
    values: dict[str, object] = {
        "batch_id": BATCH,
        "calls_total": 1200,
        "calls_dispatched": 340,
        "calls_completed": 290,
        "calls_successful": 251,
        "calls_failed": 39,
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    values.update(over)
    return BatchFacts(**values)  # type: ignore[arg-type]


def _call_object() -> dict[str, JsonValue]:
    return {
        "call_id": str(CALL),
        "summary": "Booked a table.",
        "transcripts": [{"id": 1, "user": "assistant", "text": "Hello"}],
        "concatenated_transcript": "assistant: Hello\n",
        "pathway_logs": [
            {"node_id": "n1", "role": "assistant", "text": "Hello", "user_text": "Hi", "decision": {"x": 1}},
            "not-an-object",
        ],
    }


def test_iso_ms_converts_to_utc_and_truncates_to_milliseconds() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert iso_ms(datetime(2026, 10, 24, 20, 22, 31, 412999, tzinfo=plus_two)) == "2026-10-24T18:22:31.412Z"
    assert iso_ms(CREATED) == "2026-10-24T18:00:00.005Z"


def test_live_body_has_exactly_the_bland_live_keys() -> None:
    latency = event(
        EventType.CALL_LATENCY,
        call_id=CALL,
        level="info",
        message="LLM: 266ms",
        data={"llm_ttft_ms": 266, "turn_index": 4},
    ).model_copy(update={"seq": 57})

    body = live_body(latency, {"crm": "42"})

    assert body == {
        "call_id": str(CALL),
        "category": "latency",
        "level": "info",
        "message": "LLM: 266ms",
        "data": {"llm_ttft_ms": 266, "turn_index": 4},
        "seq": 57,
        "created_at": iso_ms(latency.occurred_at),
        "metadata": {"crm": "42"},
    }


def test_row_body_of_a_stored_row_equals_the_live_body_of_its_event() -> None:
    turn = event(
        EventType.CALL_TURN, call_id=CALL, message="assistant", data={"text": "Hi", "turn_index": 0}
    ).model_copy(update={"seq": 3})
    stored = row_for(turn)
    assert stored is not None
    assert stored.event_type == "call.turn"

    record = CallEvent(**dataclasses.asdict(stored))

    assert row_body(record, {"crm": "42"}) == live_body(turn, {"crm": "42"})


def test_live_body_prefers_the_event_category() -> None:
    error = event(EventType.CALL_ERROR, call_id=CALL, category="webhook", level="warning")
    assert live_body(error, {})["category"] == "webhook"
    assert live_body(event(EventType.CALL_ERROR, call_id=CALL), {})["category"] == "error"


def test_batch_body_in_progress() -> None:
    body = batch_body(event(EventType.BATCH_IN_PROGRESS, batch_id=BATCH), _batch())
    assert body == {
        "batch_id": str(BATCH),
        "status": "in_progress",
        "phase": "batch.in_progress",
        "calls_total": 1200,
        "calls_dispatched": 340,
        "calls_completed": 290,
        "calls_successful": 251,
        "calls_failed": 39,
        "created_at": "2026-10-24T18:00:00.005Z",
        "updated_at": "2026-10-24T18:22:31.412Z",
    }


def test_batch_body_terminal_success_adds_completed_at() -> None:
    done = datetime(2026, 10, 24, 19, tzinfo=UTC)
    completed = batch_body(event(EventType.BATCH_COMPLETED, batch_id=BATCH), _batch(completed_at=done))
    assert completed["completed_at"] == "2026-10-24T19:00:00.000Z"
    assert "code" not in completed
    stopped_event = event(EventType.BATCH_STOPPED, batch_id=BATCH)
    stopped = batch_body(stopped_event, _batch())
    assert stopped["status"] == "stopped"
    assert stopped["completed_at"] == iso_ms(stopped_event.occurred_at)


def test_batch_body_failure_has_code_and_reason() -> None:
    from_row = batch_body(
        event(EventType.BATCH_FAILED, batch_id=BATCH, data={"code": "ignored"}),
        _batch(failure_code="invalid_csv", failure_reason="Row 3 has no phone number."),
    )
    assert (from_row["code"], from_row["reason"]) == ("invalid_csv", "Row 3 has no phone number.")
    assert "completed_at" not in from_row
    from_event = batch_body(
        event(
            EventType.BATCH_FAILED, batch_id=BATCH, data={"code": "no_credits", "reason": "Out of credit."}
        ),
        _batch(),
    )
    assert (from_event["code"], from_event["reason"]) == ("no_credits", "Out of credit.")
    default = batch_body(event(EventType.BATCH_FAILED, batch_id=BATCH), _batch())
    assert (default["code"], default["reason"]) == ("batch_failed", "")


def test_envelope_for_call_completed_carries_the_call_object() -> None:
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data=_call_object())
    body = envelope_body(completed, content_body(completed, metadata={"a": 1}, batch=None))
    assert body == {
        "id": completed.id,
        "type": "call.completed",
        "created_at": iso_ms(completed.occurred_at),
        "org_id": str(ORG),
        "call_id": str(CALL),
        "batch_id": None,
        "conversation_id": None,
        "data": _call_object(),
    }


def test_envelope_for_an_sms_event_carries_its_data() -> None:
    sms = event(EventType.SMS_RECEIVED, conversation_id=CONVERSATION, data={"text": "hi"})
    body = envelope_body(sms, content_body(sms, metadata=None, batch=None))
    assert body["conversation_id"] == str(CONVERSATION)
    assert body["call_id"] is None
    assert body["data"] == {"text": "hi"}


def test_content_body_by_event_kind() -> None:
    turn = event(EventType.CALL_TURN, call_id=CALL, data={"role": "user", "text": "Hi"})
    assert content_body(turn, metadata=None, batch=None) == live_body(turn, {})
    phase = event(EventType.BATCH_CREATED, batch_id=BATCH, data={"x": 1})
    assert content_body(phase, metadata=None, batch=_batch())["phase"] == "batch.created"
    assert content_body(phase, metadata=None, batch=None) == {"x": 1}


def test_strip_phi_clears_call_object_transcripts_without_mutating_the_input() -> None:
    original = _call_object()
    snapshot = copy.deepcopy(original)

    stripped = strip_phi(EventType.CALL_COMPLETED, original)

    assert original == snapshot
    assert stripped["transcripts"] == []
    assert stripped["concatenated_transcript"] == ""
    assert stripped["pathway_logs"] == [
        {"node_id": "n1", "role": "assistant", "decision": {"x": 1}},
        "not-an-object",
    ]
    assert stripped["summary"] == "Booked a table."


def test_strip_phi_removes_turn_text_only() -> None:
    for event_type in (EventType.CALL_TURN, EventType.CALL_POST_TRANSFER_TRANSCRIPT):
        live = live_body(event(event_type, call_id=CALL, data={"role": "user", "text": "My SSN"}), {})
        stripped = strip_phi(event_type, live)
        assert stripped["data"] == {"role": "user"}
        assert live["data"] == {"role": "user", "text": "My SSN"}
    latency = live_body(event(EventType.CALL_LATENCY, call_id=CALL, data={"text": "kept"}), {})
    assert strip_phi(EventType.CALL_LATENCY, latency) is latency
    no_text = live_body(event(EventType.CALL_TURN, call_id=CALL, data={"role": "user"}), {})
    assert strip_phi(EventType.CALL_TURN, no_text) is no_text
