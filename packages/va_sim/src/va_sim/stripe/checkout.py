"""FakeStripe checkout sessions (specs/30 §4.1 Decision P03-T07b rule 3): ``POST /v1/checkout/sessions``,
``GET /v1/checkout/sessions/{id}``, ``GET /v1/checkout/sessions/{id}/line_items``,
``POST /v1/checkout/sessions/{id}/expire``.

A session's ``url`` is the hosted page ``{base}/sim/stripe/checkout/{id}``
(:mod:`va_sim.stripe.checkout_page`), which completes or cancels it. Line items use a stored ``price`` or
inline ``price_data`` (stored as an inactive price after the whole request validated).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.billing_objects import checkout_item_json, checkout_session_json
from va_sim.stripe.catalog import (
    PRICE_DATA_PARAMS,
    PriceDraft,
    parse_price,
    product_name,
    require_price,
    store_price,
)
from va_sim.stripe.charging import new_id
from va_sim.stripe.errors import invalid_request, resource_missing
from va_sim.stripe.events import emit_event
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.params import (
    Params,
    check_known,
    currency,
    hash_list,
    integer,
    mapping,
    new_metadata,
    string,
    string_list,
)
from va_sim.stripe.payment_methods import require_customer
from va_sim.stripe.subscriptions import parse_quantity, same_cycle

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = [
    "create_checkout_session",
    "expire_checkout_session",
    "expire_session",
    "list_checkout_line_items",
    "require_session",
    "retrieve_checkout_session",
]

_SESSION_ID_LENGTH: Final = 58
_ITEM_ID_LENGTH: Final = 24
_MODES: Final = frozenset({"payment", "subscription", "setup"})
_PARAMS: Final = frozenset(
    {
        "mode",
        "success_url",
        "cancel_url",
        "customer",
        "customer_email",
        "client_reference_id",
        "metadata",
        "line_items",
        "payment_intent_data",
        "subscription_data",
        "currency",
        "payment_method_types",
        "expires_at",
    }
)
_LINE_PARAMS: Final = frozenset({"price", "price_data", "quantity"})
_PAYMENT_INTENT_DATA: Final = frozenset({"setup_future_usage", "description", "metadata"})
_SUBSCRIPTION_DATA: Final = frozenset({"description", "metadata"})
_SETUP_FUTURE_USAGE: Final = frozenset({"on_session", "off_session"})
_MIN_EXPIRY: Final = 30 * 60
_MAX_EXPIRY: Final = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class _Line:
    """A validated line item: a stored ``price`` or an inline ``draft``."""

    price: Mapping[str, Any] | None
    draft: PriceDraft | None
    quantity: int
    param: str

    def field(self, name: str) -> Any:
        if self.price is not None:
            return self.price[name]
        return getattr(self.draft, name)


def require_session(store: StripeStore, session_id: str) -> dict[str, Any]:
    session = store.checkout_sessions.get(session_id)
    if session is None:
        raise resource_missing("checkout.session", session_id)
    return session


def _parse_line(store: StripeStore, values: Params, prefix: str) -> _Line:
    check_known(values, _LINE_PARAMS, prefix=prefix)
    price_id = string(values, "price", prefix=prefix)
    price_data = mapping(values, "price_data", prefix=prefix)
    if (price_id is None) == (price_data is None):
        msg = f"You must specify exactly one of {prefix}[price] or {prefix}[price_data]."
        raise invalid_request(msg, param=f"{prefix}[price]")
    quantity = parse_quantity(values, prefix=prefix) or 1
    if price_data is not None:
        data_prefix = f"{prefix}[price_data]"
        check_known(price_data, PRICE_DATA_PARAMS, prefix=data_prefix)
        return _Line(
            None, parse_price(store, price_data, prefix=data_prefix), quantity, f"{prefix}[price_data]"
        )
    assert price_id is not None  # noqa: S101 - one of them is set
    price = require_price(store, price_id, param=f"{prefix}[price]")
    if not price["active"]:
        msg = f"The price specified is inactive. {prefix}[price] only accepts active prices."
        raise invalid_request(msg, param=f"{prefix}[price]")
    return _Line(price, None, quantity, f"{prefix}[price]")


def _check_payment_lines(lines: Sequence[_Line]) -> None:
    if not lines:
        msg = "line_items is required in payment mode."
        raise invalid_request(msg, code="parameter_missing", param="line_items")
    for line in lines:
        if line.field("recurring") is not None:
            msg = "You must be in subscription mode to specify a recurring price."
            raise invalid_request(msg, code="parameter_invalid", param=line.param)


def _check_subscription_lines(lines: Sequence[_Line]) -> None:
    recurring = [line for line in lines if line.field("recurring") is not None]
    if not recurring:
        msg = "You must specify at least one recurring price in `subscription` mode when using prices."
        raise invalid_request(msg, code="parameter_missing", param="line_items")
    first = {"recurring": recurring[0].field("recurring")}
    for line in recurring[1:]:
        if not same_cycle(first, {"recurring": line.field("recurring")}):
            msg = "All recurring prices in a subscription must have the same `interval` and `interval_count`."
            raise invalid_request(msg, code="parameter_invalid", param=line.param)


def _check_lines(mode: str, lines: Sequence[_Line], params: Params) -> None:
    if mode == "payment":
        _check_payment_lines(lines)
    elif mode == "subscription":
        _check_subscription_lines(lines)
    else:
        if lines:
            msg = "line_items cannot be used in setup mode."
            raise invalid_request(msg, param="line_items")
        if params.get("currency") is None and params.get("payment_method_types") is None:
            msg = "currency or payment_method_types is required in setup mode."
            raise invalid_request(msg, code="parameter_missing", param="currency")
    currencies = {line.field("currency") for line in lines}
    if len(currencies) > 1:
        msg = "All line items must use the same currency."
        raise invalid_request(msg, code="parameter_invalid", param="line_items")


def _mode_data(params: Params, mode: str) -> dict[str, Any]:
    """``payment_intent_data`` (payment mode) and ``subscription_data`` (subscription mode)."""
    options: dict[str, Any] = {"payment_intent_data": None, "subscription_data": None}
    for name, owner, allowed in (
        ("payment_intent_data", "payment", _PAYMENT_INTENT_DATA),
        ("subscription_data", "subscription", _SUBSCRIPTION_DATA),
    ):
        hash_ = mapping(params, name)
        if hash_ is None:
            continue
        if mode != owner:
            msg = f"You can only specify {name} in {owner} mode."
            raise invalid_request(msg, param=name)
        check_known(hash_, allowed, prefix=name)
        usage = string(hash_, "setup_future_usage", prefix=name)
        if usage is not None and usage not in _SETUP_FUTURE_USAGE:
            msg = f"Invalid {name}[setup_future_usage]: {usage}"
            raise invalid_request(msg, param=f"{name}[setup_future_usage]")
        options[name] = {
            "setup_future_usage": usage,
            "description": string(hash_, "description", prefix=name),
            "metadata": new_metadata(hash_, prefix=name),
        }
    return options


def _expires_at(params: Params, now: int) -> int:
    expires_at = integer(params, "expires_at")
    if expires_at is None:
        return now + _MAX_EXPIRY
    if not now + _MIN_EXPIRY <= expires_at <= now + _MAX_EXPIRY:
        msg = (
            "The `expires_at` timestamp must be between 30 minutes and 24 hours from Checkout Session "
            "creation."
        )
        raise invalid_request(msg, param="expires_at")
    return expires_at


def _customer_fields(call: StripeCall) -> dict[str, Any]:
    customer = string(call.params, "customer")
    email = string(call.params, "customer_email")
    if customer is not None and email is not None:
        msg = "You may only specify one of these parameters: customer, customer_email."
        raise invalid_request(msg, param="customer_email")
    if customer is not None:
        require_customer(call.store, customer)
    return {"customer": customer, "customer_email": email}


def _stored_prices(state: SimState, lines: Sequence[_Line]) -> list[Mapping[str, Any]]:
    """Each line's price, storing inline ``price_data`` as inactive prices."""
    prices: list[Mapping[str, Any]] = []
    for line in lines:
        if line.price is not None:
            prices.append(line.price)
        else:
            assert line.draft is not None  # noqa: S101 - a line has a price or a draft
            prices.append(store_price(state, line.draft, active=False))
    return prices


async def create_checkout_session(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _PARAMS)
    mode = string(params, "mode", required=True)
    if mode not in _MODES:
        msg = f"Invalid mode: {mode} (payment, subscription, or setup)"
        raise invalid_request(msg, param="mode")
    success_url = string(params, "success_url", required=True)
    fields = _customer_fields(call)
    lines = [
        _parse_line(call.store, values, f"line_items[{index}]")
        for index, values in enumerate(hash_list(params, "line_items") or [])
    ]
    _check_lines(mode, lines, params)
    options = _mode_data(params, mode)
    setup_currency = currency(params, required=False)
    expires_at = _expires_at(params, call.now)
    fields.update(
        mode=mode,
        success_url=success_url,
        cancel_url=string(params, "cancel_url"),
        client_reference_id=string(params, "client_reference_id"),
        metadata=new_metadata(params),
        payment_method_types=string_list(params, "payment_method_types"),
        expires_at=expires_at,
    )
    prices = _stored_prices(call.state, lines)
    session_id = new_id(call.state, "cs_test_", _SESSION_ID_LENGTH)
    items = [
        checkout_item_json(
            new_id(call.state, "li_", _ITEM_ID_LENGTH),
            price,
            quantity=line.quantity,
            description=product_name(call.store, price),
        )
        for price, line in zip(prices, lines, strict=True)
    ]
    session = checkout_session_json(
        session_id,
        call.now,
        {
            **fields,
            "url": f"{call.base_url}/sim/stripe/checkout/{session_id}",
            "currency": prices[0]["currency"] if prices else setup_currency,
            "amount_total": sum(item["amount_total"] for item in items) if mode != "setup" else None,
        },
    )
    call.store.checkout_sessions[session_id] = session
    call.store.checkout_items[session_id] = items
    call.store.checkout_options[session_id] = options
    return copy.deepcopy(session)


async def retrieve_checkout_session(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    return copy.deepcopy(require_session(call.store, call.path_params["cs_id"]))


async def list_checkout_line_items(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, LIST_PARAMS)
    session = require_session(call.store, call.path_params["cs_id"])
    items = call.store.checkout_items[session["id"]]
    return paginate(items, call.params, f"/v1/checkout/sessions/{session['id']}/line_items")


async def expire_session(state: SimState, session: dict[str, Any], *, call: StripeCall | None) -> None:
    """Expire an open session (``checkout.session.expired``)."""
    session["status"] = "expired"
    session["url"] = None
    await emit_event(state, "checkout.session.expired", session, call=call)


async def expire_checkout_session(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    session = require_session(call.store, call.path_params["cs_id"])
    if session["status"] != "open":
        msg = (
            "Only Checkout Sessions with a status of `open` can be expired. "
            f"This session is {session['status']}."
        )
        raise invalid_request(msg)
    await expire_session(call.state, session, call=call)
    return copy.deepcopy(session)
