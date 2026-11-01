"""Acceptance (P03-T07a): the official ``stripe`` SDK works against FakeStripe unmodified, and
``stripe.Webhook.construct_event`` verifies every webhook it emits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import stripe
from stripe_helpers import API_KEY, SECRET, realtime_stripe_state, served_stripe
from telnyx_helpers import RecordingTransport

from va_sim.state import SimState

# stripe-python 16.1's HTTPXClient posts the form body with ``data=<bytes>``, which httpx deprecates; the
# warning is the SDK's, not the simulator's, so only that message is ignored.
pytestmark = pytest.mark.filterwarnings(
    r"ignore:Use 'content=<\.\.\.>' to upload raw bytes/text content\.:DeprecationWarning"
)


@asynccontextmanager
async def sdk(state: SimState) -> AsyncIterator[tuple[stripe.StripeClient, str]]:
    async with served_stripe(state) as url:
        http_client = stripe.HTTPXClient(timeout=10)
        client = stripe.StripeClient(
            API_KEY, base_addresses={"api": url}, http_client=http_client, max_network_retries=0
        )
        try:
            yield client, url
        finally:
            await http_client.close_async()  # type: ignore[no-untyped-call]


async def test_auto_recharge_flow_and_webhook_verification() -> None:
    hooks = RecordingTransport()
    state = realtime_stripe_state(hooks)
    async with sdk(state) as (client, url):
        customer = await client.v1.customers.create_async(
            {"email": "owner@acme.test", "name": "Acme", "metadata": {"org_id": "org_1"}}
        )
        method = await client.v1.payment_methods.attach_async("pm_card_visa", {"customer": customer.id})
        updated = await client.v1.customers.update_async(
            customer.id, {"invoice_settings": {"default_payment_method": method.id}}
        )
        listed = await client.v1.customers.payment_methods.list_async(customer.id, {"type": "card"})
        recharge: stripe.params.PaymentIntentCreateParams = {
            "amount": 2500,
            "currency": "usd",
            "customer": customer.id,
            "confirm": True,
            "off_session": True,
            "metadata": {"org_id": "org_1", "purpose": "auto_recharge"},
        }
        options: stripe.RequestOptions = {"idempotency_key": "ar:org_1:2026110112:1"}
        intent = await client.v1.payment_intents.create_async(recharge, options)
        replayed = await client.v1.payment_intents.create_async(recharge, options)
        fetched = await client.v1.payment_intents.retrieve_async(intent.id)
        async with httpx.AsyncClient(base_url=url) as control:
            armed = await control.patch("/sim/stripe/config", json={"decline": True})
        with pytest.raises(stripe.CardError) as declined:
            await client.v1.payment_intents.create_async(
                {
                    "amount": 2500,
                    "currency": "usd",
                    "customer": customer.id,
                    "confirm": True,
                    "off_session": True,
                }
            )
        portal = await client.v1.billing_portal.sessions.create_async(
            {"customer": customer.id, "return_url": "http://localhost:3000/billing"}
        )
        events = [
            stripe.Webhook.construct_event(r.body, r.headers["stripe-signature"], SECRET)
            for r in hooks.received
        ]
        first_event = await client.v1.events.retrieve_async(events[0].id)
    assert updated.invoice_settings is not None
    assert updated.invoice_settings.default_payment_method == method.id
    assert [m.id for m in listed.data] == [method.id]
    assert method.card is not None
    assert method.card.last4 == "4242"
    assert (intent.status, intent.amount_received, intent.payment_method) == ("succeeded", 2500, method.id)
    assert replayed.id == intent.id
    assert fetched.id == intent.id
    assert armed.status_code == 200
    assert declined.value.code == "card_declined"
    assert declined.value.http_status == 402
    assert portal.url.startswith(f"{url}/sim/stripe/portal/bps_")
    assert [event.type for event in events] == ["payment_intent.succeeded", "payment_intent.payment_failed"]
    assert events[0].data.object["id"] == intent.id
    assert first_event.id == events[0].id
    assert len(state.stripe.payment_intents) == 2


async def test_sdk_errors_map_to_the_sdk_exception_types() -> None:
    state = realtime_stripe_state(RecordingTransport())
    async with sdk(state) as (client, url):
        with pytest.raises(stripe.InvalidRequestError) as missing:
            await client.v1.customers.retrieve_async("cus_nope")
        http_client = stripe.HTTPXClient(timeout=10)
        bad_client = stripe.StripeClient(
            "sk_test_nope", base_addresses={"api": url}, http_client=http_client, max_network_retries=0
        )
        try:
            with pytest.raises(stripe.AuthenticationError):
                await bad_client.v1.customers.retrieve_async("cus_nope")
        finally:
            await http_client.close_async()  # type: ignore[no-untyped-call]
    assert missing.value.code == "resource_missing"
    assert missing.value.http_status == 404
