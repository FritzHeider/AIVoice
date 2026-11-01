"""Subscription invoices (specs/30 §4.1 Decision P03-T07b rules 5-7): invoice lines, opening an invoice
(customer credit balance applied), and settling a payment attempt.

Lines are plain dicts (amount, currency, description, quantity, period, price, product, unit_amount,
subscription, subscription_item, proration) so proration lines can wait in ``StripeStore.pending_lines``
until the next invoice. Invoice charges create no PaymentIntent (sim simplification).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.billing_objects import invoice_json, invoice_line_json
from va_sim.stripe.catalog import product_name
from va_sim.stripe.charging import ChargeDecision, decide, new_id
from va_sim.stripe.events import emit_event

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = [
    "announce",
    "apply_attempt",
    "decision_for",
    "item_line",
    "one_time_line",
    "open_invoice",
    "payment_method_of",
    "proration_line",
]

_INVOICE_ID_LENGTH: Final = 24
_LINE_ID_LENGTH: Final = 24
_CENTS: Final = 100
_TIMES: Final = "\N{MULTIPLICATION SIGN}"


def _money(amount: int, currency: str) -> str:
    value = f"{abs(amount) / _CENTS:.2f}"
    return f"${value}" if currency == "usd" else f"{value} {currency.upper()}"


def _line(  # noqa: PLR0913 - one field per line attribute
    price: Mapping[str, Any],
    *,
    amount: int,
    quantity: int,
    description: str,
    period: tuple[int, int],
    subscription: str,
    subscription_item: str | None,
    proration: bool = False,
) -> dict[str, Any]:
    return {
        "amount": amount,
        "currency": price["currency"],
        "description": description,
        "quantity": quantity,
        "period": period,
        "price": price["id"],
        "product": price["product"],
        "unit_amount": price["unit_amount"],
        "subscription": subscription,
        "subscription_item": subscription_item,
        "proration": proration,
    }


def item_line(store: StripeStore, item: Mapping[str, Any]) -> dict[str, Any]:
    """The line billing a subscription item for its current period."""
    price = item["price"]
    recurring = price["recurring"]
    count = recurring["interval_count"]
    every = recurring["interval"] if count == 1 else f"{count} {recurring['interval']}s"
    description = (
        f"{item['quantity']} {_TIMES} {product_name(store, price)} "
        f"(at {_money(price['unit_amount'], price['currency'])} / {every})"
    )
    return _line(
        price,
        amount=price["unit_amount"] * item["quantity"],
        quantity=item["quantity"],
        description=description,
        period=(item["current_period_start"], item["current_period_end"]),
        subscription=item["subscription"],
        subscription_item=item["id"],
    )


def one_time_line(
    store: StripeStore, price: Mapping[str, Any], *, quantity: int, period: tuple[int, int], subscription: str
) -> dict[str, Any]:
    """A one-time checkout item billed on the first subscription invoice."""
    return _line(
        price,
        amount=price["unit_amount"] * quantity,
        quantity=quantity,
        description=f"{quantity} {_TIMES} {product_name(store, price)}",
        period=period,
        subscription=subscription,
        subscription_item=None,
    )


def proration_line(
    store: StripeStore, item: Mapping[str, Any], *, amount: int, now: int, credit: bool
) -> dict[str, Any]:
    """A proration line for ``item`` (its price and quantity) over the rest of its period."""
    name = product_name(store, item["price"])
    description = (
        f"Unused time on {item['quantity']} {_TIMES} {name}"
        if credit
        else f"Remaining time on {item['quantity']} {_TIMES} {name}"
    )
    return _line(
        item["price"],
        amount=-amount if credit else amount,
        quantity=item["quantity"],
        description=description,
        period=(now, item["current_period_end"]),
        subscription=item["subscription"],
        subscription_item=item["id"],
        proration=True,
    )


def open_invoice(
    state: SimState,
    subscription: Mapping[str, Any],
    *,
    reason: str,
    lines: Sequence[Mapping[str, Any]],
    period: tuple[int, int],
) -> dict[str, Any]:
    """Store an ``open`` invoice; the customer's balance (negative = credit) is applied to the total and a
    negative total becomes credit."""
    store = state.stripe
    customer = store.customers[subscription["customer"]]
    invoice_id = new_id(state, "in_", _INVOICE_ID_LENGTH)
    total = sum(line["amount"] for line in lines)
    starting = customer["balance"]
    due = total + starting
    ending = min(due, 0)
    customer["balance"] = ending
    number = sum(1 for inv in store.invoices.values() if inv["customer"] == customer["id"]) + 1
    invoice = invoice_json(
        invoice_id,
        int(state.clock.now().timestamp()),
        lines=[invoice_line_json(new_id(state, "il_", _LINE_ID_LENGTH), invoice_id, line) for line in lines],
        fields={
            "customer": customer["id"],
            "currency": subscription["currency"],
            "billing_reason": reason,
            "period": period,
            "subscription": subscription["id"],
            "subscription_metadata": subscription["metadata"],
            "total": total,
            "amount_due": max(due, 0),
            "starting_balance": starting,
            "ending_balance": ending,
            "number": f"{customer['invoice_prefix']}-{number:04d}",
        },
    )
    store.invoices[invoice_id] = invoice
    return invoice


def payment_method_of(store: StripeStore, subscription: Mapping[str, Any]) -> str | None:
    """The subscription's default payment method, else the customer's invoice default."""
    default = subscription["default_payment_method"]
    if default is not None:
        return str(default)
    customer = store.customers[subscription["customer"]]
    method = customer["invoice_settings"]["default_payment_method"]
    return None if method is None else str(method)


def decision_for(
    store: StripeStore, pm_id: str | None, *, authenticated: bool = False, force_decline: bool = False
) -> ChargeDecision | None:
    """How charging ``pm_id`` ends (None: no payment method). ``authenticated`` charges (the hosted checkout
    page) pass 3-D Secure; others are off-session."""
    if pm_id is None:
        return None
    decision = decide(
        store.config, store.cards[pm_id], off_session=not authenticated, force_decline=force_decline
    )
    return ChargeDecision("succeed") if decision.outcome == "authenticate" else decision


def apply_attempt(state: SimState, invoice: dict[str, Any], decision: ChargeDecision | None) -> bool:
    """Record one payment attempt (nothing due pays without a charge); returns whether the invoice is paid."""
    invoice["attempted"] = True
    charged = invoice["amount_due"] > 0
    if charged:
        invoice["attempt_count"] += 1
    if charged and (decision is None or not decision.succeeded):
        return False
    invoice["status"] = "paid"
    invoice["amount_paid"] = invoice["amount_due"]
    invoice["amount_remaining"] = 0
    invoice["status_transitions"]["paid_at"] = int(state.clock.now().timestamp())
    return True


async def announce(state: SimState, invoice: Mapping[str, Any], *, call: StripeCall | None) -> None:
    """``invoice.paid`` for a paid invoice, else ``invoice.payment_failed``."""
    kind = "invoice.paid" if invoice["status"] == "paid" else "invoice.payment_failed"
    await emit_event(state, kind, invoice, call=call)
