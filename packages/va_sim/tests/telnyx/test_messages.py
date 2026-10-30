"""FakeTelnyx messaging: segments, outbound lifecycle events, webhook targets, inbound injection."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from telnyx_helpers import (
    MESSAGING_URL,
    PROFILE_URL,
    RecordingTransport,
    create_messaging_profile,
    error_of,
    own_number,
    telnyx_client,
    telnyx_state,
    verify_signature,
)
from telnyx_shapes import Message, WebhookEnvelope, assert_shape

from va_sim.state import SimState
from va_sim.telnyx.messages import MessageParts, segment_count

AGENT = "+14155552000"
USER = "+14155550123"
MESSAGES = "/v2/messages"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", MessageParts("GSM-7", 1)),
        ("a" * 160, MessageParts("GSM-7", 1)),
        ("a" * 161, MessageParts("GSM-7", 2)),
        ("a" * 306, MessageParts("GSM-7", 2)),
        ("a" * 307, MessageParts("GSM-7", 3)),
        ("€" * 80, MessageParts("GSM-7", 1)),
        ("a" + "€" * 80, MessageParts("GSM-7", 2)),
        ("é" * 160, MessageParts("GSM-7", 1)),
        ("ж" * 70, MessageParts("UCS-2", 1)),
        ("ж" * 71, MessageParts("UCS-2", 2)),
        ("ж" * 134, MessageParts("UCS-2", 2)),
        ("😀" * 35, MessageParts("UCS-2", 1)),
        ("😀" * 36, MessageParts("UCS-2", 2)),
    ],
)
def test_segment_count(text: str, expected: MessageParts) -> None:
    assert segment_count(text) == expected


async def _messaging_setup(
    client: httpx.AsyncClient, state: SimState, **profile_fields: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = await create_messaging_profile(client, **profile_fields)
    number = await own_number(client, state, AGENT, messaging_profile_id=profile["id"])
    return profile, number


async def test_outbound_message_lifecycle() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        profile, _ = await _messaging_setup(client, state, webhook_url=PROFILE_URL)
        sent_at_ms = state.clock.virtual_ms()
        response = await client.post(
            MESSAGES, json={"from": AGENT, "to": USER, "text": "Your table is ready."}
        )
        message = response.json()["data"]
        await state.tasks.drain()
        fetched = await client.get(f"{MESSAGES}/{message['id']}")
        listed = await client.get("/sim/telnyx/messages")
        missing = await client.get(f"{MESSAGES}/nope")
    assert response.status_code == 200
    queued = assert_shape(Message, message)
    assert (queued.direction, queued.type, queued.encoding, queued.parts) == ("outbound", "SMS", "GSM-7", 1)
    assert queued.to[0].status == "queued"
    assert (queued.from_.phone_number, queued.to[0].phone_number) == (AGENT, USER)
    assert queued.messaging_profile_id == profile["id"]
    assert (queued.sent_at, queued.completed_at) == (None, None)
    events = [item for item in transport.received if item.event_type.startswith("message.")]
    assert [item.event_type for item in events] == ["message.sent", "message.finalized"]
    assert [item.virtual_ms - sent_at_ms for item in events] == [200, 600]
    assert {item.url for item in events} == {PROFILE_URL}
    for item in events:
        verify_signature(state.signing_key.public_key_b64, item.headers, item.body)
    sent = assert_shape(Message, assert_shape(WebhookEnvelope, events[0].envelope).data.payload)
    finalized = assert_shape(Message, events[1].envelope["data"]["payload"])
    assert (sent.to[0].status, sent.completed_at) == ("sent", None)
    assert sent.sent_at is not None
    assert (finalized.to[0].status, finalized.errors) == ("delivered", [])
    assert finalized.completed_at is not None
    assert fetched.json()["data"]["to"][0]["status"] == "delivered"
    assert [item["id"] for item in listed.json()["messages"]] == [message["id"]]
    assert error_of(missing) == (404, "10005", None)


async def test_failing_destination_ends_in_delivery_failed() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        await _messaging_setup(client, state)
        await client.patch("/sim/telnyx/config", json={"failing_message_destinations": [USER]})
        response = await client.post(
            MESSAGES, json={"from": AGENT, "to": USER, "media_urls": ["http://cdn:9000/a.png"]}
        )
        await state.tasks.drain()
    assert response.json()["data"]["type"] == "MMS"
    assert response.json()["data"]["media"] == [
        {"url": "http://cdn:9000/a.png", "content_type": None, "sha256": None, "size": None}
    ]
    [finalized] = transport.of_type("message.finalized")
    payload = finalized.envelope["data"]["payload"]
    assert payload["to"][0]["status"] == "delivery_failed"
    assert payload["errors"] == [
        {"code": "40008", "title": "Undeliverable", "detail": "The destination carrier rejected the message."}
    ]
    assert finalized.url == MESSAGING_URL


@pytest.mark.parametrize(
    ("profile_url", "body_fields", "expected"),
    [
        (PROFILE_URL, {}, PROFILE_URL),
        (PROFILE_URL, {"webhook_url": "http://edge:8100/req"}, PROFILE_URL),
        (
            PROFILE_URL,
            {"webhook_url": "http://edge:8100/req", "use_profile_webhooks": False},
            "http://edge:8100/req",
        ),
        (None, {"webhook_url": "http://edge:8100/req"}, "http://edge:8100/req"),
        (None, {}, MESSAGING_URL),
        (PROFILE_URL, {"use_profile_webhooks": False}, MESSAGING_URL),
    ],
)
async def test_webhook_url_precedence(
    profile_url: str | None, body_fields: dict[str, Any], expected: str
) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        await _messaging_setup(client, state, webhook_url=profile_url)
        await client.post(MESSAGES, json={"from": AGENT, "to": USER, "text": "hi", **body_fields})
        await state.tasks.drain()
    assert {item.url for item in transport.of_type("message.sent")} == {expected}


async def test_invalid_messages() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await own_number(client, state, AGENT)
        no_body = await client.post(MESSAGES, json={"from": AGENT, "to": USER})
        not_owned = await client.post(MESSAGES, json={"from": "+12125559999", "to": USER, "text": "x"})
        no_profile = await client.post(MESSAGES, json={"from": AGENT, "to": USER, "text": "x"})
        unknown_profile = await client.post(
            MESSAGES, json={"from": AGENT, "to": USER, "text": "x", "messaging_profile_id": "nope"}
        )
        bad_to = await client.post(MESSAGES, json={"from": AGENT, "to": "12", "text": "x"})
        typo = await client.post(MESSAGES, json={"from": AGENT, "to": USER, "body": "x"})
    assert error_of(no_body) == (422, "10015", "/text")
    assert error_of(not_owned) == (422, "40305", "/from")
    assert not_owned.json()["errors"][0]["title"] == "Invalid 'from' address"
    assert error_of(no_profile) == (422, "10015", "/messaging_profile_id")
    assert error_of(unknown_profile) == (422, "10015", "/messaging_profile_id")
    assert error_of(bad_to) == (422, "10015", "/to")
    assert error_of(typo) == (422, "10015", "/body")


async def test_explicit_profile_on_the_request_wins_and_unicode_is_ucs2() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await own_number(client, state, AGENT)
        profile = await create_messaging_profile(client)
        response = await client.post(
            MESSAGES,
            json={"from": AGENT, "to": USER, "text": "Привет", "messaging_profile_id": profile["id"]},
        )
    data = response.json()["data"]
    assert (data["messaging_profile_id"], data["encoding"], data["parts"]) == (profile["id"], "UCS-2", 1)


async def test_inbound_message_is_delivered_to_the_profile_url() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        profile, _ = await _messaging_setup(client, state, webhook_url=PROFILE_URL)
        response = await client.post(
            "/sim/telnyx/messages/inbound", json={"from": USER, "to": AGENT, "text": "STOP"}
        )
        unknown = await client.post("/sim/telnyx/messages/inbound", json={"from": USER, "to": "+12125552000"})
        invalid = await client.post("/sim/telnyx/messages/inbound", json={"from": USER})
    assert response.status_code == 202
    [received] = transport.of_type("message.received")
    assert received.url == PROFILE_URL
    verify_signature(state.signing_key.public_key_b64, received.headers, received.body)
    message = assert_shape(Message, received.envelope["data"]["payload"])
    assert message.id == response.json()["id"]
    assert (message.direction, message.text, message.messaging_profile_id) == (
        "inbound",
        "STOP",
        profile["id"],
    )
    assert (message.from_.phone_number, message.to[0].phone_number) == (USER, AGENT)
    assert message.to[0].status == "webhook_delivered"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["issues"][0]["path"] == "to"
    assert invalid.status_code == 422


async def test_inbound_without_profile_url_uses_the_default() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        await own_number(client, state, AGENT)
        await client.post("/sim/telnyx/messages/inbound", json={"from": USER, "to": AGENT, "text": "hi"})
    [received] = transport.of_type("message.received")
    assert received.url == MESSAGING_URL
    assert received.envelope["data"]["payload"]["messaging_profile_id"] is None
