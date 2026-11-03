"""One delivery attempt: the HTTP request, its classification, and the next state (specs/19 §8).

Error codes stored in ``webhook_deliveries.last_error`` (specs/19 §8 Decision P06-T03): ``redirect`` (3xx,
never followed), ``http_status`` (other non-2xx; the code is in ``last_status_code``), ``gone`` (410, dead at
once), ``timeout``, ``connection``, ``blocked_destination:<reason>`` (SSRF guard; ``scheme`` is dead at once,
the other reasons retry because DNS may change), ``endpoint_disabled`` (dead without a request), and
``secret_unavailable`` (retry).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx
import orjson
from opentelemetry import trace
from opentelemetry.trace import TracerProvider

from va_common.net import UnsafeDestinationError, UnsafeReason
from va_common.time import Clock
from va_db.models import WebhookDelivery
from va_webhooks.schedule import Timeouts, retry_delay_s, timeout_for
from va_webhooks.signing import request_headers

__all__ = [
    "CONNECTION",
    "ENDPOINT_DISABLED",
    "GONE",
    "HTTP_STATUS",
    "REDIRECT",
    "RESPONSE_READ_CAP",
    "SECRET_UNAVAILABLE",
    "TIMEOUT",
    "Deliverer",
    "Outcome",
    "Outgoing",
    "SendResult",
    "blocked",
    "decide",
]

REDIRECT: Final = "redirect"
HTTP_STATUS: Final = "http_status"
GONE: Final = "gone"
TIMEOUT: Final = "timeout"
CONNECTION: Final = "connection"
ENDPOINT_DISABLED: Final = "endpoint_disabled"
SECRET_UNAVAILABLE: Final = "secret_unavailable"  # noqa: S105 - an error code, not a secret
_BLOCKED_PREFIX: Final = "blocked_destination:"
_FINAL_ERRORS: Final = frozenset({GONE, ENDPOINT_DISABLED, f"{_BLOCKED_PREFIX}scheme"})
RESPONSE_READ_CAP: Final = 64 * 1024
_HTTP_GONE: Final = 410
_SPAN: Final = "webhook.deliver"


def blocked(reason: UnsafeReason) -> str:
    """The ``last_error`` code of an SSRF refusal."""
    return f"{_BLOCKED_PREFIX}{reason}"


@dataclass(frozen=True, slots=True)
class Outgoing:
    """A claimed delivery row, detached from its session; ``attempts`` counts earlier sends."""

    id: uuid.UUID
    org_id: uuid.UUID
    event_id: str
    event_type: str
    url: str
    endpoint_id: uuid.UUID | None
    payload: dict[str, Any]
    format: str
    attempts: int
    call_id: uuid.UUID | None
    endpoint_enabled: bool = True

    @classmethod
    def of(cls, row: WebhookDelivery, enabled: dict[uuid.UUID, bool]) -> Outgoing:
        """``row`` with its endpoint's ``enabled`` flag (a row of no or a deleted endpoint is enabled)."""
        return cls(
            id=row.id,
            org_id=row.org_id,
            event_id=row.event_id,
            event_type=row.event_type,
            url=row.url,
            endpoint_id=row.endpoint_id,
            payload=row.payload,
            format=row.format,
            attempts=row.attempts,
            call_id=row.call_id,
            endpoint_enabled=row.endpoint_id is None or enabled.get(row.endpoint_id, True),
        )

    @property
    def attempt(self) -> int:
        """The number of the attempt being made (1-based)."""
        return self.attempts + 1

    @property
    def host(self) -> str:
        """The URL's host, lower-cased (``""`` when it has none); the only part of the URL ever logged."""
        try:
            return (urlsplit(self.url).hostname or "").lower()
        except ValueError:
            return ""


@dataclass(frozen=True, slots=True)
class SendResult:
    """What one attempt produced; ``error`` is ``None`` exactly when it succeeded."""

    status_code: int | None
    error: str | None
    response_ms: int | None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class Outcome:
    """The row's next state after an attempt; ``delay_s``/``next_attempt_at`` are set for a retry."""

    status: Literal["succeeded", "retry", "dead"]
    next_attempt_at: datetime | None = None
    delay_s: float | None = None


def decide(attempt: int, result: SendResult, now: datetime, rng: random.Random) -> Outcome:
    """The next state of a delivery whose attempt number ``attempt`` produced ``result``."""
    if result.error is None:
        return Outcome("succeeded")
    if result.error in _FINAL_ERRORS:
        return Outcome("dead")
    delay = retry_delay_s(attempt, rng)
    if delay is None:
        return Outcome("dead")
    return Outcome("retry", next_attempt_at=now + timedelta(seconds=delay), delay_s=delay)


def _classify(status_code: int) -> str | None:
    if 200 <= status_code < 300:  # noqa: PLR2004 - the 2xx class
        return None
    if 300 <= status_code < 400:  # noqa: PLR2004 - the 3xx class
        return REDIRECT
    if status_code == _HTTP_GONE:
        return GONE
    return HTTP_STATUS


async def _read_capped(response: httpx.Response) -> None:
    """Read at most :data:`RESPONSE_READ_CAP` bytes of the body, then stop (the body is discarded)."""
    read = 0
    async for chunk in response.aiter_raw():
        read += len(chunk)
        if read >= RESPONSE_READ_CAP:
            return


class Deliverer:
    """Sends delivery requests through an SSRF-safe client (:func:`va_common.safe_http.safe_client`)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        clock: Clock,
        timeouts: Timeouts | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._http = http
        self._clock = clock
        self._timeouts = timeouts if timeouts is not None else Timeouts()
        self._tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)

    async def send(self, delivery: Outgoing, secrets: Sequence[str]) -> SendResult:
        """POST the signed payload once; never raises for network or HTTP failures."""
        body = orjson.dumps(delivery.payload)
        headers = request_headers(
            event_id=delivery.event_id,
            event_type=delivery.event_type,
            body=body,
            secrets=secrets,
            timestamp=int(self._clock.now().timestamp()),
        )
        timeout_s = timeout_for(delivery.event_type, self._timeouts)
        with self._tracer.start_as_current_span(_SPAN) as span:
            span.set_attribute("http.url", delivery.host)
            span.set_attribute("va.attempt", delivery.attempt)
            result = await self._post(delivery.url, body, headers, timeout_s)
            if result.status_code is not None:
                span.set_attribute("http.status_code", result.status_code)
            if result.error is not None:
                span.set_attribute("va.error", result.error)
        return result

    async def _post(self, url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> SendResult:
        started = self._clock.monotonic()
        try:
            async with (
                asyncio.timeout(timeout_s),
                self._http.stream("POST", url, content=body, headers=headers, timeout=timeout_s) as response,
            ):
                await _read_capped(response)
                status_code = response.status_code
        except (TimeoutError, httpx.TimeoutException):
            return SendResult(None, TIMEOUT, self._elapsed_ms(started))
        except UnsafeDestinationError as exc:
            return SendResult(None, blocked(exc.reason), None)
        except (httpx.TransportError, OSError):
            return SendResult(None, CONNECTION, None)
        return SendResult(status_code, _classify(status_code), self._elapsed_ms(started))

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock.monotonic() - started) * 1000))
