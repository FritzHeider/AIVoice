"""Contract tests: every webhook FakeTelnyx delivers verifies with ``verify_webhook`` and parses into a typed
payload model (specs/06 §4.1, §5; specs/21 §5; specs/30 §3.2).

FakeTelnyx signs and builds its envelopes from the spec on its own (``va_sim.telnyx.webhooks``), so a mismatch
here means one side drifted from the documented contract.
"""

from __future__ import annotations

from typing import Final

import pytest
from media_agent import MediaAgent
from telnyx_client_helpers import CALL_ID, leg_state, sim_clients, sms_number
from telnyx_helpers import (
    CALLEE,
    VOICE_URL,
    Received,
    RecordingTransport,
    at,
    create_app,
    register,
    settle,
    telnyx_state,
)

from va_sim.state import SimState
from va_telnyx import (
    AmdConfig,
    DialRequest,
    MessageCreate,
    MessageEventPayload,
    StreamParams,
    UnknownEventPayload,
    WebhookVerificationError,
    parse_envelope,
    parse_payload,
    verify_webhook,
)
from va_telnyx.client_state import decode
from va_telnyx.voice_events import (
    CallPayload,
    ConferencePayload,
    PremiumDetectionEnded,
    RecordingSaved,
    StreamingStarted,
)

SMS: Final = "http://edge:8100/telnyx/messaging/test"
MACHINE_WITH_BEEP: Final = (
    f'id: amd\nmatch: {{to: "{CALLEE}"}}\n'
    "answer: {after_ms: 1000, answered_by: machine, beep_at_ms: 3000}\n"
)


def _checked(state: SimState, transport: RecordingTransport) -> dict[str, list[object]]:
    """Verify and parse every delivered webhook; returns the parsed payloads by event type."""
    public_key = state.signing_key.public_key_b64
    now = state.clock.now()
    parsed: dict[str, list[object]] = {}
    assert transport.received
    for item in transport.received:
        verify_webhook(item.body, item.headers, public_key, now)
        envelope = parse_envelope(item.body)
        assert envelope.meta is not None
        assert envelope.meta.delivered_to == item.url
        payload = parse_payload(envelope.data)
        assert not isinstance(payload, UnknownEventPayload), envelope.data.event_type
        if isinstance(payload, CallPayload | ConferencePayload) and payload.client_state is not None:
            assert decode(payload.client_state).call_id == CALL_ID
        parsed.setdefault(envelope.data.event_type, []).append(payload)
    return parsed


def _tampered(item: Received) -> bytes:
    index = item.body.index(b'"event_type"')
    return item.body[:index] + b" " + item.body[index:]


def _dial(connection_id: str, cid: str, **fields: object) -> DialRequest:
    values: dict[str, object] = {
        "connection_id": connection_id,
        "to": CALLEE,
        "from_": "+14155552000",
        "client_state": leg_state(),
        "command_id": cid,
        "webhook_url": VOICE_URL,
        **fields,
    }
    return DialRequest.model_validate(values)


async def test_call_lifecycle_webhooks_verify_and_parse() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, MACHINE_WITH_BEEP)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        primary = (
            await client.dial(
                _dial(
                    app["id"],
                    "dial-1",
                    answering_machine_detection="premium",
                    answering_machine_detection_config=AmdConfig(total_analysis_time_millis=5000),
                    record="record-from-answer",
                    record_channels="dual",
                    record_format="wav",
                )
            )
        ).call_control_id
        proxy = (await client.dial(_dial(app["id"], "dial-2"))).call_control_id
        await at(state, 1.0)
        await client.playback_start(
            primary, "https://hold.test/m.wav", command_id="pb-1", client_state=leg_state()
        )
        await at(state, 4.5)
        await client.playback_stop(primary, command_id="pb-stop-1")
        conference = await client.conference_create(
            primary, f"va-{CALL_ID}", command_id="conf-c", client_state=leg_state()
        )
        await client.conference_join(
            conference.id,
            proxy,
            end_conference_on_exit=False,
            command_id="conf-j",
            client_state=leg_state("warm_proxy"),
        )
        await client.conference_leave(conference.id, proxy, command_id="conf-l")
        await settle(state)
        await client.hangup(proxy, command_id="hangup-2")
        await client.hangup(primary, command_id="hangup-1")
        await settle(state)
        parsed = _checked(state, transport)

    assert {
        "call.initiated",
        "call.answered",
        "call.machine.premium.detection.ended",
        "call.machine.premium.greeting.ended",
        "call.playback.started",
        "call.playback.ended",
        "conference.created",
        "conference.participant.joined",
        "conference.participant.left",
        "call.hangup",
        "call.recording.saved",
    } <= set(parsed)
    [detection] = parsed["call.machine.premium.detection.ended"]
    assert isinstance(detection, PremiumDetectionEnded)
    assert detection.result == "machine"
    [saved] = parsed["call.recording.saved"]
    assert isinstance(saved, RecordingSaved)
    assert saved.recording_urls.wav is not None
    assert saved.recording_urls.wav.startswith("http://")
    assert saved.channels == "dual"

    first = transport.received[0]
    with pytest.raises(WebhookVerificationError) as tampered:
        verify_webhook(_tampered(first), first.headers, state.signing_key.public_key_b64, state.clock.now())
    assert tampered.value.reason == "invalid_signature"


async def test_streaming_webhooks_verify_and_parse(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        ccid = (await client.dial(_dial(app["id"], "dial-s"))).call_control_id
        await at(state, 1.0)
        stream = StreamParams(stream_url=media_agent.url)
        await client.streaming_start(ccid, stream, command_id="stream-1", client_state=leg_state())
        await media_agent.wait_media(1)
        await client.streaming_stop(ccid, command_id="stream-stop-1", client_state=leg_state())
        await media_agent.wait_closed()
        await client.hangup(ccid, command_id="hangup-s")
        await settle(state)
        parsed = _checked(state, transport)
    [started] = parsed["streaming.started"]
    assert isinstance(started, StreamingStarted)
    assert started.stream_url == media_agent.url
    assert len(parsed["streaming.stopped"]) == 1


async def test_message_webhooks_verify_and_parse() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        number, profile_id = await sms_number(client, state, VOICE_URL, SMS)
        transport.received.clear()  # number_order.complete is not a voice or messaging event
        sent = await client.send_message(
            MessageCreate(
                from_=number,
                to=CALLEE,
                text="See you at 3.",
                messaging_profile_id=profile_id,
                webhook_url=SMS,
            )
        )
        response = await raw.post(
            "/sim/telnyx/messages/inbound", json={"to": number, "from": CALLEE, "text": "STOP"}
        )
        assert response.status_code == 202, response.text
        await state.tasks.drain()
        parsed = _checked(state, transport)

    assert {"message.sent", "message.finalized", "message.received"} <= set(parsed)
    for event_type in ("message.sent", "message.finalized"):
        for payload in parsed[event_type]:
            assert isinstance(payload, MessageEventPayload)
            assert (payload.id, payload.direction, payload.from_) == (sent.id, "outbound", number)
    [received] = parsed["message.received"]
    assert isinstance(received, MessageEventPayload)
    assert (received.direction, received.from_, received.text) == ("inbound", CALLEE, "STOP")
    assert [recipient.phone_number for recipient in received.to] == [number]
    assert received.received_at is not None
