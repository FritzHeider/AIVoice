"""FakeStripe billing object builders (specs/30 §4.1 Decision P03-T07b rules 2-6): products, prices,
checkout sessions and their line items, setup intents, subscriptions, subscription items, invoices, invoice
lines.

Pure JSON builders shaped like the current Stripe API objects (period dates live on subscription items;
invoices reference their subscription through ``parent``). Callers store the result and mutate it in place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from va_sim.stripe.objects import list_json

__all__ = [
    "checkout_item_json",
    "checkout_session_json",
    "invoice_json",
    "invoice_line_json",
    "price_json",
    "product_json",
    "setup_intent_json",
    "subscription_item_json",
    "subscription_json",
]

_SECONDS_PER_DAY: Final = 86_400


def _base(object_id: str, kind: str, created: int) -> dict[str, Any]:
    return {"id": object_id, "object": kind, "created": created, "livemode": False}


def product_json(product_id: str, created: int, fields: Mapping[str, Any]) -> dict[str, Any]:
    """A ``product`` (``fields``: name, description, metadata, active)."""
    return {
        **_base(product_id, "product", created),
        "active": fields.get("active", True),
        "default_price": None,
        "description": fields.get("description"),
        "images": [],
        "marketing_features": [],
        "metadata": dict(fields.get("metadata") or {}),
        "name": fields["name"],
        "package_dimensions": None,
        "shippable": None,
        "statement_descriptor": None,
        "tax_code": None,
        "type": "service",
        "unit_label": None,
        "updated": created,
        "url": None,
    }


def price_json(price_id: str, created: int, fields: Mapping[str, Any]) -> dict[str, Any]:
    """A ``price`` (``fields``: product, currency, unit_amount, recurring {interval, interval_count} | None,
    nickname, lookup_key, metadata, active)."""
    recurring = fields.get("recurring")
    return {
        **_base(price_id, "price", created),
        "active": fields.get("active", True),
        "billing_scheme": "per_unit",
        "currency": fields["currency"],
        "custom_unit_amount": None,
        "lookup_key": fields.get("lookup_key"),
        "metadata": dict(fields.get("metadata") or {}),
        "nickname": fields.get("nickname"),
        "product": fields["product"],
        "recurring": (
            None
            if recurring is None
            else {
                "interval": recurring["interval"],
                "interval_count": recurring["interval_count"],
                "meter": None,
                "trial_period_days": None,
                "usage_type": "licensed",
            }
        ),
        "tax_behavior": "unspecified",
        "tiers_mode": None,
        "transform_quantity": None,
        "type": "one_time" if recurring is None else "recurring",
        "unit_amount": fields["unit_amount"],
        "unit_amount_decimal": str(fields["unit_amount"]),
    }


def checkout_session_json(session_id: str, created: int, fields: Mapping[str, Any]) -> dict[str, Any]:
    """An open ``checkout.session`` (``fields``: mode, url, success_url, cancel_url, customer, customer_email,
    client_reference_id, metadata, currency, amount_total, expires_at, payment_method_types)."""
    return {
        **_base(session_id, "checkout.session", created),
        "amount_subtotal": fields.get("amount_total"),
        "amount_total": fields.get("amount_total"),
        "cancel_url": fields.get("cancel_url"),
        "client_reference_id": fields.get("client_reference_id"),
        "currency": fields.get("currency"),
        "customer": fields.get("customer"),
        "customer_details": None,
        "customer_email": fields.get("customer_email"),
        "expires_at": fields.get("expires_at", created + _SECONDS_PER_DAY),
        "invoice": None,
        "metadata": dict(fields.get("metadata") or {}),
        "mode": fields["mode"],
        "payment_intent": None,
        "payment_method_types": list(fields.get("payment_method_types") or ["card"]),
        "payment_status": "unpaid",
        "setup_intent": None,
        "status": "open",
        "subscription": None,
        "success_url": fields["success_url"],
        "ui_mode": "hosted",
        "url": fields["url"],
    }


def checkout_item_json(
    item_id: str, price: Mapping[str, Any], *, quantity: int, description: str
) -> dict[str, Any]:
    """A checkout session line ``item`` (embeds the full price)."""
    amount = price["unit_amount"] * quantity
    return {
        "id": item_id,
        "object": "item",
        "amount_discount": 0,
        "amount_subtotal": amount,
        "amount_tax": 0,
        "amount_total": amount,
        "currency": price["currency"],
        "description": description,
        "price": dict(price),
        "quantity": quantity,
    }


def setup_intent_json(
    setup_intent_id: str, created: int, *, customer: str, payment_method: str, client_secret: str
) -> dict[str, Any]:
    """A ``succeeded`` off-session ``setup_intent``."""
    return {
        **_base(setup_intent_id, "setup_intent", created),
        "client_secret": client_secret,
        "customer": customer,
        "description": None,
        "last_setup_error": None,
        "metadata": {},
        "next_action": None,
        "payment_method": payment_method,
        "payment_method_types": ["card"],
        "status": "succeeded",
        "usage": "off_session",
    }


def subscription_item_json(  # noqa: PLR0913 - mirrors the item object
    item_id: str,
    created: int,
    *,
    subscription: str,
    price: Mapping[str, Any],
    quantity: int,
    period: tuple[int, int],
) -> dict[str, Any]:
    """A ``subscription_item`` with its current period."""
    return {
        **_base(item_id, "subscription_item", created),
        "current_period_end": period[1],
        "current_period_start": period[0],
        "discounts": [],
        "metadata": {},
        "price": dict(price),
        "quantity": quantity,
        "subscription": subscription,
        "tax_rates": [],
    }


def subscription_json(
    subscription_id: str, created: int, *, items: Sequence[Mapping[str, Any]], fields: Mapping[str, Any]
) -> dict[str, Any]:
    """An ``incomplete`` ``subscription`` (``fields``: customer, currency, default_payment_method, metadata,
    description)."""
    return {
        **_base(subscription_id, "subscription", created),
        "billing_cycle_anchor": created,
        "cancel_at": None,
        "cancel_at_period_end": False,
        "canceled_at": None,
        "cancellation_details": {"comment": None, "feedback": None, "reason": None},
        "collection_method": "charge_automatically",
        "currency": fields["currency"],
        "customer": fields["customer"],
        "days_until_due": None,
        "default_payment_method": fields.get("default_payment_method"),
        "description": fields.get("description"),
        "discounts": [],
        "ended_at": None,
        "items": list_json(items, f"/v1/subscription_items?subscription={subscription_id}", has_more=False),
        "latest_invoice": None,
        "metadata": dict(fields.get("metadata") or {}),
        "pending_update": None,
        "start_date": created,
        "status": "incomplete",
        "trial_end": None,
        "trial_start": None,
    }


def invoice_line_json(line_id: str, invoice_id: str, line: Mapping[str, Any]) -> dict[str, Any]:
    """An invoice ``line_item`` (``line``: amount, currency, description, quantity, period, price,
    product, subscription, subscription_item, proration)."""
    return {
        "id": line_id,
        "object": "line_item",
        "amount": line["amount"],
        "currency": line["currency"],
        "description": line["description"],
        "discount_amounts": [],
        "discountable": not line["proration"],
        "invoice": invoice_id,
        "livemode": False,
        "metadata": {},
        "parent": {
            "type": "subscription_item_details",
            "subscription_item_details": {
                "invoice_item": None,
                "proration": line["proration"],
                "proration_details": None,
                "subscription": line["subscription"],
                "subscription_item": line["subscription_item"],
            },
        },
        "period": {"start": line["period"][0], "end": line["period"][1]},
        "pricing": {
            "type": "price_details",
            "price_details": {"price": line["price"], "product": line["product"]},
            "unit_amount_decimal": str(line["unit_amount"]),
        },
        "quantity": line["quantity"],
    }


def invoice_json(
    invoice_id: str, created: int, *, lines: Sequence[Mapping[str, Any]], fields: Mapping[str, Any]
) -> dict[str, Any]:
    """An ``open`` ``invoice`` (``fields``: customer, currency, billing_reason, period, subscription,
    subscription_metadata, total, amount_due, starting_balance, ending_balance, number)."""
    return {
        **_base(invoice_id, "invoice", created),
        "amount_due": fields["amount_due"],
        "amount_paid": 0,
        "amount_remaining": fields["amount_due"],
        "attempt_count": 0,
        "attempted": False,
        "auto_advance": True,
        "billing_reason": fields["billing_reason"],
        "collection_method": "charge_automatically",
        "currency": fields["currency"],
        "customer": fields["customer"],
        "default_payment_method": None,
        "description": None,
        "ending_balance": fields["ending_balance"],
        "hosted_invoice_url": None,
        "invoice_pdf": None,
        "lines": list_json(lines, f"/v1/invoices/{invoice_id}/lines", has_more=False),
        "metadata": {},
        "next_payment_attempt": None,
        "number": fields["number"],
        "parent": {
            "type": "subscription_details",
            "quote_details": None,
            "subscription_details": {
                "metadata": dict(fields["subscription_metadata"]),
                "subscription": fields["subscription"],
            },
        },
        "period_end": fields["period"][1],
        "period_start": fields["period"][0],
        "starting_balance": fields["starting_balance"],
        "status": "open",
        "status_transitions": {
            "finalized_at": created,
            "marked_uncollectible_at": None,
            "paid_at": None,
            "voided_at": None,
        },
        "subtotal": fields["total"],
        "total": fields["total"],
    }
