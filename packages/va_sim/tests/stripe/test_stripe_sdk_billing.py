"""Acceptance (P03-T07b): the official ``stripe`` SDK drives a top-up checkout and a subscription checkout to
completion, changes and cancels the subscription, and reads invoices; every lifecycle webhook verifies with
``stripe.Webhook.construct_event``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

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

SUCCESS_URL = "http://localhost:3000/billing?session={CHECKOUT_SESSION_ID}"


@asynccontextmanager
async def sdk(state: SimState) -> AsyncIterator[tuple[stripe.StripeClient, httpx.AsyncClient]]:
    async with served_stripe(state) as url:
        http_client = stripe.HTTPXClient(timeout=10)
        client = stripe.StripeClient(
            API_KEY, base_addresses={"api": url}, http_client=http_client, max_network_retries=0
        )
        try:
            async with httpx.AsyncClient(base_url=url) as browser:
                yield client, browser
        finally:
            await http_client.close_async()  # type: ignore[no-untyped-call]


async def complete(browser: httpx.AsyncClient, url: str | None) -> str:
    """Pay on the hosted page; returns the session id the redirect carries."""
    assert url is not None
    response = await browser.get(url, params={"outcome": "success"})
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["session"][0]


async def test_topup_and_subscription_lifecycle() -> None:
    hooks = RecordingTransport()
    state = realtime_stripe_state(hooks)
    async with sdk(state) as (client, browser):
        customer = await client.v1.customers.create_async(
            {"email": "owner@acme.test", "metadata": {"org_id": "o1"}}
        )
        topup = await client.v1.checkout.sessions.create_async(
            {
                "mode": "payment",
                "customer": customer.id,
                "success_url": SUCCESS_URL,
                "cancel_url": "http://localhost:3000/billing",
                "line_items": [
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": 2500,
                            "product_data": {"name": "VoiceAgents credits"},
                        },
                        "quantity": 1,
                    }
                ],
                "payment_intent_data": {"setup_future_usage": "off_session"},
                "metadata": {"org_id": "o1", "purpose": "topup", "amount_micros": "25000000"},
            }
        )
        assert await complete(browser, topup.url) == topup.id
        paid = await client.v1.checkout.sessions.retrieve_async(topup.id)
        assert paid.payment_intent is not None
        intent = await client.v1.payment_intents.retrieve_async(str(paid.payment_intent))
        line_items = await client.v1.checkout.sessions.line_items.list_async(topup.id)

        product = await client.v1.products.create_async({"name": "Build"})
        build = await client.v1.prices.create_async(
            {
                "currency": "usd",
                "unit_amount": 9900,
                "product": product.id,
                "recurring": {"interval": "month"},
            }
        )
        scale = await client.v1.prices.create_async(
            {
                "currency": "usd",
                "unit_amount": 29900,
                "product_data": {"name": "Scale"},
                "recurring": {"interval": "month"},
                "lookup_key": "scale_monthly",
            }
        )
        by_key = await client.v1.prices.list_async({"lookup_keys": ["scale_monthly"]})
        session = await client.v1.checkout.sessions.create_async(
            {
                "mode": "subscription",
                "customer": customer.id,
                "success_url": SUCCESS_URL,
                "line_items": [{"price": build.id, "quantity": 1}],
                "subscription_data": {"metadata": {"org_id": "o1", "plan_id": "build"}},
                "metadata": {"org_id": "o1", "purpose": "subscription"},
            }
        )
        await complete(browser, session.url)
        done = await client.v1.checkout.sessions.retrieve_async(session.id)
        assert done.subscription is not None
        sub = await client.v1.subscriptions.retrieve_async(str(done.subscription))
        changed = await client.v1.subscriptions.update_async(
            sub.id,
            {
                "items": [{"id": sub["items"].data[0].id, "price": scale.id}],
                "proration_behavior": "create_prorations",
            },
        )
        renewal = await browser.post(f"/sim/stripe/subscriptions/{sub.id}/renew", json={})
        invoices = await client.v1.invoices.list_async({"subscription": sub.id})
        latest = await client.v1.invoices.retrieve_async(str(invoices.data[0].id))
        flagged = await client.v1.subscriptions.update_async(sub.id, {"cancel_at_period_end": True})
        canceled = await client.v1.subscriptions.cancel_async(
            sub.id, {"cancellation_details": {"comment": "moving on"}}
        )
        listed = await client.v1.subscriptions.list_async({"customer": customer.id, "status": "all"})
        with pytest.raises(stripe.InvalidRequestError):
            await client.v1.subscriptions.cancel_async(sub.id)
    assert (intent.status, intent.amount, intent.customer) == ("succeeded", 2500, customer.id)
    assert line_items.data[0].description == "VoiceAgents credits"
    assert [price.id for price in by_key.data] == [scale.id]
    assert (sub.status, sub.metadata.to_dict()) == ("active", {"org_id": "o1", "plan_id": "build"})
    assert changed["items"].data[0].price.id == scale.id
    assert renewal.status_code == 200
    assert latest.billing_reason == "subscription_cycle"
    assert latest.status == "paid"
    assert latest.total > 29900
    assert len(invoices.data) == 2
    assert flagged.cancel_at_period_end is True
    assert canceled.status == "canceled"
    assert canceled.cancellation_details is not None
    assert canceled.cancellation_details.comment == "moving on"
    assert [s.id for s in listed.data] == [sub.id]
    events = [
        stripe.Webhook.construct_event(r.body, r.headers["stripe-signature"], SECRET) for r in hooks.received
    ]
    types = [event.type for event in events]
    assert types == [
        "payment_intent.succeeded",
        "checkout.session.completed",
        "customer.subscription.created",
        "invoice.paid",
        "checkout.session.completed",
        "customer.subscription.updated",
        "invoice.paid",
        "customer.subscription.updated",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ]
    completed = [event for event in events if event.type == "checkout.session.completed"]
    assert completed[0].data.object["metadata"]["purpose"] == "topup"
    assert completed[1].data.object["subscription"] == sub.id
