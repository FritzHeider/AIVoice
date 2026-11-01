"""FakeStripe subscriptions (specs/30 §4.1 Decision P03-T07b rule 5): ``POST /v1/subscriptions``,
``GET /v1/subscriptions/{id}``, ``GET /v1/subscriptions``, ``DELETE /v1/subscriptions/{id}``, and
:func:`start_subscription`, which checkout completion reuses.

A new subscription is invoiced at once (``subscription_create``). With no payment method, or
``payment_behavior=default_incomplete``, it stays ``incomplete`` with an open invoice and no charge; a decline
under ``error_if_incomplete`` is a 402 that stores nothing; any other decline leaves it ``incomplete`` with
``invoice.payment_failed``; success makes it ``active`` with ``invoice.paid``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.billing_objects import subscription_item_json, subscription_json
from va_sim.stripe.catalog import require_price
from va_sim.stripe.charging import ChargeDecision, new_id
from va_sim.stripe.errors import invalid_request, resource_missing
from va_sim.stripe.events import emit_event
from va_sim.stripe.invoicing import (
    announce,
    apply_attempt,
    decision_for,
    item_line,
    one_time_line,
    open_invoice,
)
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.params import (
    Params,
    boolean,
    check_known,
    hash_list,
    integer,
    mapping,
    new_metadata,
    string,
)
from va_sim.stripe.payment_methods import require_customer
from va_sim.stripe.periods import add_interval, anchor_day

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = [
    "NO_PAYMENT_METHOD",
    "PlannedSubscription",
    "cancel_subscription",
    "check_attached",
    "create_subscription",
    "list_subscriptions",
    "parse_quantity",
    "proration_behavior",
    "require_subscription",
    "retrieve_subscription",
    "same_cycle",
    "start_subscription",
]

_SUB_ID_LENGTH: Final = 24
_ITEM_ID_LENGTH: Final = 14
_PAYMENT_BEHAVIOURS: Final = frozenset({"allow_incomplete", "default_incomplete", "error_if_incomplete"})
_PRORATION_BEHAVIOURS: Final = frozenset({"create_prorations", "none", "always_invoice"})
_CREATE_PARAMS: Final = frozenset(
    {
        "customer",
        "items",
        "default_payment_method",
        "metadata",
        "description",
        "cancel_at_period_end",
        "payment_behavior",
        "off_session",
        "proration_behavior",
    }
)
_ITEM_PARAMS: Final = frozenset({"price", "quantity"})
_CANCEL_PARAMS: Final = frozenset({"invoice_now", "prorate", "cancellation_details"})
_CANCELLATION_DETAILS: Final = frozenset({"comment", "feedback"})
_LIST_PARAMS: Final = LIST_PARAMS | {"customer", "status", "price"}
_STATUSES: Final = frozenset({"active", "past_due", "canceled", "incomplete", "all", "ended"})
NO_PAYMENT_METHOD: Final = (
    "This customer has no attached payment source or default payment method. Please consider adding a "
    "default payment method."
)


def require_subscription(store: StripeStore, sub_id: str, *, param: str = "id") -> dict[str, Any]:
    subscription = store.subscriptions.get(sub_id)
    if subscription is None:
        raise resource_missing("subscription", sub_id, param=param)
    return subscription


def parse_quantity(values: Params, *, prefix: str) -> int | None:
    quantity = integer(values, "quantity", prefix=prefix)
    if quantity is not None and quantity < 1:
        msg = f"Invalid {prefix}[quantity]: must be at least 1"
        raise invalid_request(msg, param=f"{prefix}[quantity]")
    return quantity


def same_cycle(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two recurring prices bill on the same interval and interval count."""
    return bool(
        a["recurring"]["interval"] == b["recurring"]["interval"]
        and a["recurring"]["interval_count"] == b["recurring"]["interval_count"]
    )


def check_attached(store: StripeStore, pm_id: str, customer_id: str, *, param: str) -> None:
    method = store.payment_methods.get(pm_id)
    if method is None or method["customer"] != customer_id:
        msg = f"No such PaymentMethod: '{pm_id}'; it must be attached to customer {customer_id} first."
        raise invalid_request(msg, code="resource_missing", param=param)


@dataclass(frozen=True, slots=True)
class PlannedSubscription:
    """A validated subscription to start: recurring ``items`` and one-time ``extras`` as (price, quantity)."""

    customer: str
    items: Sequence[tuple[Mapping[str, Any], int]]
    extras: Sequence[tuple[Mapping[str, Any], int]] = ()
    default_payment_method: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    description: str | None = None
    payment_behavior: str = "allow_incomplete"
    authenticated: bool = False
    cancel_at_period_end: bool = False


def _plan_decision(store: StripeStore, plan: PlannedSubscription) -> tuple[bool, ChargeDecision | None]:
    """(charge now?, decision); raises the 402 of ``error_if_incomplete`` before anything is stored."""
    total = sum(price["unit_amount"] * qty for price, qty in [*plan.items, *plan.extras])
    customer = store.customers[plan.customer]
    pm_id = plan.default_payment_method or customer["invoice_settings"]["default_payment_method"]
    if total + customer["balance"] <= 0:
        return True, None
    if plan.payment_behavior == "default_incomplete":
        return False, None
    decision = decision_for(store, pm_id, authenticated=plan.authenticated)
    if plan.payment_behavior == "error_if_incomplete":
        if decision is None:
            raise invalid_request(NO_PAYMENT_METHOD)
        if not decision.succeeded:
            raise decision.error()
    return decision is not None, decision


async def start_subscription(
    state: SimState, plan: PlannedSubscription, *, call: StripeCall | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create, invoice, and charge a subscription; emits ``customer.subscription.created`` then the invoice
    event (none when nothing was charged). Returns (subscription, invoice)."""
    store = state.stripe
    charge, decision = _plan_decision(store, plan)
    now = int(state.clock.now().timestamp())
    sub_id = new_id(state, "sub_", _SUB_ID_LENGTH)
    first = plan.items[0][0]["recurring"]
    period = (now, add_interval(now, first["interval"], first["interval_count"], anchor=anchor_day(now)))
    items = [
        subscription_item_json(
            new_id(state, "si_", _ITEM_ID_LENGTH),
            now,
            subscription=sub_id,
            price=price,
            quantity=qty,
            period=period,
        )
        for price, qty in plan.items
    ]
    subscription = subscription_json(
        sub_id,
        now,
        items=items,
        fields={
            "customer": plan.customer,
            "currency": plan.items[0][0]["currency"],
            "default_payment_method": plan.default_payment_method,
            "metadata": plan.metadata,
            "description": plan.description,
        },
    )
    lines = [item_line(store, item) for item in items]
    lines += [one_time_line(store, p, quantity=q, period=period, subscription=sub_id) for p, q in plan.extras]
    store.subscriptions[sub_id] = subscription
    invoice = open_invoice(state, subscription, reason="subscription_create", lines=lines, period=period)
    subscription["latest_invoice"] = invoice["id"]
    paid = charge and apply_attempt(state, invoice, decision)
    subscription["status"] = "active" if paid else "incomplete"
    if plan.cancel_at_period_end:
        subscription["cancel_at_period_end"] = True
        subscription["cancel_at"] = period[1]
        subscription["canceled_at"] = now
    await emit_event(state, "customer.subscription.created", subscription, call=call)
    if charge:
        await announce(state, invoice, call=call)
    return subscription, invoice


def _parse_items(call: StripeCall) -> list[tuple[Mapping[str, Any], int]]:
    raw = hash_list(call.params, "items")
    if not raw:
        msg = "Missing required param: items."
        raise invalid_request(msg, code="parameter_missing", param="items")
    items: list[tuple[Mapping[str, Any], int]] = []
    for index, values in enumerate(raw):
        prefix = f"items[{index}]"
        check_known(values, _ITEM_PARAMS, prefix=prefix)
        price_id = string(values, "price", required=True, prefix=prefix)
        assert price_id is not None  # noqa: S101 - required=True
        price = require_price(call.store, price_id, param=f"{prefix}[price]")
        if price["recurring"] is None:
            msg = (
                f"The price specified is set to `type=one_time` but {prefix}[price] only accepts prices with "
                "`type=recurring`."
            )
            raise invalid_request(msg, param=f"{prefix}[price]")
        if not price["active"]:
            msg = f"The price specified is inactive. {prefix}[price] only accepts active prices."
            raise invalid_request(msg, param=f"{prefix}[price]")
        if items and (not same_cycle(items[0][0], price) or items[0][0]["currency"] != price["currency"]):
            msg = (
                "All prices on a subscription must have the same `recurring.interval`, `interval_count`, "
                "and currency."
            )
            raise invalid_request(msg, param=f"{prefix}[price]")
        items.append((price, parse_quantity(values, prefix=prefix) or 1))
    return items


def _choice(params: Params, name: str, allowed: frozenset[str], default: str) -> str:
    value = string(params, name)
    if value is None:
        return default
    if value not in allowed:
        msg = f"Invalid {name}: must be one of {', '.join(sorted(allowed))}"
        raise invalid_request(msg, param=name)
    return value


def proration_behavior(params: Params) -> str:
    return _choice(params, "proration_behavior", _PRORATION_BEHAVIOURS, "create_prorations")


async def create_subscription(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _CREATE_PARAMS)
    customer_id = string(params, "customer", required=True)
    assert customer_id is not None  # noqa: S101 - required=True
    require_customer(call.store, customer_id)
    items = _parse_items(call)
    default_pm = string(params, "default_payment_method")
    if default_pm is not None:
        check_attached(call.store, default_pm, customer_id, param="default_payment_method")
    proration_behavior(params)
    boolean(params, "off_session")
    plan = PlannedSubscription(
        customer=customer_id,
        items=items,
        default_payment_method=default_pm,
        metadata=new_metadata(params),
        description=string(params, "description"),
        payment_behavior=_choice(params, "payment_behavior", _PAYMENT_BEHAVIOURS, "allow_incomplete"),
        cancel_at_period_end=bool(boolean(params, "cancel_at_period_end")),
    )
    subscription, _invoice = await start_subscription(call.state, plan, call=call)
    return copy.deepcopy(subscription)


async def retrieve_subscription(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    return copy.deepcopy(require_subscription(call.store, call.path_params["sub_id"]))


async def list_subscriptions(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _LIST_PARAMS)
    customer = string(params, "customer")
    price = string(params, "price")
    status = _choice(params, "status", _STATUSES, "")

    def wanted(sub: Mapping[str, Any]) -> bool:
        if status in {"", "ended"}:
            status_ok = (sub["status"] == "canceled") == (status == "ended")
        else:
            status_ok = status in {"all", sub["status"]}
        prices = {item["price"]["id"] for item in sub["items"]["data"]}
        return status_ok and customer in {None, sub["customer"]} and (price is None or price in prices)

    subs = (sub for sub in reversed(call.store.subscriptions.values()) if wanted(sub))
    return paginate(subs, params, "/v1/subscriptions")


async def cancel_subscription(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _CANCEL_PARAMS)
    boolean(params, "invoice_now")
    boolean(params, "prorate")
    details = mapping(params, "cancellation_details") or {}
    check_known(details, _CANCELLATION_DETAILS, prefix="cancellation_details")
    subscription = require_subscription(call.store, call.path_params["sub_id"])
    if subscription["status"] == "canceled":
        msg = f"This subscription has already been canceled: {subscription['id']}"
        raise invalid_request(msg)
    subscription["status"] = "canceled"
    subscription["canceled_at"] = subscription["ended_at"] = call.now
    subscription["cancellation_details"] = {
        "comment": string(details, "comment", prefix="cancellation_details"),
        "feedback": string(details, "feedback", prefix="cancellation_details"),
        "reason": "cancellation_requested",
    }
    call.store.pending_lines.pop(subscription["id"], None)
    await emit_event(call.state, "customer.subscription.deleted", subscription, call=call)
    return copy.deepcopy(subscription)
