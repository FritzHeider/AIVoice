"""The Telnyx request pipeline (specs/06-telephony-telnyx.md §2, §2.2; specs/27-observability.md §2, §3.6).

One logical operation = rate-limit token → request attempts (Bearer auth per client) → error mapping. Retry
rules (Decision 2026-11-01 P03-T08a): connection failures where no bytes were sent (``ConnectError``,
``ConnectTimeout``, ``PoolTimeout``) are retried for every operation; other transport failures and the
statuses ``429``/``500``/``502``/``503``/``504`` are retried only for idempotent operations (call commands
carrying ``command_id``, GET, PATCH, DELETE). Each operation records one ``telnyx.<operation>`` CLIENT span
and one metrics observation (a paginated list or a download counts as one operation).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote

import anyio
import httpx
import orjson
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from pydantic import BaseModel, ValidationError

from va_common.logging import get_logger
from va_common.time import Clock
from va_telnyx.errors import (
    CALL_ENDED_CODE,
    CallAlreadyEnded,
    TelnyxApiError,
    TelnyxDownloadTooLarge,
    TelnyxError,
    TelnyxErrorItem,
    TelnyxRateLimited,
    TelnyxTransportError,
    parse_error_body,
)
from va_telnyx.metrics import Outcome, TelnyxMetrics
from va_telnyx.rate_limit import RateCategory, TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy, retry_after_seconds

__all__ = ["MAX_PAGES", "PAGE_SIZE", "Operation", "TelnyxTransport", "parse_data", "parse_items", "segment"]

type Method = Literal["GET", "POST", "PATCH", "DELETE"]

_NO_BYTES_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_HTTP_NOT_FOUND = 404
_HTTP_UNPROCESSABLE = 422
_ERROR_BODY_LIMIT: Final = 64 * 1024
PAGE_SIZE: Final = 250
MAX_PAGES: Final = 20

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Operation:
    """A Telnyx API operation: metric/span name, HTTP method, rate-limit category, and retry/mapping rules.

    ``call_command`` marks commands addressed to an existing call (``/calls/{ccid}/actions/*``): a ``404``
    there means the call is gone (:class:`CallAlreadyEnded`).
    """

    name: str
    method: Method
    category: RateCategory | None
    idempotent: bool
    call_command: bool = False


def parse_data[M: BaseModel](operation: Operation, envelope: Mapping[str, Any], model: type[M]) -> M:
    """``envelope["data"]`` as ``model``; a success body of the wrong shape is a :class:`TelnyxApiError`."""
    try:
        return model.model_validate(envelope.get("data"))
    except ValidationError as exc:
        item = TelnyxErrorItem("invalid_response", "Unexpected Telnyx response body", str(exc.errors()[:1]))
        raise TelnyxApiError(200, (item,), operation.name) from exc


def parse_items[M: BaseModel](operation: Operation, envelope: Mapping[str, Any], model: type[M]) -> list[M]:
    """Each item of the list ``envelope["data"]`` as ``model`` (``invalid_response`` when it is no list)."""
    raw_items = envelope.get("data")
    if not isinstance(raw_items, list):
        item = TelnyxErrorItem("invalid_response", "Telnyx list response has no data array")
        raise TelnyxApiError(200, (item,), operation.name)
    return [parse_data(operation, {"data": raw}, model) for raw in raw_items]


def segment(resource_id: str) -> str:
    """A path segment for a Telnyx id (``v3:…`` call control ids keep their colon)."""
    return quote(resource_id, safe=":")


def _is_absolute(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def _total_pages(envelope: Mapping[str, Any]) -> int:
    meta = envelope.get("meta")
    total = meta.get("total_pages") if isinstance(meta, Mapping) else None
    return total if isinstance(total, int) else 1


def _outcome(error: TelnyxError | None) -> Outcome:
    match error:
        case None:
            return "ok"
        case CallAlreadyEnded():
            return "already_ended"
        case TelnyxRateLimited():
            return "rate_limited"
        case TelnyxTransportError():
            return "transport_error"
        case _:
            return "carrier_error"


def _mapped_error(operation: Operation, response: httpx.Response) -> TelnyxApiError:
    errors = parse_error_body(response.status_code, response.reason_phrase, response.content)
    ended = (
        response.status_code == _HTTP_UNPROCESSABLE and any(e.code == CALL_ENDED_CODE for e in errors)
    ) or (response.status_code == _HTTP_NOT_FOUND and operation.call_command)
    if ended:
        return CallAlreadyEnded(response.status_code, errors, operation.name)
    return TelnyxApiError(response.status_code, errors, operation.name)


def _success_body(operation: Operation, response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        document = orjson.loads(response.content)
    except orjson.JSONDecodeError:
        document = None
    if not isinstance(document, dict):
        item = TelnyxErrorItem("invalid_response", "Telnyx response is not a JSON object")
        raise TelnyxApiError(response.status_code, (item,), operation.name)
    return document


@dataclass(frozen=True, slots=True)
class _Request:
    operation: Operation
    url: str
    json: Mapping[str, Any] | None
    params: Mapping[str, str | list[str]] | None
    authenticated: bool


class TelnyxTransport:
    """Sends Telnyx requests for one API key and account; never shows the key in ``repr``."""

    def __init__(  # noqa: PLR0913 - collaborators are injected for tests (clock, rng, metrics, tracing)
        self,
        *,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        limiter: TelnyxRateLimiter,
        account: str,
        clock: Clock,
        retry: RetryPolicy,
        rng: random.Random,
        metrics: TelnyxMetrics | None,
        tracer_provider: TracerProvider | None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._limiter = limiter
        self.account = account
        self._clock = clock
        self._retry = retry
        self._rng = rng
        self._metrics = metrics
        self._tracer = trace.get_tracer("va_telnyx", tracer_provider=tracer_provider)

    def __repr__(self) -> str:
        return f"TelnyxTransport(base_url={self.base_url!r}, account={self.account!r})"

    async def request(  # noqa: PLR0913 - one keyword per request part
        self,
        operation: Operation,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | list[str]] | None = None,
        call_control_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Run ``operation`` against ``base_url + path`` (or ``path`` itself when absolute, sent without the
        auth header); returns the parsed success body (``{}`` when empty)."""
        absolute = _is_absolute(path)
        request = _Request(operation, path if absolute else self.base_url + path, json, params, not absolute)
        attributes: dict[str, str] = {"telnyx.operation": operation.name}
        if call_control_id is not None:
            attributes["telnyx.call_control_id"] = call_control_id
        if command_id is not None:
            attributes["telnyx.command_id"] = command_id

        return await self._observed(operation, attributes, lambda span: self._exchange(request, span))

    async def request_pages[M: BaseModel](
        self,
        operation: Operation,
        path: str,
        model: type[M],
        *,
        params: Mapping[str, str | list[str]] | None = None,
    ) -> list[M]:
        """Every item of a paginated list (Decision 2026-11-01 P03-T08b rule 2): ``page[size]=250``, pages
        followed through ``meta.total_pages``, at most :data:`MAX_PAGES` pages."""
        base = {**(params or {}), "page[size]": str(PAGE_SIZE)}

        async def work(span: Span) -> list[M]:
            items: list[M] = []
            number = 1
            while True:
                query = {**base, "page[number]": str(number)}
                request = _Request(operation, self.base_url + path, None, query, authenticated=True)
                envelope = await self._exchange(request, span)
                items.extend(parse_items(operation, envelope, model))
                if number >= min(_total_pages(envelope), MAX_PAGES):
                    return items
                number += 1

        return await self._observed(operation, {"telnyx.operation": operation.name}, work)

    async def download(self, operation: Operation, url: str, destination: Path, *, max_bytes: int) -> int:
        """Stream the absolute ``url`` (no auth header) into ``destination``; returns the bytes written.

        Retried like ``operation`` allows (the file restarts empty on each attempt). ``destination`` is
        removed when the download fails, including :class:`TelnyxDownloadTooLarge` past ``max_bytes``.
        """
        if not _is_absolute(url):
            msg = f"download needs an absolute URL: {url!r}"
            raise ValueError(msg)
        request = _Request(operation, url, None, None, authenticated=False)
        download = _Download(self, request, destination, max_bytes)

        async def work(span: Span) -> int:
            response = await self._send_with_retries(request, span, download.attempt)
            if not response.is_success:
                raise _mapped_error(operation, response)
            return download.written

        try:
            return await self._observed(operation, {"telnyx.operation": operation.name}, work)
        except BaseException:
            await anyio.Path(destination).unlink(missing_ok=True)
            raise

    async def _observed[T](
        self, operation: Operation, attributes: Mapping[str, str], work: Callable[[Span], Awaitable[T]]
    ) -> T:
        """``work`` inside the operation's span and metrics observation, after its rate-limit token."""
        started = self._clock.monotonic()
        error: TelnyxError | None = None
        with self._tracer.start_as_current_span(
            f"telnyx.{operation.name}", kind=SpanKind.CLIENT, attributes=attributes, record_exception=False
        ) as span:
            try:
                if operation.category is not None:
                    await self._limiter.acquire(self.account, operation.category, operation=operation.name)
                return await work(span)
            except TelnyxError as exc:
                error = exc
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                if self._metrics is not None:
                    duration_ms = (self._clock.monotonic() - started) * 1000
                    self._metrics.observe(operation.name, _outcome(error), duration_ms)

    def headers(self, request: _Request) -> dict[str, str]:
        """``Accept`` plus the Bearer header for requests to the API base."""
        headers = {"Accept": "application/json"}
        if request.authenticated:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def _exchange(self, request: _Request, span: Span) -> dict[str, Any]:
        """One request with its retries: the parsed success body, or the mapped error."""
        response = await self._send_with_retries(request, span, lambda: self._attempt(request))
        if not response.is_success:
            raise _mapped_error(request.operation, response)
        return _success_body(request.operation, response)

    async def _attempt(self, request: _Request) -> httpx.Response:
        return await self._http.request(
            request.operation.method,
            request.url,
            json=request.json,
            params=request.params,
            headers=self.headers(request),
        )

    def _retry_delay(
        self, request: _Request, retry_index: int, outcome: httpx.Response | httpx.TransportError
    ) -> float | None:
        """Seconds before the next attempt, or ``None`` when ``outcome`` is final."""
        idempotent = request.operation.idempotent
        if isinstance(outcome, httpx.TransportError):
            if not (idempotent or isinstance(outcome, _NO_BYTES_SENT)):
                return None
            return self._retry.delay(retry_index, self._rng)
        if outcome.is_success or not idempotent or outcome.status_code not in self._retry.retry_statuses:
            return None
        retry_after = retry_after_seconds(outcome.headers.get("retry-after"), self._clock.now())
        return self._retry.delay(retry_index, self._rng, retry_after)

    async def _send_with_retries(
        self, request: _Request, span: Span, attempt: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        retry_index = 0
        while True:
            span.set_attribute("telnyx.attempts", retry_index + 1)
            outcome: httpx.Response | httpx.TransportError
            try:
                outcome = await attempt()
            except httpx.TransportError as exc:
                outcome = exc
            else:
                span.set_attribute("telnyx.status_code", outcome.status_code)
            delay = self._retry_delay(request, retry_index, outcome)
            if delay is None:
                if isinstance(outcome, httpx.TransportError):
                    raise TelnyxTransportError(request.operation.name, outcome) from outcome
                return outcome
            _log.info(
                "telnyx_request_retry",
                operation=request.operation.name,
                attempt=retry_index + 1,
                delay_ms=round(delay * 1000),
                reason=type(outcome).__name__ if isinstance(outcome, Exception) else outcome.status_code,
            )
            await self._clock.sleep(delay)
            retry_index += 1


class _Download:
    """One download's attempts: each streams the body into a freshly truncated file."""

    def __init__(
        self, transport: TelnyxTransport, request: _Request, destination: Path, max_bytes: int
    ) -> None:
        self._transport = transport
        self._request = request
        self._destination = destination
        self._max_bytes = max_bytes
        self.written = 0

    async def attempt(self) -> httpx.Response:
        request = self._request
        async with self._transport.http.stream(
            "GET", request.url, headers=self._transport.headers(request)
        ) as response:
            if not response.is_success:
                return await _error_response(response)
            self.written = 0
            async with await anyio.open_file(self._destination, "wb") as file:
                async for chunk in response.aiter_bytes():
                    self.written += len(chunk)
                    if self.written > self._max_bytes:
                        raise TelnyxDownloadTooLarge(request.operation.name, self._max_bytes)
                    await file.write(chunk)
            return response


async def _error_response(response: httpx.Response) -> httpx.Response:
    """A buffered copy of an error response holding at most the first 64 KiB of its body."""
    collected = bytearray()
    async for chunk in response.aiter_bytes():
        collected.extend(chunk)
        if len(collected) >= _ERROR_BODY_LIMIT:
            break
    headers = {name: value for name, value in response.headers.items() if name.lower() == "retry-after"}
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=bytes(collected[:_ERROR_BODY_LIMIT]),
        request=response.request,
    )
