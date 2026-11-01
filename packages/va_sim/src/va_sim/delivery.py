"""Signed webhook delivery with retries, shared by the FakeTelnyx and FakeStripe emitters (specs/30 §3.2
Webhooks, §4.1).

The first attempt runs inline, so events of one resource arrive in the order they were emitted. A non-2xx
answer or a transport error is retried 1 s and then 3 s after the previous attempt on the sim clock, then the
event is dropped. Each attempt asks the emitter to render the body and headers again, so signatures carry the
sim-clock time of that attempt. Every attempt is appended to a bounded delivery log.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

import httpx

from va_common.logging import get_logger
from va_sim.clock import SimClock, iso_z
from va_sim.tasks import Supervisor

__all__ = ["DELIVERY_LOG_SIZE", "RETRY_DELAYS_S", "Render", "SignedDelivery", "WebhookDelivery"]

DELIVERY_LOG_SIZE: Final = 10_000
# Delay before attempt 2 and before attempt 3, measured from the previous attempt.
RETRY_DELAYS_S: Final = (1.0, 3.0)
_TIMEOUT: Final = httpx.Timeout(5.0)
_log: Final = get_logger(__name__)

# attempt number (1-based) -> (raw body, request headers) of that attempt.
type Render = Callable[[int], tuple[bytes, Mapping[str, str]]]


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One delivery attempt (``GET /sim/telnyx/webhooks``, ``GET /sim/stripe/webhooks``)."""

    event_id: str
    event_type: str
    url: str
    attempt: int
    status_code: int | None
    error: str | None
    delivered_at: str


@dataclass(frozen=True, slots=True)
class _Target:
    url: str
    event_id: str
    event_type: str
    render: Render


class SignedDelivery:
    """POSTs rendered webhook attempts, retrying failures in supervised background tasks.

    ``vendor`` names the log events (``sim.<vendor>_webhook_failed``, ``sim.<vendor>_webhook_dropped``) and
    the retry tasks.
    """

    def __init__(
        self,
        *,
        vendor: str,
        clock: SimClock,
        tasks: Supervisor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._vendor = vendor
        self._clock = clock
        self._tasks = tasks
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.deliveries: deque[WebhookDelivery] = deque(maxlen=DELIVERY_LOG_SIZE)

    async def send(
        self, *, url: str, event_id: str, event_type: str, render: Render, duplicate: bool = False
    ) -> None:
        """Deliver one event; ``duplicate`` repeats a successful first attempt once."""
        target = _Target(url, event_id, event_type, render)
        if await self._attempt(target, 1):
            if duplicate:
                await self._attempt(target, 1)
        else:
            self._tasks.spawn(self._retry(target), name=f"{self._vendor}-webhook-retry-{event_id}")

    async def _retry(self, target: _Target) -> None:
        for attempt, delay in enumerate(RETRY_DELAYS_S, start=2):
            await self._clock.sleep(delay)
            if await self._attempt(target, attempt):
                return
        _log.warning(
            f"sim.{self._vendor}_webhook_dropped", event_id=target.event_id, event_type=target.event_type
        )

    async def _attempt(self, target: _Target, attempt: int) -> bool:
        body, headers = target.render(attempt)
        status_code: int | None = None
        error: str | None = None
        try:
            response = await self._http().post(target.url, content=body, headers=dict(headers))
            status_code = response.status_code
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.deliveries.append(
            WebhookDelivery(
                event_id=target.event_id,
                event_type=target.event_type,
                url=target.url,
                attempt=attempt,
                status_code=status_code,
                error=error,
                delivered_at=iso_z(self._clock.now()),
            )
        )
        delivered = status_code is not None and 200 <= status_code < 300  # noqa: PLR2004 - HTTP 2xx range
        if not delivered:
            _log.info(
                f"sim.{self._vendor}_webhook_failed",
                event_id=target.event_id,
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
