"""The ElevenLabs REST request pipeline (Decision 2026-11-01 P03-T12 rules 1, 3, 7; specs/27 §2, §3.6).

One logical operation = attempts with the operation's timeout and retry schedule (``xi-api-key`` header) →
error mapping. Each operation records one ``elevenlabs.<operation>`` CLIENT span (``elevenlabs.operation``,
``elevenlabs.voice_id`` when present, ``http.status_code`` of the last response, ``elevenlabs.attempts``) and
one metrics observation whose duration includes the retry waits.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx
import orjson
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from pydantic import BaseModel, ValidationError

from va_common.http import build_timeout, retry_after_seconds
from va_common.logging import get_logger
from va_common.time import Clock
from va_elevenlabs.rest_errors import (
    ElevenLabsApiError,
    ElevenLabsAuthError,
    ElevenLabsError,
    ElevenLabsProtocolError,
    ElevenLabsRateLimitedError,
    ElevenLabsTransportError,
    api_error,
)
from va_elevenlabs.rest_metrics import ElevenLabsRestMetrics, RestOutcome
from va_elevenlabs.rest_retry import RestRetry

__all__ = [
    "API_KEY_HEADER",
    "ElevenLabsTransport",
    "Exchange",
    "FormPart",
    "RestOperation",
    "RestResponse",
    "segment",
]

type Method = Literal["GET", "POST", "DELETE"]
# An httpx multipart part: a text field ``(None, value)`` or a file ``(filename, data, content_type)``.
type FormPart = tuple[str, tuple[None, str] | tuple[str, bytes, str]]

API_KEY_HEADER: Final = "xi-api-key"
_JSON: Final = "application/json"
_HTTP_SERVER: Final = 500

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RestOperation:
    """A REST operation: metric/span name, HTTP method, total timeout, and retry schedule."""

    name: str
    method: Method
    timeout_s: float
    retry: RestRetry


@dataclass(frozen=True, slots=True)
class RestResponse:
    """A successful response: status, headers, and the raw body."""

    status: int
    headers: httpx.Headers
    content: bytes

    def json_object(self, operation: RestOperation) -> dict[str, Any]:
        """The body as a JSON object; :class:`ElevenLabsProtocolError` otherwise."""
        try:
            document = orjson.loads(self.content)
        except orjson.JSONDecodeError:
            document = None
        if not isinstance(document, dict):
            raise ElevenLabsProtocolError(operation.name, "the body is not a JSON object")
        return document


def no_body(response: RestResponse) -> None:
    """The parser of operations whose success body carries nothing the caller needs."""
    del response


def _failed(span: Span, error: ElevenLabsError) -> None:
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))


def parse_model[M: BaseModel](operation: RestOperation, model: type[M], document: object) -> M:
    """``document`` as ``model``; :class:`ElevenLabsProtocolError` naming the first invalid location."""
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"]) or "body"
        raise ElevenLabsProtocolError(operation.name, f"{where}: {error['msg']}") from exc


def segment(resource_id: str) -> str:
    """A URL path segment for a vendor id (``ValueError`` for an empty id)."""
    if not resource_id:
        msg = "an ElevenLabs id must not be empty"
        raise ValueError(msg)
    return quote(resource_id, safe="")


def _outcome(error: ElevenLabsError | None) -> RestOutcome:
    match error:
        case None:
            return "ok"
        case ElevenLabsAuthError():
            return "auth_error"
        case ElevenLabsRateLimitedError():
            return "rate_limited"
        case ElevenLabsTransportError():
            return "transport_error"
        case ElevenLabsApiError(status=status) if status >= _HTTP_SERVER:
            return "server_error"
        case _:
            return "client_error"


@dataclass(frozen=True, slots=True)
class _Request:
    operation: RestOperation
    url: str
    json: Mapping[str, Any] | None
    params: Mapping[str, str | Sequence[str]] | None
    files: Sequence[FormPart] | None
    accept: str


class ElevenLabsTransport:
    """Sends ElevenLabs REST requests for one API key; never shows the key in ``repr``."""

    def __init__(  # noqa: PLR0913 - collaborators are injected for tests (clock, rng, metrics, tracing)
        self,
        *,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        clock: Clock,
        rng: random.Random,
        metrics: ElevenLabsRestMetrics | None,
        tracer_provider: TracerProvider | None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._clock = clock
        self._rng = rng
        self._metrics = metrics
        self._tracer = trace.get_tracer("va_elevenlabs", tracer_provider=tracer_provider)

    def __repr__(self) -> str:
        return f"ElevenLabsTransport(base_url={self.base_url!r})"

    async def sleep(self, seconds: float) -> None:
        await self._clock.sleep(seconds)

    async def observed[T](
        self,
        operation: RestOperation,
        work: Callable[[Exchange], Awaitable[T]],
        *,
        voice_id: str | None = None,
    ) -> T:
        """``work`` as one logical operation: one span and one metrics observation, however many requests it
        sends through its :class:`Exchange` (a paginated list counts once)."""
        attributes = {"elevenlabs.operation": operation.name}
        if voice_id is not None:
            attributes["elevenlabs.voice_id"] = voice_id
        started = self._clock.monotonic()
        error: ElevenLabsError | None = None
        with self._tracer.start_as_current_span(
            f"elevenlabs.{operation.name}",
            kind=SpanKind.CLIENT,
            attributes=attributes,
            record_exception=False,
        ) as span:
            try:
                return await work(Exchange(self, operation, span))
            except ElevenLabsError as exc:
                error = exc
                _failed(span, exc)
                raise
            finally:
                self._observe(operation, started, error)

    def _observe(self, operation: RestOperation, started: float, error: ElevenLabsError | None) -> None:
        if self._metrics is None:
            return
        elapsed_ms = (self._clock.monotonic() - started) * 1000
        self._metrics.observe(operation.name, _outcome(error), elapsed_ms)

    async def call[T](  # noqa: PLR0913 - one keyword per request part
        self,
        operation: RestOperation,
        path: str,
        parse: Callable[[RestResponse], T],
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | Sequence[str]] | None = None,
        files: Sequence[FormPart] | None = None,
        voice_id: str | None = None,
        accept: str = _JSON,
    ) -> T:
        """Run ``operation`` as one request to ``base_url + path`` and ``parse`` the successful response
        inside the operation (a malformed body counts as its failure). A parser slip that raises a
        plain ``TypeError``/``ValueError``/``KeyError``/``IndexError`` is reported as a protocol error."""
        request = _Request(operation, self.base_url + path, json, params, files, accept)

        async def work(exchange: Exchange) -> T:
            response = await exchange.send(request)
            try:
                return parse(response)
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                raise ElevenLabsProtocolError(operation.name, type(exc).__name__) from exc

        return await self.observed(operation, work, voice_id=voice_id)

    async def attempt(self, request: _Request) -> httpx.Response:
        return await self._http.request(
            request.operation.method,
            request.url,
            json=request.json,
            params=request.params,
            files=request.files,
            headers={API_KEY_HEADER: self._api_key, "Accept": request.accept},
            timeout=build_timeout(request.operation.timeout_s),
        )

    def wait(
        self, request: _Request, retry: int, outcome: httpx.Response | httpx.TransportError
    ) -> float | None:
        schedule = request.operation.retry
        if isinstance(outcome, httpx.TransportError):
            return schedule.wait(retry, self._rng)
        if outcome.is_success or outcome.status_code not in schedule.statuses:
            return None
        retry_after = retry_after_seconds(outcome.headers.get("retry-after"), self._clock.now())
        return schedule.wait(retry, self._rng, retry_after)

    @staticmethod
    def final(operation: RestOperation, outcome: httpx.Response | httpx.TransportError) -> RestResponse:
        if isinstance(outcome, httpx.TransportError):
            raise ElevenLabsTransportError(operation.name, outcome) from outcome
        if not outcome.is_success:
            raise api_error(operation.name, outcome.status_code, outcome.content)
        return RestResponse(outcome.status_code, outcome.headers, outcome.content)


class Exchange:
    """The requests of one logical operation: retries each one and counts attempts on the span."""

    def __init__(self, transport: ElevenLabsTransport, operation: RestOperation, span: Span) -> None:
        self._transport = transport
        self.operation = operation
        self._span = span
        self._attempts = 0

    def request(
        self,
        path: str,
        *,
        params: Mapping[str, str | Sequence[str]] | None = None,
    ) -> _Request:
        """A JSON ``GET``-style request of this operation (no body)."""
        return _Request(self.operation, self._transport.base_url + path, None, params, None, _JSON)

    async def send(self, request: _Request) -> RestResponse:
        """``request`` with its retries: the successful response, or the mapped error."""
        transport = self._transport
        retry = 0
        while True:
            self._attempts += 1
            self._span.set_attribute("elevenlabs.attempts", self._attempts)
            outcome: httpx.Response | httpx.TransportError
            try:
                outcome = await transport.attempt(request)
            except httpx.TransportError as exc:
                outcome = exc
            else:
                self._span.set_attribute("http.status_code", outcome.status_code)
            wait = transport.wait(request, retry, outcome)
            if wait is None:
                return transport.final(request.operation, outcome)
            _log.info(
                "elevenlabs_request_retry",
                operation=request.operation.name,
                attempt=retry + 1,
                delay_ms=round(wait * 1000),
                reason=type(outcome).__name__ if isinstance(outcome, Exception) else outcome.status_code,
            )
            await transport.sleep(wait)
            retry += 1
