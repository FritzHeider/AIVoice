"""FakeStripe state (specs/30 §4.1 Decisions P03-T07a rules 1, 4, 6-11 and P03-T07b rule 8): accepted keys,
objects (customers, payments, catalog, checkout, subscriptions, invoices), the event log,
the idempotency cache, and the control config.

Objects are global to the simulator (Stripe accounts are not modelled). ``reset`` drops everything and
restores the config to the settings defaults.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Final

from va_sim.settings import StripeDefaults
from va_sim.stripe.objects import CardSpec

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_DECLINE_CODE",
    "DEFAULT_WEBHOOK_SECRET",
    "EVENT_LOG_SIZE",
    "IdempotencyEntry",
    "StripeConfig",
    "StripeStore",
]

# specs/02 §6 dev values of STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET.
DEFAULT_API_KEY: Final = "sk_test_sim"
DEFAULT_WEBHOOK_SECRET: Final = "whsec_sim"  # noqa: S105 - public simulator dev value
DEFAULT_DECLINE_CODE: Final = "generic_decline"
EVENT_LOG_SIZE: Final = 10_000


@dataclass(frozen=True, slots=True)
class StripeConfig:
    """``/sim/stripe/config``: ``decline`` is the specs/30 §4.1 ``SimState.stripe_decline`` switch."""

    decline: bool = False
    decline_code: str = DEFAULT_DECLINE_CODE
    webhook_url: str | None = None
    webhook_secret: str = DEFAULT_WEBHOOK_SECRET


@dataclass(slots=True)
class IdempotencyEntry:
    """A stored answer (``status`` None while the first request is still running)."""

    fingerprint: bytes
    status: int | None = None
    body: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""


class StripeStore:
    """Everything FakeStripe remembers between requests."""

    def __init__(self, defaults: StripeDefaults | None = None) -> None:
        self._defaults = defaults if defaults is not None else StripeDefaults()
        self.config = self._default_config()
        self.keys: set[str] = set()
        self.customers: dict[str, dict[str, Any]] = {}
        self.payment_methods: dict[str, dict[str, Any]] = {}
        self.cards: dict[str, CardSpec] = {}
        self.payment_intents: dict[str, dict[str, Any]] = {}
        self.portal_sessions: dict[str, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.checkout_sessions: dict[str, dict[str, Any]] = {}
        # Per checkout session: its line ``item`` objects and the payment_intent_data / subscription_data.
        self.checkout_items: dict[str, list[dict[str, Any]]] = {}
        self.checkout_options: dict[str, dict[str, Any]] = {}
        self.setup_intents: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        # Per subscription: proration lines waiting for the next invoice.
        self.pending_lines: dict[str, list[dict[str, Any]]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[tuple[str, str], IdempotencyEntry] = {}

    def _default_config(self) -> StripeConfig:
        return StripeConfig(webhook_url=self._defaults.webhook_url)

    def accepts(self, key: str) -> bool:
        return key == DEFAULT_API_KEY or key in self.keys

    def update_config(self, **changes: Any) -> StripeConfig:
        self.config = replace(self.config, **changes)
        return self.config

    def record_event(self, event: dict[str, Any]) -> None:
        """Log ``event``, forgetting the oldest beyond :data:`EVENT_LOG_SIZE`."""
        self.events[event["id"]] = event
        while len(self.events) > EVENT_LOG_SIZE:
            del self.events[next(iter(self.events))]

    def newest_events(self) -> Iterator[dict[str, Any]]:
        return reversed(self.events.values())

    def reset(self) -> None:
        self.config = self._default_config()
        self.keys.clear()
        self.customers.clear()
        self.payment_methods.clear()
        self.cards.clear()
        self.payment_intents.clear()
        self.portal_sessions.clear()
        for billing in (
            self.products,
            self.prices,
            self.checkout_sessions,
            self.checkout_items,
            self.checkout_options,
            self.setup_intents,
            self.subscriptions,
            self.pending_lines,
            self.invoices,
        ):
            billing.clear()
        self.events.clear()
        self.idempotency.clear()
