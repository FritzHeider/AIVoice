"""Webhook envelope parsing and the payload model of every voice and messaging event type.

Specs: specs/06 §4.1 step 5, specs/06 §5, specs/21 §5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import orjson
import pytest
from telnyx_spec import spec_voice_event_types

from va_telnyx import (
    MessageEventPayload,
    UnknownEventPayload,
    WebhookParseError,
    parse_envelope,
    parse_payload,
)
from va_telnyx.voice_events import (
    CallHangup,
    CallInitiated,
    CallPayload,
    ConferencePayload,
    DtmfReceived,
    PlaybackEnded,
    PremiumDetectionEnded,
    RecordingSaved,
    StreamingFailed,
)
from va_telnyx.webhooks import (
    MESSAGING_EVENT_TYPES,
    PAYLOAD_MODELS,
    VOICE_EVENT_TYPES,
    WebhookEvent,
)

CCID: Final = "v3:abc"
LEG: Final = {
    "call_control_id": CCID,
    "call_leg_id": "leg-1",
    "call_session_id": "session-1",
    "connection_id": "1494404757140276705",
    "client_state": "eyJ2IjoxfQ==",
    "from": "+14155550100",
    "to": "+14155550111",
    "direction": "outgoing",
    "state": "answered",
    "start_time": "2026-11-01T12:00:00.000000Z",
}


def _event(event_type: str, payload: dict[str, Any]) -> WebhookEvent:
    return WebhookEvent(
        id="e1", event_type=event_type, occurred_at=datetime(2026, 11, 1, tzinfo=UTC), payload=payload
    )


def _envelope(**data: Any) -> bytes:
    body = {
        "data": {
            "record_type": "event",
            "id": "0ccc7b54-4df3-4bca-a65a-3da1ecc777f0",
            "event_type": "call.answered",
            "occurred_at": "2026-11-01T12:00:00.123456Z",
            "payload": LEG,
            **data,
        },
        "meta": {"attempt": 2, "delivered_to": "https://edge.test/telnyx/voice"},
    }
    return orjson.dumps(body)


def test_models_cover_every_spec_event_type_and_the_message_events() -> None:
    spec_types = spec_voice_event_types()
    assert len(spec_types) >= 27
    assert spec_types == VOICE_EVENT_TYPES
    assert {"message.received", "message.sent", "message.finalized"} == MESSAGING_EVENT_TYPES
    assert set(PAYLOAD_MODELS) == VOICE_EVENT_TYPES | MESSAGING_EVENT_TYPES


def test_envelope_parses_with_meta_and_ignores_unknown_fields() -> None:
    envelope = parse_envelope(_envelope(extra_field=1))
    assert envelope.data.id == "0ccc7b54-4df3-4bca-a65a-3da1ecc777f0"
    assert envelope.data.record_type == "event"
    assert envelope.data.occurred_at == datetime(2026, 11, 1, 12, 0, 0, 123456, tzinfo=UTC)
    assert envelope.meta is not None
    assert (envelope.meta.attempt, envelope.meta.delivered_to) == (2, "https://edge.test/telnyx/voice")


def test_envelope_meta_and_payload_are_optional() -> None:
    body = orjson.dumps(
        {"data": {"id": "e1", "event_type": "call.hold", "occurred_at": "2026-11-01T12:00:00Z"}}
    )
    envelope = parse_envelope(body)
    assert envelope.meta is None
    assert envelope.data.payload == {}


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        orjson.dumps({"data": {"event_type": "call.answered", "occurred_at": "2026-11-01T12:00:00Z"}}),
        orjson.dumps({"data": {"id": "e1", "event_type": "call.answered", "occurred_at": "yesterday"}}),
        orjson.dumps({"meta": {}}),
    ],
)
def test_invalid_envelopes_raise_parse_errors(body: bytes) -> None:
    with pytest.raises(WebhookParseError):
        parse_envelope(body)


# One documented-shape payload per event type (specs/06 §5); extra unknown keys must be ignored.
PAYLOADS: Final[dict[str, dict[str, Any]]] = {
    "call.initiated": {
        **LEG,
        "direction": "incoming",
        "caller_id_name": "Ann",
        "custom_headers": [{"name": "X-VA-Call-Id", "value": "c1"}],
    },
    "call.answered": LEG,
    "call.bridged": LEG,
    "call.hangup": {
        **LEG,
        "hangup_cause": "user_busy",
        "hangup_source": "callee",
        "sip_hangup_cause": 486,
        "end_time": "2026-11-01T12:01:00Z",
        "call_quality_stats": {"inbound": {}},
    },
    "call.machine.detection.ended": {**LEG, "result": "not_sure"},
    "call.machine.greeting.ended": {**LEG, "result": "beep_detected"},
    "call.machine.premium.detection.ended": {**LEG, "result": "human_business"},
    "call.machine.premium.greeting.ended": {**LEG, "result": "beep_detected"},
    "call.machine.premium.call_screening.detected": LEG,
    "call.dtmf.received": {**LEG, "digit": "5"},
    "call.recording.saved": {
        **LEG,
        "channels": "dual",
        "recording_started_at": "2026-11-01T12:00:01Z",
        "recording_ended_at": "2026-11-01T12:01:00Z",
        "recording_urls": {"wav": "https://rec.test/1.wav", "mp3": None},
        "public_recording_urls": {"wav": "https://rec.test/p1.wav"},
    },
    "call.recording.error": {**LEG, "reason": "upload_failed"},
    "call.playback.started": {**LEG, "media_url": "https://hold.test/m.wav", "overlay": False},
    "call.playback.ended": {**LEG, "media_url": "https://hold.test/m.wav", "status": "cancelled"},
    "streaming.started": {**LEG, "stream_url": "wss://media.test/ws"},
    "streaming.stopped": {**LEG, "stream_url": "wss://media.test/ws"},
    "streaming.failed": {**LEG, "stream_id": "s1", "failure_reason": "connection_failed"},
    "conference.created": {"conference_id": "conf-1", "call_control_id": CCID},
    "conference.participant.joined": {"conference_id": "conf-1", "call_control_id": CCID},
    "conference.participant.left": {"conference_id": "conf-1", "call_control_id": CCID},
    "conference.ended": {"conference_id": "conf-1", "reason": "all_left"},
    "call.hold": LEG,
    "call.unhold": LEG,
    "call.deepfake_detection.result": {**LEG, "result": "real"},
    "call.deepfake_detection.error": {**LEG, "error": "audio_too_short"},
    "call.conversation.created": {**LEG, "conversation_id": "conv-1"},
    "call.conversation.start_failed": {**LEG, "conversation_id": "conv-1"},
}
MESSAGE: Final[dict[str, Any]] = {
    "record_type": "message",
    "direction": "inbound",
    "id": "msg-1",
    "type": "MMS",
    "messaging_profile_id": "mp-1",
    "from": {"phone_number": "+14155550100", "carrier": "SIM Wireless", "line_type": "Wireless"},
    "to": [{"phone_number": "+14155550111", "status": "webhook_delivered", "carrier": "Telnyx"}],
    "text": "STOP",
    "subject": None,
    "media": [{"url": "https://media.test/a.jpg", "content_type": "image/jpeg", "sha256": None}],
    "errors": [{"code": "40008", "title": "Undeliverable", "detail": "rejected"}],
    "cost": {"amount": "0.0051", "currency": "USD"},
    "encoding": "GSM-7",
    "parts": 1,
    "received_at": "2026-11-01T12:00:00.000+00:00",
    "sent_at": None,
    "completed_at": None,
    "webhook_url": "https://edge.test/telnyx/messaging",
    "tags": [],
}


def test_every_voice_fixture_is_covered() -> None:
    assert set(PAYLOADS) == VOICE_EVENT_TYPES


@pytest.mark.parametrize("event_type", sorted(PAYLOADS))
def test_every_voice_event_parses_into_its_model(event_type: str) -> None:
    parsed = parse_payload(_event(event_type, {**PAYLOADS[event_type], "brand_new_field": {"x": 1}}))
    assert isinstance(parsed, PAYLOAD_MODELS[event_type])
    assert isinstance(parsed, CallPayload | ConferencePayload)
    if isinstance(parsed, CallPayload):
        assert parsed.call_control_id == CCID
        assert parsed.from_ == "+14155550100"


def test_typed_fields_of_key_events() -> None:
    initiated = parse_payload(_event("call.initiated", PAYLOADS["call.initiated"]))
    assert isinstance(initiated, CallInitiated)
    assert initiated.direction == "incoming"
    assert [(h.name, h.value) for h in initiated.custom_headers] == [("X-VA-Call-Id", "c1")]
    assert initiated.start_time == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    hangup = parse_payload(_event("call.hangup", PAYLOADS["call.hangup"]))
    assert isinstance(hangup, CallHangup)
    assert (hangup.hangup_cause, hangup.hangup_source, hangup.sip_hangup_cause) == (
        "user_busy",
        "callee",
        "486",
    )

    amd = parse_payload(
        _event("call.machine.premium.detection.ended", PAYLOADS["call.machine.premium.detection.ended"])
    )
    assert isinstance(amd, PremiumDetectionEnded)
    assert amd.result == "human_business"

    dtmf = parse_payload(_event("call.dtmf.received", PAYLOADS["call.dtmf.received"]))
    assert isinstance(dtmf, DtmfReceived)
    assert dtmf.digit == "5"

    saved = parse_payload(_event("call.recording.saved", PAYLOADS["call.recording.saved"]))
    assert isinstance(saved, RecordingSaved)
    assert (saved.recording_urls.wav, saved.recording_urls.mp3) == ("https://rec.test/1.wav", None)
    assert saved.public_recording_urls.wav == "https://rec.test/p1.wav"
    assert saved.recording_id is None

    ended = parse_payload(_event("call.playback.ended", PAYLOADS["call.playback.ended"]))
    assert isinstance(ended, PlaybackEnded)
    assert ended.status == "cancelled"

    failed = parse_payload(_event("streaming.failed", PAYLOADS["streaming.failed"]))
    assert isinstance(failed, StreamingFailed)
    assert (failed.stream_id, failed.failure_reason, failed.stream_url) == ("s1", "connection_failed", None)

    conference = parse_payload(_event("conference.ended", PAYLOADS["conference.ended"]))
    assert isinstance(conference, ConferencePayload)
    assert (conference.conference_id, conference.call_control_id, conference.reason) == (
        "conf-1",
        None,
        "all_left",
    )


def test_missing_optional_fields_take_defaults() -> None:
    parsed = parse_payload(_event("call.hangup", {"call_control_id": CCID}))
    assert isinstance(parsed, CallHangup)
    assert (parsed.from_, parsed.hangup_cause, parsed.end_time, parsed.custom_headers) == (
        None,
        None,
        None,
        (),
    )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("call.answered", {"to": "+14155550111"}),
        ("call.dtmf.received", {"call_control_id": CCID}),
        ("conference.created", {"call_control_id": CCID}),
        ("message.sent", {"id": "m1"}),
    ],
)
def test_known_events_missing_required_fields_raise_parse_errors(
    event_type: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(WebhookParseError, match=event_type):
        parse_payload(_event(event_type, payload))


def test_unknown_event_type_keeps_the_raw_payload() -> None:
    payload = {"call_control_id": CCID, "anything": [1, 2]}
    parsed = parse_payload(_event("call.ai_gather.ended", payload))
    assert parsed == UnknownEventPayload("call.ai_gather.ended", payload)


@pytest.mark.parametrize("event_type", sorted(MESSAGING_EVENT_TYPES))
def test_message_events_parse_the_message_record(event_type: str) -> None:
    parsed = parse_payload(_event(event_type, MESSAGE))
    assert isinstance(parsed, MessageEventPayload)
    assert (parsed.id, parsed.direction, parsed.from_, parsed.text) == (
        "msg-1",
        "inbound",
        "+14155550100",
        "STOP",
    )
    assert [(r.phone_number, r.status) for r in parsed.to] == [("+14155550111", "webhook_delivered")]
    assert [(m.url, m.content_type) for m in parsed.media] == [("https://media.test/a.jpg", "image/jpeg")]
    assert [(e.code, e.title) for e in parsed.errors] == [("40008", "Undeliverable")]
    assert parsed.cost is not None
    assert (parsed.cost.amount, parsed.cost.currency) == (Decimal("0.0051"), "USD")
    assert parsed.received_at == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert (parsed.sent_at, parsed.subject, parsed.record_type) == (None, None, "message")


def test_minimal_message_record_defaults() -> None:
    parsed = parse_payload(
        _event("message.finalized", {"id": "m1", "direction": "outbound", "from": "+1415"})
    )
    assert isinstance(parsed, MessageEventPayload)
    assert (parsed.media, parsed.errors, parsed.cost, parsed.to) == ((), (), None, ())
