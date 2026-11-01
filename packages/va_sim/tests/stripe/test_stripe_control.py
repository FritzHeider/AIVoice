"""FakeStripe control API, reset, and snapshot (Decision P03-T07a rule 11)."""

from __future__ import annotations

import pytest
from stripe_helpers import WEBHOOK_URL, post, stripe_client, stripe_state
from telnyx_helpers import RecordingTransport

from va_sim.state import SimState
from va_sim.stripe.store import EVENT_LOG_SIZE, StripeStore

DEFAULT_CONFIG = {
    "decline": False,
    "decline_code": "generic_decline",
    "webhook_url": WEBHOOK_URL,
    "webhook_secret": "whsec_sim",
}


async def test_config_read_and_patch() -> None:
    async with stripe_client(stripe_state()) as client:
        initial = await client.get("/sim/stripe/config")
        patched = await client.patch(
            "/sim/stripe/config", json={"decline": True, "decline_code": "lost_card"}
        )
        null_decline = await client.patch("/sim/stripe/config", json={"decline": None})
    assert initial.json() == DEFAULT_CONFIG
    assert patched.json() == DEFAULT_CONFIG | {"decline": True, "decline_code": "lost_card"}
    assert null_decline.json()["decline"] is True


@pytest.mark.parametrize(
    "body", [{"unknown": 1}, {"decline": "maybe"}, {"webhook_secret": ""}, {"decline_code": ""}]
)
async def test_invalid_config_patches_are_422(body: dict[str, object]) -> None:
    async with stripe_client(stripe_state()) as client:
        response = await client.patch("/sim/stripe/config", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [{"api_key": "pk_test_x"}, {"api_key": "sk_ x"}, {}])
async def test_api_keys_must_be_secret_keys(body: dict[str, object]) -> None:
    async with stripe_client(stripe_state()) as client:
        response = await client.post("/sim/stripe/api-keys", json=body)
    assert response.status_code == 422


async def test_event_and_delivery_logs() -> None:
    hooks = RecordingTransport([500])
    async with stripe_client(stripe_state(hooks)) as client:
        fired = []
        for kind in ("invoice.paid", "charge.dispute.created"):
            response = await client.post("/sim/stripe/events", json={"type": kind, "object": {"id": "x"}})
            fired.append(response.json()["id"])
        events = await client.get("/sim/stripe/events")
        deliveries = await client.get("/sim/stripe/webhooks")
        invalid = await client.post("/sim/stripe/events", json={"type": "x", "object": [1]})
    assert [e["id"] for e in events.json()["events"]] == fired
    assert [(d["event_type"], d["attempt"], d["status_code"]) for d in deliveries.json()["deliveries"]] == [
        ("invoice.paid", 1, 500),
        ("charge.dispute.created", 1, 200),
    ]
    assert invalid.status_code == 422


async def test_reset_drops_everything_and_restores_the_config() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        await client.post("/sim/stripe/api-keys", json={"api_key": "sk_test_more"})
        await client.patch("/sim/stripe/config", json={"decline": True, "webhook_url": None})
        cus = (await post(client, "/v1/customers", idempotency_key="k")).json()["id"]
        await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": cus})
        await post(client, "/v1/billing_portal/sessions", {"customer": cus})
        await client.post("/sim/stripe/events", json={"type": "invoice.paid", "object": {}})
        before = (await client.get("/sim/state")).json()["stripe"]
        reset = await client.post("/sim/reset")
        after = (await client.get("/sim/state")).json()["stripe"]
        config = await client.get("/sim/stripe/config")
    assert before == {
        "customers": 1,
        "payment_methods": 1,
        "payment_intents": 0,
        "events": 1,
        "webhook_deliveries": 0,
    }
    assert reset.status_code == 204
    assert after == dict.fromkeys(before, 0)
    assert config.json() == DEFAULT_CONFIG
    assert not state.stripe.keys
    assert not state.stripe.idempotency
    assert not state.stripe.portal_sessions


def test_the_event_log_is_bounded() -> None:
    store = StripeStore()
    for index in range(EVENT_LOG_SIZE + 2):
        store.record_event({"id": f"evt_{index}", "type": "x"})
    assert len(store.events) == EVENT_LOG_SIZE
    assert next(iter(store.events)) == "evt_2"


def test_defaults_without_settings() -> None:
    state = SimState(clock=stripe_state().clock, latencies=stripe_state().latencies)
    assert state.stripe.config.webhook_url == "http://localhost:8000/v1/billing/stripe/webhook"
