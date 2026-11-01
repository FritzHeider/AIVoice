"""FakeStripe customers (specs/30 §4.1 Decision P03-T07a rule 7): ``POST /v1/customers``,
``GET``/``POST /v1/customers/{id}``.

Updates merge ``metadata`` key-wise (an empty value deletes a key, ``metadata=`` clears it) and an empty
string unsets a scalar field. ``invoice_settings[default_payment_method]`` must name a payment method
attached to the customer.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import invalid_request
from va_sim.stripe.objects import customer_json
from va_sim.stripe.params import check_known, mapping, metadata, string
from va_sim.stripe.payment_methods import require_customer, resolve_payment_method

if TYPE_CHECKING:
    from va_sim.stripe.http import StripeCall

__all__ = ["create_customer", "retrieve_customer", "update_customer"]

_CUSTOMER_ID_LENGTH: Final = 14
_SCALARS: Final = ("email", "name", "description", "phone")
_UPDATE_PARAMS: Final = frozenset({*_SCALARS, "metadata", "invoice_settings"})
_CREATE_PARAMS: Final = _UPDATE_PARAMS | {"payment_method"}
_INVOICE_SETTINGS: Final = frozenset({"default_payment_method"})
_DEFAULT_PM: Final = "default_payment_method"


def _default_payment_method(
    call: StripeCall, customer: dict[str, Any], aliases: dict[str, str] | None = None
) -> None:
    settings = mapping(call.params, "invoice_settings")
    if settings is None:
        return
    check_known(settings, _INVOICE_SETTINGS, prefix="invoice_settings")
    pm_id = string(settings, _DEFAULT_PM, prefix="invoice_settings")
    if pm_id is None:
        return
    param = f"invoice_settings[{_DEFAULT_PM}]"
    pm_id = (aliases or {}).get(pm_id, pm_id)
    if not pm_id:
        customer["invoice_settings"][_DEFAULT_PM] = None
        return
    method = call.store.payment_methods.get(pm_id)
    if method is None or method["customer"] != customer["id"]:
        msg = f"No such PaymentMethod: '{pm_id}'; it must be attached to customer {customer['id']} first."
        raise invalid_request(
            msg,
            code="resource_missing",
            param=param,
        )
    customer["invoice_settings"][_DEFAULT_PM] = pm_id


def _apply_scalars(call: StripeCall, customer: dict[str, Any]) -> None:
    for name in _SCALARS:
        value = string(call.params, name)
        if value is not None:
            customer[name] = value or None
    patch = metadata(call.params)
    if patch is not None:
        customer["metadata"] = patch.apply(customer["metadata"])


async def create_customer(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _CREATE_PARAMS)
    customer_id = f"cus_{call.state.ids.alnum(_CUSTOMER_ID_LENGTH)}"
    customer = customer_json(customer_id, call.now, {})
    _apply_scalars(call, customer)
    # Validate everything before anything is stored, so a rejected create leaves no trace.
    settings = mapping(call.params, "invoice_settings")
    if settings is not None:
        check_known(settings, _INVOICE_SETTINGS, prefix="invoice_settings")
    pm_id = string(call.params, "payment_method")
    call.store.customers[customer_id] = customer
    known_methods = set(call.store.payment_methods)
    try:
        aliases: dict[str, str] = {}
        if pm_id is not None:
            # A magic id passed as both payment_method and the default means the method just created.
            aliases[pm_id] = resolve_payment_method(call, pm_id, customer=customer_id)["id"]
        _default_payment_method(call, customer, aliases)
    except BaseException:
        _forget_customer(call, customer_id, known_methods)
        raise
    return copy.deepcopy(customer)


def _forget_customer(call: StripeCall, customer_id: str, known_methods: set[str]) -> None:
    """Undo a rejected create: drop the customer and the methods it created, detach the ones it attached."""
    store = call.store
    del store.customers[customer_id]
    for pm_id, method in list(store.payment_methods.items()):
        if method["customer"] != customer_id:
            continue
        if pm_id in known_methods:
            method["customer"] = None
        else:
            del store.payment_methods[pm_id]
            store.cards.pop(pm_id, None)


async def retrieve_customer(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    return copy.deepcopy(require_customer(call.store, call.path_params["customer_id"], param="id"))


async def update_customer(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _UPDATE_PARAMS)
    stored = require_customer(call.store, call.path_params["customer_id"], param="id")
    # Work on a copy so a rejected update changes nothing.
    customer = copy.deepcopy(stored)
    _apply_scalars(call, customer)
    _default_payment_method(call, customer)
    stored.clear()
    stored.update(customer)
    return copy.deepcopy(customer)
