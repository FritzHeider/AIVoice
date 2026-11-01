"""The card-charge decision shared by payment intents, invoices, and checkout (specs/30 §4.1 Decisions
P03-T07a rule 8, P03-T07b rules 4-7), plus seeded object ids.

The armed ``decline`` config (``SimState.stripe_decline``) declines every charge with its ``decline_code``;
a forced decline (renewal control ``fail: true``) declines with ``generic_decline``; an authentication card
charged off-session declines with ``authentication_required``; otherwise the test card decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import StripeError, error_object
from va_sim.stripe.store import DEFAULT_DECLINE_CODE, StripeConfig

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.objects import CardBehaviour, CardSpec

__all__ = ["ChargeDecision", "decide", "new_id"]

_AUTHENTICATION_REQUIRED: Final = "authentication_required"
_DECLINE_MESSAGES: Final[Mapping[str, str]] = {
    "insufficient_funds": "Your card has insufficient funds.",
    "expired_card": "Your card has expired.",
    "incorrect_cvc": "Your card's security code is incorrect.",
    "lost_card": "Your card was declined.",
    "stolen_card": "Your card was declined.",
}
_DECLINED: Final = "Your card was declined."
_AUTH_REQUIRED: Final = "Your card was declined. This transaction requires authentication."


def new_id(state: SimState, prefix: str, length: int) -> str:
    """``prefix`` + ``length`` seeded alphanumerics."""
    return f"{prefix}{state.ids.alnum(length)}"


@dataclass(frozen=True, slots=True)
class ChargeDecision:
    """How a charge ends: ``succeed``, ``decline`` (with a decline code), or ``authenticate`` (on-session
    3-D Secure)."""

    outcome: CardBehaviour
    decline_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeed"

    @property
    def code(self) -> str:
        return _AUTHENTICATION_REQUIRED if self.decline_code == _AUTHENTICATION_REQUIRED else "card_declined"

    @property
    def message(self) -> str:
        if self.code == _AUTHENTICATION_REQUIRED:
            return _AUTH_REQUIRED
        return _DECLINE_MESSAGES.get(self.decline_code or "", _DECLINED)

    def error_object(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The ``card_error`` object (a payment intent's ``last_payment_error``)."""
        error = error_object("card_error", self.message, code=self.code, decline_code=self.decline_code)
        error.update(extra or {})
        return error

    def error(self, extra: Mapping[str, Any] | None = None) -> StripeError:
        """The HTTP 402 ``card_error`` answer."""
        return StripeError(
            402, "card_error", self.message, code=self.code, decline_code=self.decline_code, extra=extra
        )


def decide(
    config: StripeConfig, card: CardSpec, *, off_session: bool, force_decline: bool = False
) -> ChargeDecision:
    """The outcome of charging ``card`` now."""
    if force_decline:
        return ChargeDecision("decline", DEFAULT_DECLINE_CODE)
    if config.decline:
        return ChargeDecision("decline", config.decline_code)
    if card.behaviour == "authenticate" and off_session:
        return ChargeDecision("decline", _AUTHENTICATION_REQUIRED)
    return ChargeDecision(card.behaviour, card.decline_code)
