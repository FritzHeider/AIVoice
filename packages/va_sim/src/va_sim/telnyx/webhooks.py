"""FakeTelnyx webhook emitter (specs/30-testing-and-evals.md §3.2 Webhooks; specs/06-telephony-telnyx.md
§4.1).

Envelope ``{"data": {"record_type": "event", "id", "event_type", "occurred_at", "payload"}, "meta":
{"attempt", "delivered_to"}}``, signed per attempt with ``telnyx-signature-ed25519`` over ``"{timestamp}|{raw
body}"`` and ``telnyx-timestamp`` (sim clock, unix seconds). The first attempt runs inline so events of one
resource arrive in order; a non-2xx answer or a transport error is retried 1 s and then 3 s later on the sim
clock (same event id, ``meta.attempt`` 2 and 3), then the event is dropped. With ``force_duplicate_webhooks``
every successful first attempt is sent a second time.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
import orjson

from va_common.logging import get_logger
from va_sim.clock import SimClock, iso_z
from va_sim.ids import SimIds
from va_sim.tasks import Supervisor
from va_sim.telnyx.keys import SigningKey
from va_sim.telnyx.models import TelnyxSimConfig

__all__ = ["DELIVERY_LOG_SIZE", "RETRY_DELAYS_S", "WebhookDelivery", "WebhookEmitter"]

DELIVERY_LOG_SIZE: Final = 10_000
# Delay before attempt 2 and before attempt 3, measured from the previous attempt.
RETRY_DELAYS_S: Final = (1.0, 3.0)
_TIMEOUT: Final = httpx.Timeout(5.0)
_log: Final = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One delivery attempt (``GET /sim/telnyx/webhooks``)."""

    event_id: str
    event_type: str
    url: str
    attempt: int
    status_code: int | None
    error: str | None
    delivered_at: str


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
        self._tasks = tasks
        self._config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.deliveries: deque[WebhookDelivery] = deque(maxlen=DELIVERY_LOG_SIZE)

    async def emit(self, url: str, event_type: str, payload: Mapping[str, Any]) -> str:
        """Deliver ``event_type`` with ``payload`` to ``url``; returns the event id."""
        # Snapshot the payload: the resource may change before a retry re-sends this event.
        event = _Event(
            url, self._ids.uuid(), event_type, iso_z(self._clock.now()), copy.deepcopy(dict(payload))
        )
        if await self._attempt(event, 1):
            if self._config().force_duplicate_webhooks:
                await self._attempt(event, 1)
        else:
            self._tasks.spawn(self._retry(event), name=f"telnyx-webhook-retry-{event.event_id}")
        return event.event_id

    async def _retry(self, event: _Event) -> None:
        for attempt, delay in enumerate(RETRY_DELAYS_S, start=2):
            await self._clock.sleep(delay)
            if await self._attempt(event, attempt):
                return
        _log.warning("sim.telnyx_webhook_dropped", event_id=event.event_id, event_type=event.event_type)

    def _body(self, event: _Event, attempt: int) -> bytes:
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

    async def _attempt(self, event: _Event, attempt: int) -> bool:
        body = self._body(event, attempt)
        headers = {
            "content-type": "application/json",
            **self._signing_key.signature_headers(int(self._clock.now().timestamp()), body),
        }
        status_code: int | None = None
        error: str | None = None
        try:
            response = await self._http().post(event.url, content=body, headers=headers)
            status_code = response.status_code
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.deliveries.append(
            WebhookDelivery(
                event_id=event.event_id,
                event_type=event.event_type,
                url=event.url,
                attempt=attempt,
                status_code=status_code,
                error=error,
                delivered_at=iso_z(self._clock.now()),
            )
        )
        delivered = status_code is not None and 200 <= status_code < 300  # noqa: PLR2004 - HTTP 2xx range
        if not delivered:
            _log.info(
                "sim.telnyx_webhook_failed",
                event_id=event.event_id,
                attempt=attempt,
                status_code=status_code,
                error=error,
            )
        return delivered

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)
        return self._client

    def clear(self) -> None:
        """Forget the delivery log (``/sim/reset``; pending retries are cancelled by the supervisor)."""
        self.deliveries.clear()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
