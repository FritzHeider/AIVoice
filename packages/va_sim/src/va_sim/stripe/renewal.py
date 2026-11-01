"""Ending a subscription period on demand (specs/30 §4.1 Decision P03-T07b rule 7), behind the control route
``POST /sim/stripe/subscriptions/{id}/renew``.

``cancel_at_period_end`` subscriptions end (``customer.subscription.deleted``). Others move every item to the
next period (from the old end, keeping the billing anchor day) and are invoiced (``subscription_cycle``, items
plus pending prorations): paid → ``active``; a decline (forced with ``fail``, the armed config, the card, or
no payment method) → ``past_due``. The invoice event comes first, then ``customer.subscription.updated``. The
sim clock does not move.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from va_sim.stripe.events import emit_event
from va_sim.stripe.invoicing import (
    announce,
    apply_attempt,
    decision_for,
    item_line,
    open_invoice,
    payment_method_of,
)
from va_sim.stripe.periods import add_interval, anchor_day

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["renew_subscription"]


async def _end(state: SimState, subscription: dict[str, Any]) -> None:
    subscription["status"] = "canceled"
    subscription["ended_at"] = subscription["items"]["data"][0]["current_period_end"]
    subscription["cancellation_details"]["reason"] = "cancellation_requested"
    state.stripe.pending_lines.pop(subscription["id"], None)
    await emit_event(state, "customer.subscription.deleted", subscription, call=None)


async def renew_subscription(
    state: SimState, subscription: dict[str, Any], *, fail: bool
) -> dict[str, Any] | None:
    """End the current period of an ``active``/``past_due`` subscription; returns the new invoice (None when
    the subscription ended instead)."""
    if subscription["cancel_at_period_end"]:
        await _end(state, subscription)
        return None
    store = state.stripe
    previous: dict[str, Any] = {"items": copy.deepcopy(subscription["items"])}
    anchor = anchor_day(subscription["billing_cycle_anchor"])
    for item in subscription["items"]["data"]:
        recurring = item["price"]["recurring"]
        start = item["current_period_end"]
        item["current_period_start"] = start
        item["current_period_end"] = add_interval(
            start, recurring["interval"], recurring["interval_count"], anchor=anchor
        )
    first = subscription["items"]["data"][0]
    lines = [item_line(store, item) for item in subscription["items"]["data"]]
    lines += store.pending_lines.pop(subscription["id"], [])
    invoice = open_invoice(
        state,
        subscription,
        reason="subscription_cycle",
        lines=lines,
        period=(first["current_period_start"], first["current_period_end"]),
    )
    subscription["latest_invoice"] = invoice["id"]
    decision = decision_for(store, payment_method_of(store, subscription), force_decline=fail)
    status = "active" if apply_attempt(state, invoice, decision) else "past_due"
    if status != subscription["status"]:
        previous["status"] = subscription["status"]
        subscription["status"] = status
    await announce(state, invoice, call=None)
    await emit_event(
        state, "customer.subscription.updated", subscription, call=None, previous_attributes=previous
    )
    return invoice
