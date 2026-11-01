"""FakeStripe invoice endpoints (specs/30 §4.1 Decision P03-T07b rule 6): ``GET /v1/invoices/{id}``,
``GET /v1/invoices``, ``POST /v1/invoices/{id}/pay``.

Paying an open invoice charges the given payment method (attached to the invoice's customer), else the
subscription's: success → ``paid``, ``invoice.paid``, and a ``past_due``/``incomplete`` subscription becomes
``active`` (``customer.subscription.updated``); a decline → 402 ``card_error`` and ``invoice.payment_failed``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import invalid_request, resource_missing
from va_sim.stripe.events import emit_event
from va_sim.stripe.invoicing import announce, apply_attempt, decision_for, payment_method_of
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.params import boolean, check_known, string
from va_sim.stripe.payment_methods import resolve_payment_method
from va_sim.stripe.subscriptions import NO_PAYMENT_METHOD

if TYPE_CHECKING:
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = ["list_invoices", "pay_invoice", "retrieve_invoice"]

_LIST_PARAMS: Final = LIST_PARAMS | {"customer", "subscription", "status"}
_STATUSES: Final = frozenset({"draft", "open", "paid", "uncollectible", "void"})
_PAY_PARAMS: Final = frozenset({"payment_method", "off_session"})
_RECOVERABLE: Final = frozenset({"past_due", "incomplete"})


def _subscription_of(invoice: Mapping[str, Any]) -> str:
    return str(invoice["parent"]["subscription_details"]["subscription"])


def _require_invoice(store: StripeStore, invoice_id: str) -> dict[str, Any]:
    invoice = store.invoices.get(invoice_id)
    if invoice is None:
        raise resource_missing("invoice", invoice_id)
    return invoice


async def retrieve_invoice(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    return copy.deepcopy(_require_invoice(call.store, call.path_params["in_id"]))


async def list_invoices(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _LIST_PARAMS)
    customer = string(params, "customer")
    subscription = string(params, "subscription")
    status = string(params, "status")
    if status is not None and status not in _STATUSES:
        msg = f"Invalid status: {status}"
        raise invalid_request(msg, param="status")

    def wanted(invoice: Mapping[str, Any]) -> bool:
        return (
            customer in {None, invoice["customer"]}
            and subscription in {None, _subscription_of(invoice)}
            and status in {None, invoice["status"]}
        )

    invoices = (inv for inv in reversed(call.store.invoices.values()) if wanted(inv))
    return paginate(invoices, params, "/v1/invoices")


async def pay_invoice(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _PAY_PARAMS)
    boolean(call.params, "off_session")
    store = call.store
    invoice = _require_invoice(store, call.path_params["in_id"])
    if invoice["status"] != "open":
        msg = "Invoice is already paid"
        raise invalid_request(msg)
    subscription = store.subscriptions[_subscription_of(invoice)]
    pm_id = string(call.params, "payment_method")
    if pm_id is not None:
        pm_id = resolve_payment_method(call, pm_id, customer=invoice["customer"])["id"]
    else:
        pm_id = payment_method_of(store, subscription)
    if pm_id is None:
        raise invalid_request(NO_PAYMENT_METHOD)
    decision = decision_for(store, pm_id)
    assert decision is not None  # noqa: S101 - a payment method is known
    paid = apply_attempt(call.state, invoice, decision)
    await announce(call.state, invoice, call=call)
    if not paid:
        raise decision.error({"invoice": invoice["id"]})
    if subscription["status"] in _RECOVERABLE:
        previous = {"status": subscription["status"]}
        subscription["status"] = "active"
        await emit_event(
            call.state, "customer.subscription.updated", subscription, call=call, previous_attributes=previous
        )
    return copy.deepcopy(invoice)
