"""FakeStripe webhook emitter (specs/30 §4.1; Decision P03-T07a rule 10).

The event JSON is POSTed to the configured ``webhook_url`` with ``Stripe-Signature`` computed per attempt at
the sim-clock time of that attempt (:mod:`va_sim.stripe.signing`); retries and the delivery log are
:class:`va_sim.delivery.SignedDelivery`. A ``webhook_url`` of None disables delivery.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Final

import httpx
import orjson

from va_sim.clock import SimClock
from va_sim.delivery import SignedDelivery, WebhookDelivery
from va_sim.stripe.signing import signature_header
from va_sim.stripe.store import StripeConfig
from va_sim.tasks import Supervisor

__all__ = ["StripeWebhookEmitter"]

_CONTENT_TYPE: Final = "application/json; charset=utf-8"
_USER_AGENT: Final = "Stripe/1.0 (+https://stripe.com/docs/webhooks)"


class StripeWebhookEmitter:
    """Signs and POSTs FakeStripe events."""

    def __init__(
        self,
        *,
        clock: SimClock,
        tasks: Supervisor,
        config: Callable[[], StripeConfig],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._config = config
        self._delivery = SignedDelivery(vendor="stripe", clock=clock, tasks=tasks, transport=transport)

    @property
    def deliveries(self) -> deque[WebhookDelivery]:
        """Every delivery attempt, oldest first (bounded)."""
        return self._delivery.deliveries

    async def emit(self, event: Mapping[str, Any]) -> None:
        """Deliver ``event`` (an already snapshotted event object) unless delivery is disabled."""
        config = self._config()
        if config.webhook_url is None:
            return
        body = orjson.dumps(event)
        secret = config.webhook_secret

        def render(_attempt: int) -> tuple[bytes, Mapping[str, str]]:
            timestamp = int(self._clock.now().timestamp())
            return body, {
                "content-type": _CONTENT_TYPE,
                "user-agent": _USER_AGENT,
                "stripe-signature": signature_header(secret, timestamp, body),
            }

        await self._delivery.send(
            url=config.webhook_url, event_id=event["id"], event_type=event["type"], render=render
        )

    def clear(self) -> None:
        self._delivery.clear()

    async def aclose(self) -> None:
        await self._delivery.aclose()
