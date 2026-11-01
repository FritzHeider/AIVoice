"""FakeStripe checkout sessions and the hosted page (specs/30 §4.1 Decision P03-T07b rules 3, 4)."""

from __future__ import annotations

from typing import Any

import pytest
from stripe_helpers import (
    checkout,
    event_types,
    events_of,
    monthly_price,
    ok,
    post,
    stripe_client,
    stripe_state,
    visit,
)
from telnyx_helpers import RecordingTransport


def topup(**extra: Any) -> dict[str, Any]:
    """The specs/24 §7 top-up session parameters."""
    return {
        "mode": "payment",
        "cancel_url": "http://localhost:3000/billing?canceled=1",
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
        "payment_intent_data": {"setup_future_usage": "off_session", "metadata": {"org_id": "org_1"}},
        "metadata": {"org_id": "org_1", "purpose": "topup", "amount_micros": "25000000"},
        **extra,
    }


async def test_topup_checkout_completes_with_a_succeeded_payment_intent() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        cus = ok(await post(client, "/v1/customers", {"email": "owner@acme.test"}))["id"]
        session = await checkout(client, **topup(customer=cus, client_reference_id="org_1"))
        page = await visit(client, session)
        done = await visit(client, session, outcome="success")
        completed = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
        intent = ok(await client.get(f"/v1/payment_intents/{completed['payment_intent']}"))
        customer = ok(await client.get(f"/v1/customers/{cus}"))
        methods = ok(await client.get(f"/v1/customers/{cus}/payment_methods"))
        again = await visit(client, session, outcome="success")
    assert session["id"].startswith("cs_test_")
    assert len(session["id"]) == len("cs_test_") + 58
    assert session["url"] == f"http://sim:9400/sim/stripe/checkout/{session['id']}"
    assert (session["status"], session["payment_status"], session["mode"]) == ("open", "unpaid", "payment")
    assert (session["amount_total"], session["amount_subtotal"], session["currency"]) == (2500, 2500, "usd")
    assert session["expires_at"] == session["created"] + 86_400
    assert (session["payment_intent"], session["customer_details"]) == (None, None)
    assert page.status_code == 200
    assert "?outcome=success" in page.text
    assert done.status_code == 303
    assert done.headers["location"] == f"http://localhost:3000/billing?session={session['id']}"
    assert (completed["status"], completed["payment_status"], completed["url"]) == ("complete", "paid", None)
    assert completed["customer"] == cus
    assert completed["customer_details"]["email"] == "owner@acme.test"
    assert (intent["status"], intent["amount"], intent["amount_received"]) == ("succeeded", 2500, 2500)
    assert (intent["customer"], intent["setup_future_usage"]) == (cus, "off_session")
    assert intent["metadata"] == {"org_id": "org_1"}
    assert intent["latest_charge"].startswith("ch_")
    assert customer["invoice_settings"]["default_payment_method"] is None
    assert [m["id"] for m in methods["data"]] == [intent["payment_method"]]
    assert event_types(hooks) == ["payment_intent.succeeded", "checkout.session.completed"]
    event = events_of(hooks, "checkout.session.completed")[0]
    assert event["data"]["object"]["metadata"] == {
        "org_id": "org_1",
        "purpose": "topup",
        "amount_micros": "25000000",
    }
    assert event["data"]["object"]["client_reference_id"] == "org_1"
    assert event["request"] == {"id": None, "idempotency_key": None}
    assert again.status_code == 409


async def test_customer_email_makes_a_customer_and_unsaved_cards_stay_detached() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        session = await checkout(client, **topup(customer_email="new@acme.test", payment_intent_data={}))
        await visit(client, session, outcome="success", card="pm_card_authenticationRequired")
        completed = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
        customer = ok(await client.get(f"/v1/customers/{completed['customer']}"))
        intent = ok(await client.get(f"/v1/payment_intents/{completed['payment_intent']}"))
        method = ok(await client.get(f"/v1/payment_methods/{intent['payment_method']}"))
    assert customer["email"] == "new@acme.test"
    assert completed["customer_email"] == "new@acme.test"
    assert intent["setup_future_usage"] is None
    assert method["customer"] is None
    assert method["card"]["last4"] == "3184"


async def test_cancel_expires_the_session() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        session = await checkout(client, **topup())
        cancel = await visit(client, session, outcome="cancel")
        expired = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
    assert cancel.status_code == 303
    assert cancel.headers["location"] == "http://localhost:3000/billing?canceled=1"
    assert (expired["status"], expired["url"]) == ("expired", None)
    assert event_types(hooks) == ["checkout.session.expired"]


async def test_cancel_without_a_cancel_url_answers_a_page() -> None:
    async with stripe_client(stripe_state()) as client:
        params = topup()
        del params["cancel_url"]
        session = await checkout(client, **params)
        response = await visit(client, session, outcome="cancel")
    assert response.status_code == 200
    assert "Checkout canceled" in response.text


async def test_hosted_page_errors() -> None:
    async with stripe_client(stripe_state()) as client:
        session = await checkout(client, **topup())
        unknown = await client.get("/sim/stripe/checkout/cs_test_nope")
        outcome = await visit(client, session, outcome="maybe")
        card = await visit(client, session, outcome="success", card="pm_card_amex")
        still_open = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
    assert (outcome.status_code, outcome.json()["error"]["code"]) == (400, "invalid_request")
    assert card.status_code == 400
    assert still_open["status"] == "open"


@pytest.mark.parametrize(
    ("card", "armed", "code"),
    [
        ("pm_card_chargeDeclinedInsufficientFunds", False, "insufficient_funds"),
        ("pm_card_visa", True, "generic_decline"),
    ],
)
async def test_declines_leave_the_session_open(card: str, armed: bool, code: str) -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        if armed:
            await client.patch("/sim/stripe/config", json={"decline": True})
        session = await checkout(client, **topup())
        response = await visit(client, session, outcome="success", card=card)
        reopened = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
    assert response.status_code == 402
    assert code in response.text
    assert reopened["status"] == "open"
    assert events_of(hooks) == []
    assert not state.stripe.payment_intents
    assert not state.stripe.customers


async def test_setup_mode_saves_a_card() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        cus = ok(await post(client, "/v1/customers"))["id"]
        session = await checkout(client, mode="setup", currency="usd", customer=cus)
        await visit(client, session, outcome="success", card="pm_card_chargeDeclined")
        completed = ok(await client.get(f"/v1/checkout/sessions/{session['id']}"))
        intent = ok(await client.get(f"/v1/setup_intents/{completed['setup_intent']}"))
        method = ok(await client.get(f"/v1/payment_methods/{intent['payment_method']}"))
        missing = await client.get("/v1/setup_intents/seti_nope")
    assert (session["amount_total"], session["currency"]) == (None, "usd")
    assert (completed["status"], completed["payment_status"]) == ("complete", "no_payment_required")
    assert intent["id"].startswith("seti_")
    assert (intent["object"], intent["status"], intent["usage"], intent["customer"]) == (
        "setup_intent",
        "succeeded",
        "off_session",
        cus,
    )
    assert method["customer"] == cus
    assert missing.status_code == 404
    assert event_types(hooks) == ["setup_intent.succeeded", "checkout.session.completed"]


async def test_line_items_are_listed_with_full_prices() -> None:
    async with stripe_client(stripe_state()) as client:
        plan = await monthly_price(client, 9900)
        addon = await monthly_price(client, 500, interval=None, name="Number")
        session = await checkout(
            client,
            mode="subscription",
            line_items=[{"price": plan["id"]}, {"price": addon["id"], "quantity": 2}],
        )
        items = ok(await client.get(f"/v1/checkout/sessions/{session['id']}/line_items"))
        page = ok(await client.get(f"/v1/checkout/sessions/{session['id']}/line_items", params={"limit": 1}))
        missing = await client.get("/v1/checkout/sessions/cs_test_nope/line_items")
    assert session["amount_total"] == 10900
    first, second = items["data"]
    assert first["id"].startswith("li_")
    assert (first["object"], first["price"], first["quantity"], first["description"]) == (
        "item",
        plan,
        1,
        "Build plan",
    )
    assert (second["amount_total"], second["description"], second["currency"]) == (1000, "Number", "usd")
    assert items["url"] == f"/v1/checkout/sessions/{session['id']}/line_items"
    assert page["has_more"] is True
    assert missing.status_code == 404


async def test_expire_api() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        session = await checkout(client, **topup())
        expired = ok(await post(client, f"/v1/checkout/sessions/{session['id']}/expire"))
        twice = await post(client, f"/v1/checkout/sessions/{session['id']}/expire")
        page = await visit(client, session)
        unknown = await client.get("/v1/checkout/sessions/cs_test_nope")
    assert (expired["status"], expired["url"]) == ("expired", None)
    assert twice.status_code == 400
    assert page.status_code == 409
    assert unknown.status_code == 404
    event = events_of(hooks)[0]
    assert event["type"] == "checkout.session.expired"
    assert event["request"]["id"] is not None


async def test_expires_at_and_options() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        now = int(state.clock.now().timestamp())
        session = await checkout(
            client, **topup(expires_at=now + 3600, payment_method_types=["card"], client_reference_id="c")
        )
    assert session["expires_at"] == now + 3600
    assert session["payment_method_types"] == ["card"]


INLINE = {"price_data": {"currency": "usd", "unit_amount": 100, "product_data": {"name": "x"}}}
RECURRING = {
    "price_data": {
        "currency": "usd",
        "unit_amount": 100,
        "product_data": {"name": "x"},
        "recurring": {"interval": "month"},
    }
}


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"line_items": [INLINE]}, "mode"),
        ({"mode": "rent", "line_items": [INLINE]}, "mode"),
        ({"mode": "payment"}, "line_items"),
        ({"mode": "payment", "line_items": [RECURRING]}, "line_items[0][price_data]"),
        ({"mode": "subscription", "line_items": [INLINE]}, "line_items"),
        ({"mode": "setup", "line_items": [INLINE], "currency": "usd"}, "line_items"),
        ({"mode": "setup"}, "currency"),
        ({"mode": "payment", "line_items": [{"quantity": 1}]}, "line_items[0][price]"),
        ({"mode": "payment", "line_items": [{"price": "price_nope"}]}, "line_items[0][price]"),
        ({"mode": "payment", "line_items": [{**INLINE, "quantity": 0}]}, "line_items[0][quantity]"),
        ({"mode": "payment", "line_items": [{**INLINE, "tax_rates": "x"}]}, "line_items[0][tax_rates]"),
        (
            {
                "mode": "payment",
                "line_items": [INLINE, {"price_data": {**INLINE["price_data"], "currency": "eur"}}],
            },
            "line_items",
        ),
        (
            {"mode": "payment", "line_items": [INLINE], "customer": "cus_x", "customer_email": "a@b.test"},
            "customer_email",
        ),
        ({"mode": "payment", "line_items": [INLINE], "customer": "cus_nope"}, "customer"),
        (
            {"mode": "payment", "line_items": [INLINE], "subscription_data": {"description": "x"}},
            "subscription_data",
        ),
        (
            {"mode": "subscription", "line_items": [RECURRING], "payment_intent_data": {"description": "x"}},
            "payment_intent_data",
        ),
        (
            {
                "mode": "payment",
                "line_items": [INLINE],
                "payment_intent_data": {"setup_future_usage": "always"},
            },
            "payment_intent_data[setup_future_usage]",
        ),
        ({"mode": "payment", "line_items": [INLINE], "expires_at": 1}, "expires_at"),
        ({"mode": "payment", "line_items": "x"}, "line_items"),
        ({"mode": "payment", "line_items": [INLINE], "allow_promotion_codes": True}, "allow_promotion_codes"),
    ],
)
async def test_invalid_sessions_are_rejected(params: dict[str, Any], param: str) -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        response = await post(client, "/v1/checkout/sessions", {"success_url": "http://x.test/ok", **params})
    assert response.status_code in {400, 404}, response.text
    assert response.json()["error"]["param"] == param
    assert not state.stripe.checkout_sessions
    assert not state.stripe.prices


async def test_session_prices_must_be_active_and_cycles_must_match() -> None:
    async with stripe_client(stripe_state()) as client:
        inactive = ok(
            await post(
                client,
                "/v1/prices",
                {"currency": "usd", "unit_amount": 1, "product_data": {"name": "old"}, "active": False},
            )
        )
        monthly = await monthly_price(client)
        yearly = await monthly_price(client, interval="year")
        rejected = await post(
            client,
            "/v1/checkout/sessions",
            {"success_url": "http://x.test", "mode": "payment", "line_items": [{"price": inactive["id"]}]},
        )
        mixed = await post(
            client,
            "/v1/checkout/sessions",
            {
                "success_url": "http://x.test",
                "mode": "subscription",
                "line_items": [{"price": monthly["id"]}, {"price": yearly["id"]}],
            },
        )
        both = await post(
            client,
            "/v1/checkout/sessions",
            {
                "success_url": "http://x.test",
                "mode": "payment",
                "line_items": [{"price": inactive["id"], **INLINE}],
            },
        )
    assert rejected.json()["error"]["param"] == "line_items[0][price]"
    assert mixed.json()["error"]["param"] == "line_items[1][price]"
    assert both.json()["error"]["param"] == "line_items[0][price]"
