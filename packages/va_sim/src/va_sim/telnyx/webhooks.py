"""FakeTelnyx webhook emitter (specs/30-testing-and-evals.md §3.2 Webhooks; specs/06-telephony-telnyx.md
§4.1).

Envelope ``{"data": {"record_type": "event", "id", "event_type", "occurred_at", "payload"}, "meta":
{"attempt", "delivered_to"}}``, signed per attempt with ``telnyx-signature-ed25519`` over ``"{timestamp}|{raw
body}"`` and ``telnyx-timestamp`` (sim clock, unix seconds). Delivery and retries (1 s, then 3 s, then the
event is dropped) are :class:`va_sim.delivery.SignedDelivery`; with ``force_duplicate_webhooks`` every
successful first attempt is sent a second time.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import orjson

from va_sim.clock import SimClock, iso_z
from va_sim.delivery import DELIVERY_LOG_SIZE, RETRY_DELAYS_S, SignedDelivery, WebhookDelivery
from va_sim.ids import SimIds
from va_sim.tasks import Supervisor
from va_sim.telnyx.keys import SigningKey
from va_sim.telnyx.models import TelnyxSimConfig

__all__ = ["DELIVERY_LOG_SIZE", "RETRY_DELAYS_S", "WebhookDelivery", "WebhookEmitter"]


@dataclass(frozen=True, slots=True)
class _Event:
    url: str
    event_id: str
    event_type: str
    occurred_at: str
    payload: Mapping[str, Any]


class WebhookEmitter:
    """Signs and POSTs FakeTelnyx webhooks, retrying failures in supervised background tasks."""

    def __init__(  # noqa: PLR0913 - collaborators injected by SimState
        self,
        *,
        signing_key: SigningKey,
        clock: SimClock,
        ids: SimIds,
        tasks: Supervisor,
        config: Callable[[], TelnyxSimConfig],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._signing_key = signing_key
        self._clock = clock
        self._ids = ids
        self._config = config
        self._delivery = SignedDelivery(vendor="telnyx", clock=clock, tasks=tasks, transport=transport)

    @property
    def deliveries(self) -> deque[WebhookDelivery]:
        """Every delivery attempt, oldest first (bounded)."""
        return self._delivery.deliveries

    async def emit(self, url: str, event_type: str, payload: Mapping[str, Any]) -> str:
        """Deliver ``event_type`` with ``payload`` to ``url``; returns the event id."""
        # Snapshot the payload: the resource may change before a retry re-sends this event.
        event = _Event(
            url, self._ids.uuid(), event_type, iso_z(self._clock.now()), copy.deepcopy(dict(payload))
        )

        def render(attempt: int) -> tuple[bytes, Mapping[str, str]]:
            body = self._body(event, attempt)
            headers = {
                "content-type": "application/json",
                **self._signing_key.signature_headers(int(self._clock.now().timestamp()), body),
            }
            return body, headers

        await self._delivery.send(
            url=url,
            event_id=event.event_id,
            event_type=event_type,
            render=render,
            duplicate=self._config().force_duplicate_webhooks,
        )
        return event.event_id

    @staticmethod
    def _body(event: _Event, attempt: int) -> bytes:
        return orjson.dumps(
            {
                "data": {
                    "record_type": "event",
                    "id": event.event_id,
                    "event_type": event.event_type,
                    "occurred_at": event.occurred_at,
                    "payload": event.payload,
                },
                "meta": {"attempt": attempt, "delivered_to": event.url},
            }
        )

    def clear(self) -> None:
        """Forget the delivery log (``/sim/reset``; pending retries are cancelled by the supervisor)."""
        self._delivery.clear()

    async def aclose(self) -> None:
        await self._delivery.aclose()
