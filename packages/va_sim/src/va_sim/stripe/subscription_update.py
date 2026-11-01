"""``POST /v1/subscriptions/{id}`` (specs/30 §4.1 Decision P03-T07b rule 5).

Items change by ``items[n][id]`` (price and/or quantity; a new price must bill on the same interval). A
changed item prorates the rest of its period unless ``proration_behavior=none``: a credit for the old price
and a charge for the new one. ``create_prorations`` keeps the lines for the next invoice; ``always_invoice``
bills them now (``subscription_update``; a failed charge makes the subscription ``past_due``, a net credit
goes to the customer balance). Any change emits ``customer.subscription.updated`` with
``previous_attributes``; a no-op update emits nothing.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.catalog import require_price
from va_sim.stripe.errors import invalid_request
from va_sim.stripe.events import emit_event
from va_sim.stripe.invoicing import (
    announce,
    apply_attempt,
    decision_for,
    open_invoice,
    payment_method_of,
    proration_line,
)
from va_sim.stripe.params import boolean, check_known, hash_list, metadata, string
from va_sim.stripe.periods import prorate
from va_sim.stripe.subscriptions import (
    check_attached,
    parse_quantity,
    proration_behavior,
    require_subscription,
    same_cycle,
)

if TYPE_CHECKING:
    from va_sim.stripe.http import StripeCall

__all__ = ["update_subscription"]

_PARAMS: Final = frozenset(
    {
        "items",
        "proration_behavior",
        "cancel_at_period_end",
        "metadata",
        "default_payment_method",
        "description",
        "off_session",
        "payment_behavior",
    }
)
_ITEM_PARAMS: Final = frozenset({"id", "price", "quantity"})

type _Change = tuple[dict[str, Any], dict[str, Any]]


def _new_price(
    call: StripeCall, values: Mapping[str, Any], item: Mapping[str, Any], prefix: str
) -> dict[str, Any]:
    price_id = string(values, "price", prefix=prefix)
    if price_id is None:
        return dict(item["price"])
    price = require_price(call.store, price_id, param=f"{prefix}[price]")
    current = item["price"]
    if (
        price["recurring"] is None
        or not same_cycle(price, current)
        or price["currency"] != current["currency"]
    ):
        msg = (
            "The simulator only changes a subscription item to a recurring price with the same interval, "
            "interval_count, and currency."
        )
        raise invalid_request(msg, code="parameter_invalid", param=f"{prefix}[price]")
    if not price["active"] and price["id"] != current["id"]:
        msg = f"The price specified is inactive. {prefix}[price] only accepts active prices."
        raise invalid_request(msg, param=f"{prefix}[price]")
    return copy.deepcopy(price)


def _apply_items(call: StripeCall, subscription: dict[str, Any]) -> list[_Change]:
    changes: list[_Change] = []
    items = {item["id"]: item for item in subscription["items"]["data"]}
    for index, values in enumerate(hash_list(call.params, "items") or []):
        prefix = f"items[{index}]"
        check_known(values, _ITEM_PARAMS, prefix=prefix)
        item_id = string(values, "id", required=True, prefix=prefix)
        item = items.get(item_id or "")
        if item is None:
            msg = f"Invalid {prefix}[id]: {item_id} is not an item of subscription {subscription['id']}"
            raise invalid_request(msg, param=f"{prefix}[id]")
        price = _new_price(call, values, item, prefix)
        quantity = parse_quantity(values, prefix=prefix) or item["quantity"]
        if price["id"] == item["price"]["id"] and quantity == item["quantity"]:
            continue
        old = copy.deepcopy(item)
        item["price"] = price
        item["quantity"] = quantity
        changes.append((old, item))
    return changes


def _apply_cancel(call: StripeCall, subscription: dict[str, Any], previous: dict[str, Any]) -> None:
    wanted = boolean(call.params, "cancel_at_period_end")
    if wanted is None or wanted == subscription["cancel_at_period_end"]:
        return
    previous["cancel_at_period_end"] = subscription["cancel_at_period_end"]
    previous["cancel_at"] = subscription["cancel_at"]
    subscription["cancel_at_period_end"] = wanted
    subscription["cancel_at"] = subscription["items"]["data"][0]["current_period_end"] if wanted else None
    subscription["canceled_at"] = call.now if wanted else None


def _apply_fields(call: StripeCall, subscription: dict[str, Any], previous: dict[str, Any]) -> None:
    params = call.params
    patch = metadata(params)
    if patch is not None:
        merged = patch.apply(subscription["metadata"])
        if merged != subscription["metadata"]:
            previous["metadata"] = subscription["metadata"]
            subscription["metadata"] = merged
    default_pm = string(params, "default_payment_method")
    if default_pm:
        check_attached(call.store, default_pm, subscription["customer"], param="default_payment_method")
    description = string(params, "description")
    for name, value in (("default_payment_method", default_pm), ("description", description)):
        if value is not None and (value or None) != subscription[name]:
            previous[name] = subscription[name]
            subscription[name] = value or None


def _proration_lines(call: StripeCall, changes: list[_Change]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for old, new in changes:
        for item, credit in ((old, True), (new, False)):
            amount = prorate(
                item["price"]["unit_amount"] * item["quantity"],
                start=item["current_period_start"],
                end=item["current_period_end"],
                now=call.now,
            )
            lines.append(proration_line(call.store, item, amount=amount, now=call.now, credit=credit))
    return lines


async def _invoice_now(
    call: StripeCall, subscription: dict[str, Any], lines: list[dict[str, Any]], previous: dict[str, Any]
) -> None:
    store = call.store
    lines = store.pending_lines.pop(subscription["id"], []) + lines
    period_end = subscription["items"]["data"][0]["current_period_end"]
    invoice = open_invoice(
        call.state, subscription, reason="subscription_update", lines=lines, period=(call.now, period_end)
    )
    subscription["latest_invoice"] = invoice["id"]
    decision = decision_for(store, payment_method_of(store, subscription))
    if not apply_attempt(call.state, invoice, decision) and subscription["status"] != "past_due":
        previous["status"] = subscription["status"]
        subscription["status"] = "past_due"
    await announce(call.state, invoice, call=call)


async def update_subscription(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _PARAMS)
    stored = require_subscription(call.store, call.path_params["sub_id"])
    behaviour = proration_behavior(call.params)
    boolean(call.params, "off_session")
    if stored["status"] == "canceled":
        msg = f"A canceled subscription can not be updated: {stored['id']}"
        raise invalid_request(msg)
    # Work on a copy so a rejected update changes nothing.
    subscription = copy.deepcopy(stored)
    previous: dict[str, Any] = {}
    changes = _apply_items(call, subscription)
    if changes:
        previous["items"] = copy.deepcopy(stored["items"])
    _apply_cancel(call, subscription, previous)
    _apply_fields(call, subscription, previous)
    if not previous:
        return copy.deepcopy(stored)
    stored.clear()
    stored.update(subscription)
    lines = _proration_lines(call, changes) if behaviour != "none" else []
    if lines and behaviour == "create_prorations":
        call.store.pending_lines.setdefault(stored["id"], []).extend(lines)
    elif lines:
        await _invoice_now(call, stored, lines, previous)
    await emit_event(
        call.state, "customer.subscription.updated", stored, call=call, previous_attributes=previous
    )
    return copy.deepcopy(stored)
