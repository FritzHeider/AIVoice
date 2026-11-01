"""FakeStripe object builders and the test card table (specs/30 §4.1 Decision P03-T07a rules 3, 5-10).

Stored objects are plain JSON dicts shaped like the Stripe API objects; handlers mutate them in place and
answer deep copies. Every object has ``id``, ``object``, ``created`` (sim clock, unix seconds), and
``livemode: false``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_API_VERSION",
    "TEST_CARDS",
    "CardBehaviour",
    "CardSpec",
    "customer_json",
    "event_json",
    "list_json",
    "payment_intent_json",
    "payment_method_json",
    "portal_json",
]

# The API version stripe-python 16.1 pins; events carry it when the causing request sent no Stripe-Version.
DEFAULT_API_VERSION: Final = "2026-10-28.endive"
_PORTAL_CONFIGURATION: Final = "bpc_sim"

type CardBehaviour = Literal["succeed", "decline", "authenticate"]


@dataclass(frozen=True, slots=True)
class CardSpec:
    """What a test payment method looks like and how charging it behaves."""

    brand: str
    last4: str
    funding: str
    behaviour: CardBehaviour = "succeed"
    decline_code: str | None = None


TEST_CARDS: Final[Mapping[str, CardSpec]] = MappingProxyType(
    {
        "pm_card_visa": CardSpec("visa", "4242", "credit"),
        "pm_card_mastercard": CardSpec("mastercard", "4444", "credit"),
        "pm_card_chargeDeclined": CardSpec("visa", "0002", "credit", "decline", "generic_decline"),
        "pm_card_chargeDeclinedInsufficientFunds": CardSpec(
            "visa", "9995", "credit", "decline", "insufficient_funds"
        ),
        "pm_card_authenticationRequired": CardSpec("visa", "3184", "credit", "authenticate"),
    }
)
_EXP_MONTH: Final = 12
_EXP_YEARS_AHEAD: Final = 3


def _base(object_id: str, kind: str, created: int) -> dict[str, Any]:
    return {"id": object_id, "object": kind, "created": created, "livemode": False}


def customer_json(customer_id: str, created: int, fields: Mapping[str, Any]) -> dict[str, Any]:
    """A ``customer`` with ``fields`` (email, name, description, phone, metadata) applied."""
    return {
        **_base(customer_id, "customer", created),
        "address": None,
        "balance": 0,
        "currency": None,
        "default_source": None,
        "delinquent": False,
        "description": fields.get("description"),
        "email": fields.get("email"),
        "invoice_prefix": customer_id.removeprefix("cus_")[:8].upper(),
        "invoice_settings": {
            "custom_fields": None,
            "default_payment_method": None,
            "footer": None,
            "rendering_options": None,
        },
        "metadata": dict(fields.get("metadata") or {}),
        "name": fields.get("name"),
        "phone": fields.get("phone"),
        "preferred_locales": [],
        "shipping": None,
        "tax_exempt": "none",
    }


def payment_method_json(
    pm_id: str, created: int, card: CardSpec, *, customer: str | None, year: int
) -> dict[str, Any]:
    """A ``payment_method`` of type card."""
    return {
        **_base(pm_id, "payment_method", created),
        "allow_redisplay": "unspecified",
        "billing_details": {"address": None, "email": None, "name": None, "phone": None},
        "card": {
            "brand": card.brand,
            "checks": {"address_line1_check": None, "address_postal_code_check": None, "cvc_check": "pass"},
            "country": "US",
            "display_brand": card.brand,
            "exp_month": _EXP_MONTH,
            "exp_year": year + _EXP_YEARS_AHEAD,
            "funding": card.funding,
            "last4": card.last4,
            "networks": {"available": [card.brand], "preferred": None},
        },
        "customer": customer,
        "metadata": {},
        "type": "card",
    }


def payment_intent_json(  # noqa: PLR0913 - mirrors the create parameters
    pi_id: str,
    created: int,
    *,
    amount: int,
    currency: str,
    client_secret: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    """A ``payment_intent`` before confirmation (``status`` set by the caller)."""
    return {
        **_base(pi_id, "payment_intent", created),
        "amount": amount,
        "amount_capturable": 0,
        "amount_received": 0,
        "automatic_payment_methods": fields.get("automatic_payment_methods"),
        "canceled_at": None,
        "cancellation_reason": None,
        "capture_method": "automatic",
        "client_secret": client_secret,
        "confirmation_method": "automatic",
        "currency": currency,
        "customer": fields.get("customer"),
        "description": fields.get("description"),
        "last_payment_error": None,
        "latest_charge": None,
        "metadata": dict(fields.get("metadata") or {}),
        "next_action": None,
        "payment_method": fields.get("payment_method"),
        "payment_method_types": list(fields.get("payment_method_types") or ["card"]),
        "receipt_email": fields.get("receipt_email"),
        "setup_future_usage": fields.get("setup_future_usage"),
        "status": "requires_payment_method",
    }


def portal_json(
    session_id: str, created: int, *, customer: str, return_url: str | None, url: str
) -> dict[str, Any]:
    """A ``billing_portal.session``."""
    return {
        **_base(session_id, "billing_portal.session", created),
        "configuration": _PORTAL_CONFIGURATION,
        "customer": customer,
        "flow": None,
        "locale": None,
        "on_behalf_of": None,
        "return_url": return_url,
        "url": url,
    }


def event_json(  # noqa: PLR0913 - mirrors the event object
    event_id: str,
    created: int,
    *,
    event_type: str,
    data_object: Mapping[str, Any],
    api_version: str,
    request_id: str | None,
    idempotency_key: str | None,
    previous_attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """An ``event`` (``data.object`` must already be a snapshot)."""
    data: dict[str, Any] = {"object": dict(data_object)}
    if previous_attributes is not None:
        data["previous_attributes"] = dict(previous_attributes)
    return {
        **_base(event_id, "event", created),
        "api_version": api_version,
        "data": data,
        "pending_webhooks": 1,
        "request": {"id": request_id, "idempotency_key": idempotency_key},
        "type": event_type,
    }


def list_json(items: Sequence[Mapping[str, Any]], url: str, *, has_more: bool) -> dict[str, Any]:
    """A list object."""
    return {"object": "list", "data": [dict(item) for item in items], "has_more": has_more, "url": url}
