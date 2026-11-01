"""FakeStripe payment intents (specs/30 §4.1; Decision P03-T07a rule 8)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from stripe_helpers import events_of, post, stripe_client, stripe_state
from telnyx_helpers import RecordingTransport

from va_sim.state import SimState


async def customer_with_card(client: httpx.AsyncClient, magic: str = "pm_card_visa") -> tuple[str, str]:
    cus = (await post(client, "/v1/customers")).json()["id"]
    pm = (await post(client, f"/v1/payment_methods/{magic}/attach", {"customer": cus})).json()["id"]
    await post(client, f"/v1/customers/{cus}", {"invoice_settings": {"default_payment_method": pm}})
    return cus, pm


def recharge(cus: str, **extra: Any) -> dict[str, Any]:
    return {"amount": 2000, "currency": "usd", "customer": cus, "confirm": True, "off_session": True, **extra}


async def test_unconfirmed_intents_wait_for_a_method_or_confirmation() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        cus, pm = await customer_with_card(client)
        bare = await post(client, "/v1/payment_intents", {"amount": 1000, "currency": "USD"})
        with_pm = await post(
            client,
            "/v1/payment_intents",
            {
                "amount": 1000,
                "currency": "usd",
                "customer": cus,
                "payment_method": pm,
                "metadata": {"org_id": "o1"},
                "payment_method_types": ["card"],
                "automatic_payment_methods": {"enabled": False},
                "setup_future_usage": "off_session",
                "description": "credits",
            },
        )
    intent = bare.json()
    assert intent["id"].startswith("pi_")
    assert intent["client_secret"].startswith(f"{intent['id']}_secret_")
    assert (intent["status"], intent["currency"], intent["amount"]) == (
        "requires_payment_method",
        "usd",
        1000,
    )
    detailed = with_pm.json()
    assert detailed["status"] == "requires_confirmation"
    assert (detailed["payment_method"], detailed["customer"]) == (pm, cus)
    assert detailed["metadata"] == {"org_id": "o1"}
    assert detailed["automatic_payment_methods"] == {"enabled": False}
    assert detailed["setup_future_usage"] == "off_session"
    assert events_of(hooks) == []


async def test_off_session_success_uses_the_default_method_and_emits_succeeded() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        cus, pm = await customer_with_card(client)
        response = await post(client, "/v1/payment_intents", recharge(cus, metadata={"org_id": "o1"}))
        fetched = await client.get(f"/v1/payment_intents/{response.json()['id']}")
    intent = response.json()
    assert response.status_code == 200
    assert (intent["status"], intent["amount_received"], intent["payment_method"]) == ("succeeded", 2000, pm)
    assert intent["latest_charge"].startswith("ch_")
    assert fetched.json() == intent
    [event] = events_of(hooks)
    assert event["type"] == "payment_intent.succeeded"
    assert event["data"]["object"] == intent
    assert event["request"]["id"] == response.headers["request-id"]


@pytest.mark.parametrize(
    ("magic", "decline_code", "message"),
    [
        ("pm_card_chargeDeclined", "generic_decline", "Your card was declined."),
        (
            "pm_card_chargeDeclinedInsufficientFunds",
            "insufficient_funds",
            "Your card has insufficient funds.",
        ),
    ],
)
async def test_declining_cards_answer_402(magic: str, decline_code: str, message: str) -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        cus, pm = await customer_with_card(client, magic)
        response = await post(client, "/v1/payment_intents", recharge(cus))
    assert response.status_code == 402
    error = response.json()["error"]
    assert (error["type"], error["code"], error["decline_code"], error["message"]) == (
        "card_error",
        "card_declined",
        decline_code,
        message,
    )
    assert error["payment_method"]["id"] == pm
    assert error["charge"].startswith("ch_")
    intent = error["payment_intent"]
    assert intent["status"] == "requires_payment_method"
    assert intent["last_payment_error"]["decline_code"] == decline_code
    assert state.stripe.payment_intents[intent["id"]]["status"] == "requires_payment_method"
    [event] = events_of(hooks)
    assert event["type"] == "payment_intent.payment_failed"
    assert event["data"]["object"]["id"] == intent["id"]


async def test_armed_decline_declines_every_card_with_its_code() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        cus, _ = await customer_with_card(client)
        await client.patch("/sim/stripe/config", json={"decline": True, "decline_code": "expired_card"})
        declined = await post(client, "/v1/payment_intents", recharge(cus))
        await client.patch("/sim/stripe/config", json={"decline": False})
        succeeded = await post(client, "/v1/payment_intents", recharge(cus))
    assert declined.status_code == 402
    assert declined.json()["error"]["decline_code"] == "expired_card"
    assert declined.json()["error"]["message"] == "Your card has expired."
    assert succeeded.json()["status"] == "succeeded"
    assert [e["type"] for e in events_of(hooks)] == [
        "payment_intent.payment_failed",
        "payment_intent.succeeded",
    ]


async def test_authentication_required_off_session_vs_on_session() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        cus, pm = await customer_with_card(client, "pm_card_authenticationRequired")
        off = await post(client, "/v1/payment_intents", recharge(cus))
        on = await post(
            client,
            "/v1/payment_intents",
            {"amount": 2000, "currency": "usd", "customer": cus, "payment_method": pm, "confirm": True},
        )
    assert off.status_code == 402
    assert (off.json()["error"]["code"], off.json()["error"]["decline_code"]) == (
        "authentication_required",
        "authentication_required",
    )
    assert on.status_code == 200
    assert on.json()["status"] == "requires_action"
    assert on.json()["next_action"]["type"] == "use_stripe_sdk"
    assert [e["type"] for e in events_of(hooks)] == ["payment_intent.payment_failed"]


async def test_magic_ids_work_directly_and_off_session_accepts_the_named_values() -> None:
    async with stripe_client(stripe_state()) as client:
        response = await post(
            client,
            "/v1/payment_intents",
            {
                "amount": 500,
                "currency": "usd",
                "payment_method": "pm_card_mastercard",
                "confirm": True,
                "off_session": "recurring",
            },
        )
    assert response.json()["status"] == "succeeded"
    assert response.json()["payment_method"].startswith("pm_")


@pytest.mark.parametrize(
    ("params", "status", "code", "param"),
    [
        ({"currency": "usd"}, 400, "parameter_missing", "amount"),
        ({"amount": 49, "currency": "usd"}, 400, "amount_too_small", "amount"),
        ({"amount": "ten", "currency": "usd"}, 400, "parameter_invalid_integer", "amount"),
        ({"amount": 100}, 400, "parameter_missing", "currency"),
        ({"amount": 100, "currency": "dollars"}, 400, "parameter_invalid_string", "currency"),
        ({"amount": 100, "currency": "usd", "confirm": True}, 400, "parameter_missing", "payment_method"),
        (
            {"amount": 100, "currency": "usd", "off_session": True},
            400,
            "parameter_invalid_empty",
            "off_session",
        ),
        ({"amount": 100, "currency": "usd", "customer": "cus_x"}, 404, "resource_missing", "customer"),
        (
            {"amount": 100, "currency": "usd", "payment_method": "pm_x"},
            404,
            "resource_missing",
            "payment_method",
        ),
        ({"amount": 100, "currency": "usd", "capture": "x"}, 400, "parameter_unknown", "capture"),
    ],
)
async def test_invalid_creates(params: dict[str, Any], status: int, code: str, param: str) -> None:
    async with stripe_client(stripe_state()) as client:
        response = await post(client, "/v1/payment_intents", params)
    assert response.status_code == status
    assert (response.json()["error"]["code"], response.json()["error"]["param"]) == (code, param)


async def test_other_invalid_fields() -> None:
    async with stripe_client(stripe_state()) as client:
        usage = await post(
            client, "/v1/payment_intents", {"amount": 100, "currency": "usd", "setup_future_usage": "x"}
        )
        types = await post(
            client, "/v1/payment_intents", {"amount": 100, "currency": "usd", "payment_method_types": "card"}
        )
        automatic = await post(
            client,
            "/v1/payment_intents",
            {"amount": 100, "currency": "usd", "automatic_payment_methods": {"x": "1"}},
        )
        missing = await client.get("/v1/payment_intents/pi_nope")
    assert usage.json()["error"]["param"] == "setup_future_usage"
    assert types.json()["error"]["param"] == "payment_method_types"
    assert automatic.json()["error"]["param"] == "automatic_payment_methods[x]"
    assert missing.status_code == 404


async def test_a_method_of_another_customer_is_refused() -> None:
    state: SimState = stripe_state()
    async with stripe_client(state) as client:
        _, pm = await customer_with_card(client)
        other = (await post(client, "/v1/customers")).json()["id"]
        response = await post(
            client,
            "/v1/payment_intents",
            {"amount": 100, "currency": "usd", "customer": other, "payment_method": pm},
        )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "payment_method"
