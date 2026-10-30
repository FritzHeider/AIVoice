"""Webhook emitter: envelope, Ed25519 signature, retries, duplicates, delivery log, reset."""

from __future__ import annotations

import pytest
from nacl.exceptions import BadSignatureError
from telnyx_helpers import (
    VOICE_URL,
    RecordingTransport,
    advance,
    telnyx_client,
    telnyx_state,
    verify_signature,
)
from telnyx_shapes import WebhookEnvelope, assert_shape

from va_sim.telnyx.webhooks import DELIVERY_LOG_SIZE

PAYLOAD = {"call_control_id": "v3:abc", "state": "answered"}


async def test_envelope_and_signature() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    advance(state, 90)
    event_id = await state.webhooks.emit(VOICE_URL, "call.answered", PAYLOAD)
    [received] = transport.received
    envelope = assert_shape(WebhookEnvelope, received.envelope)
    assert envelope.data.id == event_id
    assert (envelope.data.event_type, envelope.data.occurred_at) == ("call.answered", "2026-01-01T00:01:30Z")
    assert envelope.data.payload == PAYLOAD
    assert (envelope.meta.attempt, envelope.meta.delivered_to) == (1, VOICE_URL)
    assert received.headers["content-type"] == "application/json"
    assert received.headers["telnyx-timestamp"] == str(1_767_225_600 + 90)
    verify_signature(state.signing_key.public_key_b64, received.headers, received.body)
    with pytest.raises(BadSignatureError):
        verify_signature(
            state.signing_key.public_key_b64, received.headers, received.body.replace(b"abc", b"abd")
        )
    await state.aclose()


async def test_server_error_then_success_retries_once_after_one_second() -> None:
    transport = RecordingTransport([500, 200])
    state = telnyx_state(transport)
    payload = {"value": 1}
    event_id = await state.webhooks.emit(VOICE_URL, "message.sent", payload)
    payload["value"] = 2  # the retry re-sends the payload as it was emitted
    await state.tasks.drain()
    first, second = transport.received
    assert second.virtual_ms - first.virtual_ms == 1000
    assert [item.envelope["meta"]["attempt"] for item in (first, second)] == [1, 2]
    assert {item.envelope["data"]["id"] for item in (first, second)} == {event_id}
    assert second.envelope["data"]["payload"] == {"value": 1}
    assert second.headers["telnyx-timestamp"] == str(1_767_225_601)
    verify_signature(state.signing_key.public_key_b64, second.headers, second.body)
    assert [(d.attempt, d.status_code) for d in state.webhooks.deliveries] == [(1, 500), (2, 200)]
    await state.aclose()


async def test_persistent_failure_gives_up_after_three_attempts() -> None:
    transport = RecordingTransport([500, None, 503, 200])
    state = telnyx_state(transport)
    await state.webhooks.emit(VOICE_URL, "call.hangup", PAYLOAD)
    await state.tasks.drain()
    assert [item.virtual_ms for item in transport.received] == [0, 1000, 4000]
    deliveries = list(state.webhooks.deliveries)
    assert [(d.attempt, d.status_code) for d in deliveries] == [(1, 500), (2, None), (3, 503)]
    assert deliveries[1].error == "ConnectError: receiver unreachable"
    assert state.tasks.pending == 0
    await state.aclose()


async def test_force_duplicate_sends_successful_events_twice() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    state.telnyx.config = state.telnyx.config.model_copy(update={"force_duplicate_webhooks": True})
    await state.webhooks.emit(VOICE_URL, "call.initiated", PAYLOAD)
    first, second = transport.received
    assert first.envelope == second.envelope
    assert first.envelope["meta"]["attempt"] == 1
    await state.aclose()


async def test_delivery_log_endpoint_and_reset_cancels_retries() -> None:
    transport = RecordingTransport([500])
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        event_id = await state.webhooks.emit(VOICE_URL, "call.answered", PAYLOAD)
        log = await client.get("/sim/telnyx/webhooks")
        assert state.tasks.pending == 1
        await client.post("/sim/reset")
        await state.tasks.drain()
        after = await client.get("/sim/telnyx/webhooks")
    assert log.json() == {
        "deliveries": [
            {
                "event_id": event_id,
                "event_type": "call.answered",
                "url": VOICE_URL,
                "attempt": 1,
                "status_code": 500,
                "error": None,
                "delivered_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    assert after.json() == {"deliveries": []}
    assert len(transport.received) == 1
    assert state.webhooks.deliveries.maxlen == DELIVERY_LOG_SIZE


async def test_real_network_client_is_created_lazily_and_closed() -> None:
    state = telnyx_state()
    await state.webhooks.emit("http://127.0.0.1:9/unreachable", "call.answered", PAYLOAD)
    [delivery] = state.webhooks.deliveries
    assert delivery.status_code is None
    assert delivery.error is not None
    assert delivery.error.startswith("ConnectError")
    await state.aclose()
    assert state.tasks.pending == 0
