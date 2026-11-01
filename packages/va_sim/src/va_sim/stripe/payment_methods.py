"""FakeStripe payment methods (specs/30 §4.1 Decision P03-T07a rule 6).

Magic test ids (:data:`va_sim.stripe.objects.TEST_CARDS`) stand for test cards: attaching one, or passing it
to a customer or payment intent, creates a new ``pm_...`` whose charging behaviour is remembered in
``StripeStore.cards``. ``POST /v1/payment_methods/{id}/attach``, ``GET /v1/payment_methods/{id}``, and
``GET /v1/customers/{id}/payment_methods``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import invalid_request, resource_missing
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.objects import TEST_CARDS, payment_method_json
from va_sim.stripe.params import check_known, string

if TYPE_CHECKING:
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = [
    "attach_payment_method",
    "customer_payment_methods",
    "require_customer",
    "resolve_payment_method",
    "retrieve_payment_method",
]

_PM_ID_LENGTH: Final = 24
_ATTACH_PARAMS: Final = frozenset({"customer"})
_CUSTOMER_LIST_PARAMS: Final = LIST_PARAMS | {"type"}
_CARD: Final = "card"


def require_customer(store: StripeStore, customer_id: str, *, param: str = "customer") -> dict[str, Any]:
    customer = store.customers.get(customer_id)
    if customer is None:
        raise resource_missing("customer", customer_id, param=param)
    return customer


def resolve_payment_method(
    call: StripeCall, pm_id: str, *, customer: str | None, param: str = "payment_method"
) -> dict[str, Any]:
    """The stored payment method ``pm_id`` (a magic id creates a fresh one attached to ``customer``).

    A stored method attached to another customer is a 400.
    """
    store = call.store
    card = TEST_CARDS.get(pm_id)
    if card is not None:
        new_id = f"pm_{call.state.ids.alnum(_PM_ID_LENGTH)}"
        year = call.state.clock.now().year
        method = payment_method_json(new_id, call.now, card, customer=customer, year=year)
        store.payment_methods[new_id] = method
        store.cards[new_id] = card
        return method
    stored = store.payment_methods.get(pm_id)
    if stored is None:
        raise resource_missing("PaymentMethod", pm_id, param=param)
    method = stored
    owner = method["customer"]
    if customer is not None and owner is not None and owner != customer:
        msg = f"The payment method {pm_id} belongs to customer {owner}, not {customer}."
        raise invalid_request(msg, param=param)
    if customer is not None and owner is None:
        method["customer"] = customer
    return method


async def attach_payment_method(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _ATTACH_PARAMS)
    customer_id = string(call.params, "customer", required=True)
    assert customer_id is not None  # noqa: S101 - required=True
    require_customer(call.store, customer_id)
    method = resolve_payment_method(call, call.path_params["pm_id"], customer=customer_id, param="id")
    return copy.deepcopy(method)


async def retrieve_payment_method(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    pm_id = call.path_params["pm_id"]
    method = call.store.payment_methods.get(pm_id)
    if method is None:
        raise resource_missing("PaymentMethod", pm_id)
    return copy.deepcopy(method)


async def customer_payment_methods(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _CUSTOMER_LIST_PARAMS)
    customer_id = call.path_params["customer_id"]
    require_customer(call.store, customer_id, param="customer")
    kind = string(call.params, "type")
    if kind is not None and kind != _CARD:
        msg = f"Invalid type: {kind} (the simulator only has card payment methods)"
        raise invalid_request(msg, param="type")
    owned = [m for m in call.store.payment_methods.values() if m["customer"] == customer_id]
    return paginate(reversed(owned), call.params, f"/v1/customers/{customer_id}/payment_methods")
