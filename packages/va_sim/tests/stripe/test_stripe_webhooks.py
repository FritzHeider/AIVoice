"""FakeStripe events and signed webhooks (specs/30 §4.1; Decision P03-T07a rule 10)."""

from __future__ import annotations

import httpx
from stripe_helpers import SECRET, WEBHOOK_URL, events_of, post, signature_ok, stripe_client, stripe_state
from telnyx_helpers import RecordingTransport

from va_sim.state import SimState
from va_sim.stripe.objects import DEFAULT_API_VERSION


async def charge(client: httpx.AsyncClient, **headers: str) -> httpx.Response:
    params = {"amount": 2000, "currency": "usd", "payment_method": "pm_card_visa", "confirm": True}
    return await post(client, "/v1/payment_intents", params, **headers)


async def test_webhooks_are_signed_with_the_sim_clock_time() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        await charge(client, stripe_version="2025-01-27.acacia")
    [received] = hooks.received
    assert received.url == WEBHOOK_URL
    assert received.headers["content-type"] == "application/json; charset=utf-8"
    valid, timestamp = signature_ok(received.body, received.headers["stripe-signature"])
    assert valid
    assert timestamp == int(state.clock.now().timestamp())
    [event] = events_of(hooks)
    assert event["id"].startswith("evt_")
    assert (event["object"], event["livemode"], event["pending_webhooks"]) == ("event", False, 1)
    assert event["api_version"] == "2025-01-27.acacia"
    assert "previous_attributes" not in event["data"]


async def test_failed_deliveries_are_retried_after_1_and_3_seconds_then_dropped() -> None:
    hooks = RecordingTransport([500, None, 503])
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        await charge(client)
        await state.tasks.drain()
    assert [item.virtual_ms for item in hooks.received] == [0, 1000, 4000]
    assert len({item.body for item in hooks.received}) == 1
    timestamps = [signature_ok(item.body, item.headers["stripe-signature"])[1] for item in hooks.received]
    assert timestamps == [timestamps[0], timestamps[0] + 1, timestamps[0] + 4]
    deliveries = list(state.stripe_webhooks.deliveries)
    assert [(d.attempt, d.status_code) for d in deliveries] == [(1, 500), (2, None), (3, 503)]
    assert deliveries[1].error is not None


async def test_a_retry_that_succeeds_stops_retrying() -> None:
    hooks = RecordingTransport([500, 200])
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        await charge(client)
        await state.tasks.drain()
    assert [d.status_code for d in state.stripe_webhooks.deliveries] == [500, 200]


async def test_null_webhook_url_disables_delivery_but_events_are_logged() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        await client.patch("/sim/stripe/config", json={"webhook_url": None})
        response = await charge(client)
        listed = await client.get("/v1/events")
    assert response.status_code == 200
    assert hooks.received == []
    assert [e["type"] for e in listed.json()["data"]] == ["payment_intent.succeeded"]


async def test_a_custom_secret_and_url_are_used() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        await client.patch(
            "/sim/stripe/config", json={"webhook_secret": "whsec_custom", "webhook_url": "http://other/hook"}
        )
        await charge(client)
    [received] = hooks.received
    assert received.url == "http://other/hook"
    assert signature_ok(received.body, received.headers["stripe-signature"], "whsec_custom")[0]
    assert not signature_ok(received.body, received.headers["stripe-signature"], SECRET)[0]


async def test_fire_an_arbitrary_event_through_the_control_api() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        fired = await client.post(
            "/sim/stripe/events",
            json={
                "type": "charge.refunded",
                "object": {"id": "ch_1", "object": "charge", "amount_refunded": 500},
            },
        )
        fetched = await client.get(f"/v1/events/{fired.json()['id']}")
    assert fired.status_code == 201
    event = fired.json()
    assert event["type"] == "charge.refunded"
    assert event["data"]["object"]["amount_refunded"] == 500
    assert event["request"] == {"id": None, "idempotency_key": None}
    assert event["api_version"] == DEFAULT_API_VERSION
    assert fetched.json() == event
    assert events_of(hooks) == [event]


async def test_events_snapshot_their_object() -> None:
    state: SimState = stripe_state()
    async with stripe_client(state) as client:
        intent = (await charge(client)).json()
        state.stripe.payment_intents[intent["id"]]["description"] = "changed later"
        [event] = (await client.get("/v1/events")).json()["data"]
    assert event["data"]["object"]["description"] is None


async def test_list_and_retrieve_events() -> None:
    async with stripe_client(stripe_state()) as client:
        await charge(client)
        await client.patch("/sim/stripe/config", json={"decline": True})
        await charge(client)
        everything = await client.get("/v1/events")
        failed = await client.get("/v1/events", params={"type": "payment_intent.payment_failed"})
        limited = await client.get("/v1/events", params={"limit": 1})
        missing = await client.get("/v1/events/evt_nope")
        unknown = await client.get("/v1/events", params={"created": "1"})
    assert [e["type"] for e in everything.json()["data"]] == [
        "payment_intent.payment_failed",
        "payment_intent.succeeded",
    ]
    assert [e["type"] for e in failed.json()["data"]] == ["payment_intent.payment_failed"]
    assert limited.json()["has_more"] is True
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "No such event: 'evt_nope'"
    assert unknown.status_code == 400
