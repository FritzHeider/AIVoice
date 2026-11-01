"""FakeStripe subscriptions (specs/30 §4.1 Decision P03-T07b rules 4, 5)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from stripe_helpers import (
    checkout,
    event_types,
    events_of,
    monthly_price,
    ok,
    post,
    renew,
    stripe_client,
    stripe_state,
    subscribed_customer,
    visit,
)
from telnyx_helpers import RecordingTransport

from va_sim.state import SimState
from va_sim.stripe.periods import add_interval


def period_of(subscription: dict[str, Any]) -> tuple[int, int]:
    item = subscription["items"]["data"][0]
    return item["current_period_start"], item["current_period_end"]


async def test_subscription_checkout_starts_an_active_subscription() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        plan = await monthly_price(client, 29900)
        setup_fee = await monthly_price(client, 1000, interval=None, name="Setup fee")
        session = await checkout(
            client,
            mode="subscription",
            customer_email="owner@acme.test",
            line_items=[{"price": plan["id"]}, {"price": setup_fee["id"]}],
            subscription_data={"metadata": {"org_id": "org_1", "plan_id": "scale"}, "description": "Scale"},
            metadata={"org_id": "org_1", "purpose": "subscription"},
        )
        done = await visit(client, session, outcome="success")
        completed = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
        sub = ok(await client.get(f"/v1/subscriptions/{completed['subscription']}"))
        invoice = ok(await client.get(f"/v1/invoices/{completed['invoice']}"))
        method = ok(await client.get(f"/v1/payment_methods/{sub['default_payment_method']}"))
    assert done.status_code == 303
    assert sub["id"].startswith("sub_")
    assert (sub["object"], sub["status"], sub["customer"], sub["currency"]) == (
        "subscription",
        "active",
        completed["customer"],
        "usd",
    )
    assert (sub["metadata"], sub["description"]) == ({"org_id": "org_1", "plan_id": "scale"}, "Scale")
    assert (sub["cancel_at_period_end"], sub["cancel_at"], sub["latest_invoice"]) == (
        False,
        None,
        invoice["id"],
    )
    item = sub["items"]["data"][0]
    assert len(sub["items"]["data"]) == 1
    assert item["id"].startswith("si_")
    assert (item["object"], item["price"], item["quantity"], item["subscription"]) == (
        "subscription_item",
        plan,
        1,
        sub["id"],
    )
    start, end = period_of(sub)
    assert start == sub["start_date"] == sub["billing_cycle_anchor"] == sub["created"]
    assert end == add_interval(start, "month", 1)
    assert method["customer"] == completed["customer"]
    assert (invoice["status"], invoice["billing_reason"], invoice["total"], invoice["amount_paid"]) == (
        "paid",
        "subscription_create",
        30900,
        30900,
    )
    assert invoice["parent"]["subscription_details"] == {
        "metadata": {"org_id": "org_1", "plan_id": "scale"},
        "subscription": sub["id"],
    }
    assert [line["amount"] for line in invoice["lines"]["data"]] == [29900, 1000]
    assert invoice["lines"]["data"][1]["parent"]["subscription_item_details"]["subscription_item"] is None
    assert event_types(hooks) == [
        "customer.subscription.created",
        "invoice.paid",
        "checkout.session.completed",
    ]
    assert events_of(hooks, "checkout.session.completed")[0]["data"]["object"]["metadata"] == {
        "org_id": "org_1",
        "purpose": "subscription",
    }


async def test_api_create_charges_the_invoice_default() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        cus, sub = await subscribed_customer(client, price, metadata={"org_id": "o"}, description="d")
        invoice = ok(await client.get(f"/v1/invoices/{sub['latest_invoice']}"))
    assert sub["status"] == "active"
    assert (sub["customer"], sub["default_payment_method"], sub["metadata"]) == (cus, None, {"org_id": "o"})
    assert (invoice["status"], invoice["attempt_count"], invoice["attempted"]) == ("paid", 1, True)
    assert invoice["status_transitions"]["paid_at"] == invoice["created"]
    assert invoice["number"].endswith("-0001")
    line = invoice["lines"]["data"][0]
    assert line["id"].startswith("il_")
    assert line["description"] == "1 \N{MULTIPLICATION SIGN} Build plan (at $20.00 / month)"
    assert line["pricing"]["price_details"] == {"price": price["id"], "product": price["product"]}
    assert line["period"] == {"start": sub["start_date"], "end": period_of(sub)[1]}
    assert event_types(hooks) == ["customer.subscription.created", "invoice.paid"]


async def test_create_without_a_payment_method_or_with_default_incomplete_stays_incomplete() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        bare = ok(await post(client, "/v1/customers"))["id"]
        no_method = ok(
            await post(client, "/v1/subscriptions", {"customer": bare, "items": [{"price": price["id"]}]})
        )
        _cus, deferred = await subscribed_customer(client, price, payment_behavior="default_incomplete")
        open_invoice = ok(await client.get(f"/v1/invoices/{no_method['latest_invoice']}"))
    assert no_method["status"] == deferred["status"] == "incomplete"
    assert (open_invoice["status"], open_invoice["attempt_count"], open_invoice["amount_remaining"]) == (
        "open",
        0,
        2000,
    )
    assert event_types(hooks) == ["customer.subscription.created", "customer.subscription.created"]


async def test_declines_leave_the_subscription_incomplete_or_fail_the_request() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        price = await monthly_price(client)
        _cus, declined = await subscribed_customer(client, price, card="pm_card_chargeDeclined")
        invoice = ok(await client.get(f"/v1/invoices/{declined['latest_invoice']}"))
        _cus, error = await subscribed_customer(
            client, price, card="pm_card_chargeDeclined", payment_behavior="error_if_incomplete"
        )
        bare = ok(await post(client, "/v1/customers"))["id"]
        no_method = await post(
            client,
            "/v1/subscriptions",
            {"customer": bare, "items": [{"price": price["id"]}], "payment_behavior": "error_if_incomplete"},
        )
        _cus, authenticate = await subscribed_customer(client, price, card="pm_card_authenticationRequired")
    assert declined["status"] == "incomplete"
    assert (invoice["status"], invoice["attempt_count"]) == ("open", 1)
    assert error["error"]["type"] == "card_error"
    assert error["error"]["decline_code"] == "generic_decline"
    assert no_method.status_code == 400
    assert authenticate["status"] == "incomplete"
    assert len(state.stripe.subscriptions) == 2
    assert event_types(hooks) == [
        "customer.subscription.created",
        "invoice.payment_failed",
        "customer.subscription.created",
        "invoice.payment_failed",
    ]


async def test_free_subscriptions_are_paid_without_a_charge() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client, 0)
        bare = ok(await post(client, "/v1/customers"))["id"]
        sub = ok(
            await post(client, "/v1/subscriptions", {"customer": bare, "items": [{"price": price["id"]}]})
        )
        invoice = ok(await client.get(f"/v1/invoices/{sub['latest_invoice']}"))
    assert sub["status"] == "active"
    assert (invoice["status"], invoice["attempt_count"], invoice["amount_due"]) == ("paid", 0, 0)


async def subscribed(
    client: httpx.AsyncClient, amount: int = 2000
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(old price, new 5000 price, an active subscription to the old price) for proration tests."""
    old = await monthly_price(client, amount)
    new = await monthly_price(client, 5000, name="Scale plan")
    _cus, sub = await subscribed_customer(client, old)
    return old, new, sub


def half_period(state: SimState, sub: dict[str, Any]) -> None:
    start, end = period_of(sub)
    state.clock.advance((end - start) / 2)  # type: ignore[attr-defined]


async def change(
    client: httpx.AsyncClient, sub: dict[str, Any], price: dict[str, Any], **params: Any
) -> httpx.Response:
    item = sub["items"]["data"][0]["id"]
    return await post(
        client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": item, "price": price["id"]}], **params}
    )


async def test_create_prorations_bills_the_change_on_the_next_invoice() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        old, new, sub = await subscribed(client)
        half_period(state, sub)
        updated = ok(await change(client, sub, new, proration_behavior="create_prorations"))
        pending = list(state.stripe.pending_lines[sub["id"]])
        renewed = ok(await renew(client, sub["id"]))
    assert updated["items"]["data"][0]["price"]["id"] == new["id"]
    assert [line["amount"] for line in pending] == [-1000, 2500]
    assert pending[0]["description"] == "Unused time on 1 \N{MULTIPLICATION SIGN} Build plan"
    assert pending[1]["description"] == "Remaining time on 1 \N{MULTIPLICATION SIGN} Scale plan"
    event = events_of(hooks, "customer.subscription.updated")[0]
    assert event["data"]["previous_attributes"]["items"]["data"][0]["price"]["id"] == old["id"]
    assert list(event["data"]["previous_attributes"]) == ["items"]
    invoice = renewed["invoice"]
    assert (invoice["billing_reason"], invoice["total"], invoice["status"]) == (
        "subscription_cycle",
        6500,
        "paid",
    )
    prorations = [
        line["parent"]["subscription_item_details"]["proration"] for line in invoice["lines"]["data"]
    ]
    assert prorations == [False, True, True]
    assert sub["id"] not in state.stripe.pending_lines


async def test_proration_none_and_quantity_changes() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        _old, new, sub = await subscribed(client)
        half_period(state, sub)
        await change(client, sub, new, proration_behavior="none")
        item = sub["items"]["data"][0]["id"]
        doubled = ok(
            await post(client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": item, "quantity": 2}]})
        )
        pending = list(state.stripe.pending_lines[sub["id"]])
    assert doubled["items"]["data"][0]["quantity"] == 2
    assert [line["amount"] for line in pending] == [-2500, 5000]


async def test_always_invoice_charges_upgrades_and_credits_downgrades() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        old, new, sub = await subscribed(client)
        half_period(state, sub)
        upgraded = ok(await change(client, sub, new, proration_behavior="always_invoice"))
        upgrade_invoice = ok(await client.get(f"/v1/invoices/{upgraded['latest_invoice']}"))
        hooks.received.clear()
        downgraded = ok(await change(client, upgraded, old, proration_behavior="always_invoice"))
        credit_invoice = ok(await client.get(f"/v1/invoices/{downgraded['latest_invoice']}"))
        customer = ok(await client.get(f"/v1/customers/{sub['customer']}"))
        renewed = ok(await renew(client, sub["id"]))
    assert (upgrade_invoice["billing_reason"], upgrade_invoice["total"], upgrade_invoice["status"]) == (
        "subscription_update",
        1500,
        "paid",
    )
    assert (credit_invoice["total"], credit_invoice["amount_due"], credit_invoice["status"]) == (
        -1500,
        0,
        "paid",
    )
    assert (credit_invoice["starting_balance"], credit_invoice["ending_balance"]) == (0, -1500)
    assert customer["balance"] == -1500
    assert event_types(hooks)[:2] == ["invoice.paid", "customer.subscription.updated"]
    cycle = renewed["invoice"]
    assert (cycle["total"], cycle["starting_balance"], cycle["amount_due"], cycle["ending_balance"]) == (
        2000,
        -1500,
        500,
        0,
    )


async def test_always_invoice_declines_make_the_subscription_past_due() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        _old, new, sub = await subscribed(client)
        await client.patch("/sim/stripe/config", json={"decline": True})
        updated = ok(await change(client, sub, new, proration_behavior="always_invoice"))
    assert updated["status"] == "past_due"
    event = events_of(hooks, "customer.subscription.updated")[0]
    assert event["data"]["previous_attributes"]["status"] == "active"
    assert events_of(hooks, "invoice.payment_failed")


async def test_cancel_at_period_end_then_renewal_ends_it() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        _old, _new, sub = await subscribed(client)
        flagged = ok(await post(client, f"/v1/subscriptions/{sub['id']}", {"cancel_at_period_end": True}))
        ended = ok(await renew(client, sub["id"]))
        again = await renew(client, sub["id"])
    assert (flagged["cancel_at_period_end"], flagged["cancel_at"], flagged["status"]) == (
        True,
        period_of(sub)[1],
        "active",
    )
    assert flagged["canceled_at"] is not None
    updated = events_of(hooks, "customer.subscription.updated")[0]
    assert updated["data"]["previous_attributes"] == {"cancel_at_period_end": False, "cancel_at": None}
    assert ended["invoice"] is None
    assert (ended["subscription"]["status"], ended["subscription"]["ended_at"]) == (
        "canceled",
        period_of(sub)[1],
    )
    assert events_of(hooks)[-1]["type"] == "customer.subscription.deleted"
    assert again.status_code == 409


async def test_uncancel_and_field_updates() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        cus, sub = await subscribed_customer(client, price, cancel_at_period_end=True, metadata={"a": "1"})
        pm = ok(await post(client, "/v1/payment_methods/pm_card_mastercard/attach", {"customer": cus}))["id"]
        hooks.received.clear()
        updated = ok(
            await post(
                client,
                f"/v1/subscriptions/{sub['id']}",
                {
                    "cancel_at_period_end": False,
                    "metadata": {"a": "", "b": "2"},
                    "default_payment_method": pm,
                    "description": "renamed",
                },
            )
        )
        noop = ok(
            await post(
                client, f"/v1/subscriptions/{sub['id']}", {"metadata": {"b": "2"}, "description": "renamed"}
            )
        )
        foreign = await post(client, f"/v1/subscriptions/{sub['id']}", {"default_payment_method": "pm_nope"})
    assert sub["cancel_at"] == period_of(sub)[1]
    assert (updated["cancel_at_period_end"], updated["cancel_at"], updated["canceled_at"]) == (
        False,
        None,
        None,
    )
    assert (updated["metadata"], updated["default_payment_method"], updated["description"]) == (
        {"b": "2"},
        pm,
        "renamed",
    )
    previous = events_of(hooks, "customer.subscription.updated")[0]["data"]["previous_attributes"]
    assert previous == {
        "cancel_at_period_end": True,
        "cancel_at": sub["cancel_at"],
        "metadata": {"a": "1"},
        "default_payment_method": None,
        "description": None,
    }
    assert noop == updated
    assert len(events_of(hooks)) == 1
    assert foreign.status_code == 400


async def test_update_errors_change_nothing() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        old, _new, sub = await subscribed(client)
        yearly = await monthly_price(client, interval="year")
        one_time = await monthly_price(client, interval=None)
        item = sub["items"]["data"][0]["id"]
        interval = await post(
            client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": item, "price": yearly["id"]}]}
        )
        kind = await post(
            client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": item, "price": one_time["id"]}]}
        )
        unknown = await post(
            client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": "si_nope", "quantity": 2}]}
        )
        behaviour = await post(client, f"/v1/subscriptions/{sub['id']}", {"proration_behavior": "later"})
        partial = await post(
            client,
            f"/v1/subscriptions/{sub['id']}",
            {"metadata": {"x": "1"}, "items": [{"id": item, "price": yearly["id"]}]},
        )
        missing = await post(client, "/v1/subscriptions/sub_nope", {"metadata": {"x": "1"}})
        retired = ok(
            await post(
                client,
                "/v1/prices",
                {
                    "currency": "usd",
                    "unit_amount": 1,
                    "product_data": {"name": "old"},
                    "recurring": {"interval": "month"},
                    "active": False,
                },
            )
        )
        inactive = await post(
            client, f"/v1/subscriptions/{sub['id']}", {"items": [{"id": item, "price": retired["id"]}]}
        )
        same = ok(
            await post(
                client,
                f"/v1/subscriptions/{sub['id']}",
                {"items": [{"id": item, "price": old["id"], "quantity": 1}]},
            )
        )
        stored = ok(await client.get(f"/v1/subscriptions/{sub['id']}"))
    assert (interval.status_code, interval.json()["error"]["code"]) == (400, "parameter_invalid")
    assert kind.json()["error"]["param"] == "items[0][price]"
    assert unknown.json()["error"]["param"] == "items[0][id]"
    assert behaviour.json()["error"]["param"] == "proration_behavior"
    assert partial.status_code == 400
    assert missing.status_code == 404
    assert inactive.json()["error"]["param"] == "items[0][price]"
    assert same == sub
    assert stored == sub
    assert stored["items"]["data"][0]["price"]["id"] == old["id"]


async def test_delete_cancels_immediately() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        _old, new, sub = await subscribed(client)
        await change(client, sub, new)
        canceled = ok(
            await client.delete(
                f"/v1/subscriptions/{sub['id']}",
                params={"cancellation_details[comment]": "too pricey", "prorate": "false"},
            )
        )
        twice = await client.delete(f"/v1/subscriptions/{sub['id']}")
        update = await post(client, f"/v1/subscriptions/{sub['id']}", {"metadata": {"x": "1"}})
        renewal = await renew(client, sub["id"])
    assert canceled["status"] == "canceled"
    assert canceled["canceled_at"] == canceled["ended_at"] == canceled["start_date"]
    assert canceled["cancellation_details"] == {
        "comment": "too pricey",
        "feedback": None,
        "reason": "cancellation_requested",
    }
    assert sub["id"] not in state.stripe.pending_lines
    assert events_of(hooks)[-1]["type"] == "customer.subscription.deleted"
    assert twice.status_code == 400
    assert update.status_code == 400
    assert renewal.status_code == 409


async def test_lists_filter_by_customer_status_and_price() -> None:
    async with stripe_client(stripe_state()) as client:
        a = await monthly_price(client)
        b = await monthly_price(client, 5000)
        cus_a, sub_a = await subscribed_customer(client, a)
        _cus_b, sub_b = await subscribed_customer(client, b)
        await client.delete(f"/v1/subscriptions/{sub_b['id']}")

        async def ids(**query: str) -> list[str]:
            body = ok(await client.get("/v1/subscriptions", params=query))
            return [sub["id"] for sub in body["data"]]

        default = await ids()
        everything = await ids(status="all")
        canceled = await ids(status="canceled")
        ended = await ids(status="ended")
        by_customer = await ids(customer=cus_a, status="all")
        by_price = await ids(price=b["id"], status="all")
        bad = await client.get("/v1/subscriptions", params={"status": "sleeping"})
        missing = await client.get("/v1/subscriptions/sub_nope")
    assert default == [sub_a["id"]]
    assert everything == [sub_b["id"], sub_a["id"]]
    assert canceled == ended == [sub_b["id"]]
    assert by_customer == [sub_a["id"]]
    assert by_price == [sub_b["id"]]
    assert bad.status_code == 400
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("items", "extra", "param"),
    [
        (None, {}, "items"),
        ([{"price": "price_nope"}], {}, "items[0][price]"),
        ([{"price": "ONE_TIME"}], {}, "items[0][price]"),
        ([{"price": "INACTIVE"}], {}, "items[0][price]"),
        ([{"price": "MONTHLY"}, {"price": "YEARLY"}], {}, "items[1][price]"),
        ([{"price": "MONTHLY", "quantity": 0}], {}, "items[0][quantity]"),
        ([{"price": "MONTHLY", "plan": "x"}], {}, "items[0][plan]"),
        ([{"price": "MONTHLY"}], {"default_payment_method": "pm_nope"}, "default_payment_method"),
        ([{"price": "MONTHLY"}], {"payment_behavior": "pending_if_incomplete"}, "payment_behavior"),
        ([{"price": "MONTHLY"}], {"trial_period_days": 7}, "trial_period_days"),
    ],
)
async def test_invalid_creates_store_nothing(
    items: list[dict[str, Any]] | None, extra: dict[str, Any], param: str
) -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        names = {
            "ONE_TIME": (await monthly_price(client, interval=None))["id"],
            "INACTIVE": ok(
                await post(
                    client,
                    "/v1/prices",
                    {
                        "currency": "usd",
                        "unit_amount": 1,
                        "product_data": {"name": "x"},
                        "recurring": {"interval": "month"},
                        "active": False,
                    },
                )
            )["id"],
            "MONTHLY": (await monthly_price(client))["id"],
            "YEARLY": (await monthly_price(client, interval="year"))["id"],
        }
        cus = ok(await post(client, "/v1/customers"))["id"]
        params: dict[str, Any] = {"customer": cus, **extra}
        if items is not None:
            params["items"] = [
                {k: names.get(v, v) if k == "price" else v for k, v in i.items()} for i in items
            ]
        response = await post(client, "/v1/subscriptions", params)
        unknown_customer = await post(
            client, "/v1/subscriptions", {"customer": "cus_nope", "items": params.get("items")}
        )
    assert response.status_code in {400, 404}, response.text
    assert response.json()["error"]["param"] == param
    assert unknown_customer.status_code == 404
    assert not state.stripe.subscriptions
    assert not state.stripe.invoices
