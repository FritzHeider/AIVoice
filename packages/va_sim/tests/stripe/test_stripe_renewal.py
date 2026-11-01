"""Renewal control route (specs/30 §4.1 Decision P03-T07b rule 7)."""

from __future__ import annotations

import pytest
from stripe_helpers import (
    event_types,
    events_of,
    monthly_price,
    ok,
    post,
    renew,
    stripe_client,
    stripe_state,
    subscribed_customer,
)
from telnyx_helpers import RecordingTransport

from va_sim.stripe.periods import add_interval


async def test_renewal_advances_the_period_and_pays_the_cycle_invoice() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        _cus, sub = await subscribed_customer(client, price)
        hooks.received.clear()
        body = ok(await client.post(f"/sim/stripe/subscriptions/{sub['id']}/renew"))
        second = ok(await renew(client, sub["id"]))
    old_end = sub["items"]["data"][0]["current_period_end"]
    item = body["subscription"]["items"]["data"][0]
    assert (item["current_period_start"], item["current_period_end"]) == (
        old_end,
        add_interval(old_end, "month", 1),
    )
    invoice = body["invoice"]
    assert (invoice["billing_reason"], invoice["status"], invoice["total"]) == (
        "subscription_cycle",
        "paid",
        2000,
    )
    assert (invoice["period_start"], invoice["period_end"]) == (old_end, item["current_period_end"])
    assert body["subscription"]["latest_invoice"] == invoice["id"]
    assert event_types(hooks)[:2] == ["invoice.paid", "customer.subscription.updated"]
    previous = events_of(hooks, "customer.subscription.updated")[0]["data"]["previous_attributes"]
    assert list(previous) == ["items"]
    assert previous["items"]["data"][0]["current_period_end"] == old_end
    assert events_of(hooks)[0]["request"] == {"id": None, "idempotency_key": None}
    assert second["subscription"]["items"]["data"][0]["current_period_start"] == item["current_period_end"]


async def test_failed_renewals_go_past_due_and_recover() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        _cus, sub = await subscribed_customer(client, price)
        hooks.received.clear()
        failed = ok(await renew(client, sub["id"], fail=True))
        still = ok(await renew(client, sub["id"], fail=True))
        recovered = ok(await renew(client, sub["id"]))
    assert failed["subscription"]["status"] == "past_due"
    assert (failed["invoice"]["status"], failed["invoice"]["attempt_count"]) == ("open", 1)
    assert event_types(hooks) == [
        "invoice.payment_failed",
        "customer.subscription.updated",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "invoice.paid",
        "customer.subscription.updated",
    ]
    previous = [
        event["data"]["previous_attributes"] for event in events_of(hooks, "customer.subscription.updated")
    ]
    assert previous[0]["status"] == "active"
    assert "status" not in previous[1]
    assert previous[2]["status"] == "past_due"
    assert still["subscription"]["status"] == "past_due"
    assert recovered["subscription"]["status"] == "active"


async def test_armed_declines_fail_renewals_and_free_renewals_pay() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        _cus, sub = await subscribed_customer(client, price)
        await client.patch("/sim/stripe/config", json={"decline": True})
        armed = ok(await renew(client, sub["id"]))
        await client.patch("/sim/stripe/config", json={"decline": False})
        free = await monthly_price(client, 0)
        bare = ok(await post(client, "/v1/customers"))["id"]
        free_sub = ok(
            await post(client, "/v1/subscriptions", {"customer": bare, "items": [{"price": free["id"]}]})
        )
        free_renewal = ok(await renew(client, free_sub["id"]))
    assert armed["subscription"]["status"] == "past_due"
    assert free_renewal["invoice"]["status"] == "paid"


async def test_renewing_without_a_payment_method_fails() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        cus, sub = await subscribed_customer(client, price)
        await post(client, f"/v1/customers/{cus}", {"invoice_settings": {"default_payment_method": ""}})
        renewed = ok(await renew(client, sub["id"]))
    assert renewed["subscription"]["status"] == "past_due"
    assert renewed["invoice"]["attempt_count"] == 1


@pytest.mark.parametrize(("body", "status"), [({"fail": "maybe"}, 422), ({"later": True}, 422)])
async def test_renewal_bodies_are_strict(body: dict[str, object], status: int) -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        _cus, sub = await subscribed_customer(client, price)
        response = await client.post(f"/sim/stripe/subscriptions/{sub['id']}/renew", json=body)
    assert response.status_code == status


async def test_renewal_errors() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        bare = ok(await post(client, "/v1/customers"))["id"]
        incomplete = ok(
            await post(client, "/v1/subscriptions", {"customer": bare, "items": [{"price": price["id"]}]})
        )
        conflict = await renew(client, incomplete["id"])
        unknown = await renew(client, "sub_nope")
    assert (conflict.status_code, conflict.json()["error"]["code"]) == (409, "conflict")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
