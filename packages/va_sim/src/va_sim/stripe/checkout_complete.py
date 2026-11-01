"""Completing a checkout session from the hosted page (specs/30 §4.1 Decision P03-T07b rule 4), and
``GET /v1/setup_intents/{id}``.

The customer is the session's, else a new one with ``customer_email``; the test card becomes a new
``pm_...``. The hosted page passes 3-D Secure, so only declines fail (:class:`CheckoutDeclinedError`,
nothing created). Per mode:

- payment: a ``succeeded`` PaymentIntent (``payment_intent.succeeded``); the payment method is attached to the
  customer only with ``payment_intent_data.setup_future_usage``.
- subscription: the payment method is attached and becomes the subscription default; a subscription with a
  paid ``subscription_create`` invoice (``customer.subscription.created``, ``invoice.paid``).
- setup: the payment method is attached; a ``succeeded`` SetupIntent (``setup_intent.succeeded``).

Then ``checkout.session.completed``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.billing_objects import setup_intent_json
from va_sim.stripe.charging import ChargeDecision, decide, new_id
from va_sim.stripe.customers import new_customer_json
from va_sim.stripe.errors import resource_missing
from va_sim.stripe.events import emit_event
from va_sim.stripe.objects import payment_intent_json
from va_sim.stripe.params import check_known
from va_sim.stripe.payment_intents import CHARGE_ID_LENGTH, PI_ID_LENGTH, SECRET_LENGTH
from va_sim.stripe.payment_methods import create_card_method
from va_sim.stripe.subscriptions import PlannedSubscription, start_subscription

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.objects import CardSpec

__all__ = ["CheckoutDeclinedError", "complete_session", "retrieve_setup_intent"]

_SETUP_INTENT_ID_LENGTH: Final = 24


class CheckoutDeclinedError(Exception):
    """The card was declined; the session stays open."""

    def __init__(self, decision: ChargeDecision) -> None:
        super().__init__(decision.message)
        self.decision = decision


def _customer(state: SimState, session: Mapping[str, Any]) -> dict[str, Any]:
    if session["customer"] is not None:
        return state.stripe.customers[session["customer"]]
    customer = new_customer_json(state, {"email": session["customer_email"]})
    state.stripe.customers[customer["id"]] = customer
    return customer


def _payment_intent(
    state: SimState, session: Mapping[str, Any], customer: str, method: str, data: Mapping[str, Any] | None
) -> dict[str, Any]:
    pi_id = new_id(state, "pi_", PI_ID_LENGTH)
    data = data or {}
    intent = payment_intent_json(
        pi_id,
        int(state.clock.now().timestamp()),
        amount=session["amount_total"],
        currency=session["currency"],
        client_secret=f"{pi_id}_secret_{state.ids.alnum(SECRET_LENGTH)}",
        fields={
            "customer": customer,
            "payment_method": method,
            "description": data.get("description"),
            "metadata": data.get("metadata"),
            "setup_future_usage": data.get("setup_future_usage"),
        },
    )
    intent.update(
        status="succeeded",
        amount_received=intent["amount"],
        latest_charge=new_id(state, "ch_", CHARGE_ID_LENGTH),
    )
    state.stripe.payment_intents[pi_id] = intent
    return intent


async def _pay(state: SimState, session: dict[str, Any], customer: str, card: CardSpec) -> None:
    data = state.stripe.checkout_options[session["id"]]["payment_intent_data"]
    keep = data is not None and data["setup_future_usage"] is not None
    method = create_card_method(state, card, customer=customer if keep else None)
    intent = _payment_intent(state, session, customer, method["id"], data)
    session["payment_intent"] = intent["id"]
    await emit_event(state, "payment_intent.succeeded", intent, call=None)


async def _subscribe(state: SimState, session: dict[str, Any], customer: str, card: CardSpec) -> None:
    store = state.stripe
    method = create_card_method(state, card, customer=customer)
    data = store.checkout_options[session["id"]]["subscription_data"] or {}
    lines = [(item["price"], item["quantity"]) for item in store.checkout_items[session["id"]]]
    plan = PlannedSubscription(
        customer=customer,
        items=[line for line in lines if line[0]["recurring"] is not None],
        extras=[line for line in lines if line[0]["recurring"] is None],
        default_payment_method=method["id"],
        metadata=data.get("metadata") or {},
        description=data.get("description"),
        authenticated=True,
    )
    subscription, invoice = await start_subscription(state, plan, call=None)
    session["subscription"] = subscription["id"]
    session["invoice"] = invoice["id"]


async def _set_up(state: SimState, session: dict[str, Any], customer: str, card: CardSpec) -> None:
    method = create_card_method(state, card, customer=customer)
    seti_id = new_id(state, "seti_", _SETUP_INTENT_ID_LENGTH)
    intent = setup_intent_json(
        seti_id,
        int(state.clock.now().timestamp()),
        customer=customer,
        payment_method=method["id"],
        client_secret=f"{seti_id}_secret_{state.ids.alnum(SECRET_LENGTH)}",
    )
    state.stripe.setup_intents[seti_id] = intent
    session["setup_intent"] = seti_id
    await emit_event(state, "setup_intent.succeeded", intent, call=None)


async def complete_session(state: SimState, session: dict[str, Any], card: CardSpec) -> None:
    """Complete an open session paid with ``card``; raises :class:`CheckoutDeclinedError` on a decline."""
    if session["mode"] != "setup":
        decision = decide(state.stripe.config, card, off_session=False)
        if decision.outcome == "decline":
            raise CheckoutDeclinedError(decision)
    customer = _customer(state, session)
    finish = {"payment": _pay, "subscription": _subscribe, "setup": _set_up}[session["mode"]]
    await finish(state, session, customer["id"], card)
    session.update(
        status="complete",
        payment_status="no_payment_required" if session["mode"] == "setup" else "paid",
        url=None,
        customer=customer["id"],
        customer_details={
            "address": None,
            "email": customer["email"] or session["customer_email"],
            "name": customer["name"],
            "phone": customer["phone"],
            "tax_exempt": "none",
            "tax_ids": [],
        },
    )
    await emit_event(state, "checkout.session.completed", session, call=None)


async def retrieve_setup_intent(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset({"client_secret"}))
    seti_id = call.path_params["seti_id"]
    intent = call.store.setup_intents.get(seti_id)
    if intent is None:
        raise resource_missing("setup_intent", seti_id)
    return copy.deepcopy(intent)
