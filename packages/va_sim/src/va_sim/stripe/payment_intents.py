"""FakeStripe payment intents (specs/30 §4.1; Decision P03-T07a rule 8): ``POST /v1/payment_intents`` and
``GET /v1/payment_intents/{id}``.

``confirm=true`` charges at once: the payment method is the given one, else the customer's
``invoice_settings.default_payment_method``. The armed ``decline`` config (``SimState.stripe_decline``)
declines every charge with its ``decline_code``; otherwise the test card decides. Success → ``succeeded`` and
``payment_intent.succeeded``; a decline (or ``authentication_required`` off-session) → HTTP 402 ``card_error``
carrying the stored intent and ``payment_intent.payment_failed``; an authentication card on-session →
``requires_action`` (200, no event).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import StripeError, error_object, invalid_request, missing_param, resource_missing
from va_sim.stripe.events import emit_event
from va_sim.stripe.objects import CardSpec, payment_intent_json
from va_sim.stripe.params import Params, boolean, check_known, integer, mapping, metadata, string
from va_sim.stripe.payment_methods import require_customer, resolve_payment_method

if TYPE_CHECKING:
    from va_sim.stripe.http import StripeCall

__all__ = ["MINIMUM_AMOUNT", "create_payment_intent", "retrieve_payment_intent"]

MINIMUM_AMOUNT: Final = 50
_PI_ID_LENGTH: Final = 24
_CHARGE_ID_LENGTH: Final = 24
_SECRET_LENGTH: Final = 25
_CURRENCY: Final = re.compile(r"[a-z]{3}")
_OFF_SESSION_VALUES: Final = frozenset({"one_off", "recurring"})
_SETUP_FUTURE_USAGE: Final = frozenset({"on_session", "off_session"})
_PARAMS: Final = frozenset(
    {
        "amount",
        "automatic_payment_methods",
        "confirm",
        "currency",
        "customer",
        "description",
        "metadata",
        "off_session",
        "payment_method",
        "payment_method_types",
        "receipt_email",
        "return_url",
        "setup_future_usage",
    }
)
_AUTOMATIC_PARAMS: Final = frozenset({"enabled", "allow_redirects"})
_DECLINE_MESSAGES: Final[Mapping[str, str]] = {
    "insufficient_funds": "Your card has insufficient funds.",
    "expired_card": "Your card has expired.",
    "incorrect_cvc": "Your card's security code is incorrect.",
    "lost_card": "Your card was declined.",
    "stolen_card": "Your card was declined.",
}
_DECLINED: Final = "Your card was declined."
_AUTH_REQUIRED: Final = "Your card was declined. This transaction requires authentication."


def _amount(params: Params) -> int:
    amount = integer(params, "amount", required=True)
    assert amount is not None  # noqa: S101 - required=True
    if amount < MINIMUM_AMOUNT:
        msg = "Amount must be at least $0.50 usd"
        raise invalid_request(msg, code="amount_too_small", param="amount")
    return amount


def _currency(params: Params) -> str:
    raw = string(params, "currency", required=True)
    assert raw is not None  # noqa: S101 - required=True
    currency = raw.lower()
    if _CURRENCY.fullmatch(currency) is None:
        msg = f"Invalid currency: {raw}"
        raise invalid_request(msg, code="parameter_invalid_string", param="currency")
    return currency


def _off_session(params: Params) -> bool:
    raw = string(params, "off_session")
    if raw in _OFF_SESSION_VALUES:
        return True
    return bool(boolean(params, "off_session"))


def _optional_fields(params: Params) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in ("description", "receipt_email"):
        fields[name] = string(params, name)
    usage = string(params, "setup_future_usage")
    if usage is not None and usage not in _SETUP_FUTURE_USAGE:
        msg = f"Invalid setup_future_usage: {usage}"
        raise invalid_request(msg, param="setup_future_usage")
    fields["setup_future_usage"] = usage
    patch = metadata(params)
    fields["metadata"] = patch.apply({}) if patch is not None else {}
    types = params.get("payment_method_types")
    if types is not None:
        if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
            msg = "Invalid array: payment_method_types"
            raise invalid_request(msg, param="payment_method_types")
        fields["payment_method_types"] = types
    automatic = mapping(params, "automatic_payment_methods")
    if automatic is not None:
        check_known(automatic, _AUTOMATIC_PARAMS, prefix="automatic_payment_methods")
        enabled = boolean(automatic, "enabled")
        fields["automatic_payment_methods"] = {"enabled": bool(enabled)}
    return fields


def _new_id(call: StripeCall, prefix: str, length: int) -> str:
    return f"{prefix}{call.state.ids.alnum(length)}"


def _payment_method(call: StripeCall, customer_id: str | None, *, confirm: bool) -> dict[str, Any] | None:
    """The given payment method, else (when confirming) the customer's default; None when unconfirmed
    without one."""
    customer = require_customer(call.store, customer_id) if customer_id is not None else None
    pm_id = string(call.params, "payment_method")
    if pm_id is None and confirm and customer is not None:
        pm_id = customer["invoice_settings"]["default_payment_method"]
    if pm_id is None:
        if confirm:
            raise missing_param("payment_method")
        return None
    return resolve_payment_method(call, pm_id, customer=customer_id)


async def create_payment_intent(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _PARAMS)
    amount = _amount(params)
    currency = _currency(params)
    confirm = bool(boolean(params, "confirm"))
    off_session = _off_session(params)
    if off_session and not confirm:
        msg = "off_session can only be used when confirm=true."
        raise invalid_request(
            msg,
            code="parameter_invalid_empty",
            param="off_session",
        )
    fields = _optional_fields(params)
    customer_id = string(params, "customer")
    method = _payment_method(call, customer_id, confirm=confirm)
    pi_id = _new_id(call, "pi_", _PI_ID_LENGTH)
    intent = payment_intent_json(
        pi_id,
        call.now,
        amount=amount,
        currency=currency,
        client_secret=f"{pi_id}_secret_{call.state.ids.alnum(_SECRET_LENGTH)}",
        fields={**fields, "customer": customer_id, "payment_method": method["id"] if method else None},
    )
    if method is not None:
        intent["status"] = "requires_confirmation"
    call.store.payment_intents[pi_id] = intent
    if not confirm or method is None:
        return copy.deepcopy(intent)
    return await _confirm(call, intent, method, off_session=off_session)


def _outcome(call: StripeCall, card: CardSpec, *, off_session: bool) -> tuple[str, str | None]:
    """(``succeed`` | ``decline`` | ``authenticate``, decline code)."""
    config = call.store.config
    if config.decline:
        return "decline", config.decline_code
    if card.behaviour == "authenticate" and off_session:
        return "decline", "authentication_required"
    return card.behaviour, card.decline_code


async def _confirm(
    call: StripeCall, intent: dict[str, Any], method: Mapping[str, Any], *, off_session: bool
) -> dict[str, Any]:
    card = call.store.cards[method["id"]]
    outcome, decline_code = _outcome(call, card, off_session=off_session)
    if outcome == "authenticate":
        intent["status"] = "requires_action"
        intent["next_action"] = {
            "type": "use_stripe_sdk",
            "use_stripe_sdk": {"type": "three_d_secure_redirect"},
        }
        return copy.deepcopy(intent)
    charge_id = _new_id(call, "ch_", _CHARGE_ID_LENGTH)
    intent["latest_charge"] = charge_id
    if outcome == "succeed":
        intent["status"] = "succeeded"
        intent["amount_received"] = intent["amount"]
        await emit_event(call.state, "payment_intent.succeeded", intent, call=call)
        return copy.deepcopy(intent)
    code = "authentication_required" if decline_code == "authentication_required" else "card_declined"
    message = (
        _AUTH_REQUIRED
        if code == "authentication_required"
        else _DECLINE_MESSAGES.get(decline_code or "", _DECLINED)
    )
    error = error_object("card_error", message, code=code, decline_code=decline_code)
    error.update({"charge": charge_id, "payment_method": copy.deepcopy(dict(method))})
    intent["status"] = "requires_payment_method"
    intent["last_payment_error"] = error
    await emit_event(call.state, "payment_intent.payment_failed", intent, call=call)
    raise StripeError(
        402,
        "card_error",
        message,
        code=code,
        decline_code=decline_code,
        extra={
            "charge": charge_id,
            "payment_method": copy.deepcopy(dict(method)),
            "payment_intent": copy.deepcopy(intent),
        },
    )


async def retrieve_payment_intent(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset({"client_secret"}))
    pi_id = call.path_params["pi_id"]
    intent = call.store.payment_intents.get(pi_id)
    if intent is None:
        raise resource_missing("payment_intent", pi_id)
    return copy.deepcopy(intent)
