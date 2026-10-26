"""Pure-ASGI middleware: request id + security headers, and request metrics + access log.

specs/31-error-catalog.md §1 (``X-Request-Id`` on every response), specs/27-observability.md §1
(``request_id`` bound to every log line, one access line per request) and §3.1 (HTTP metrics),
specs/28-security.md §5 (headers on all responses).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Final

import structlog
from fastapi.routing import APIRoute
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics

__all__ = [
    "REQUEST_ID_HEADER",
    "SECURITY_HEADERS",
    "MetricsMiddleware",
    "RequestContextMiddleware",
    "merge_headers",
    "new_request_id",
    "request_id_of",
    "resolve_request_id",
    "route_label",
]

REQUEST_ID_HEADER: Final = "x-request-id"
SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
)
UNMATCHED_ROUTE: Final = "unmatched"
_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_HTTP_SCOPES: Final = frozenset({"http", "websocket"})
# A scope key of our own: `scope["state"]` may be one dict shared by every request (lifespan state).
_SCOPE_KEY: Final = "va.request_id"
_SERVER_ERROR: Final = 500

_log = get_logger(__name__)


def new_request_id() -> str:
    """``req_`` + the hex of a fresh UUIDv7."""
    return f"req_{new_id().hex}"


def resolve_request_id(incoming: str | None) -> str:
    """Echo a well-formed incoming id (``[A-Za-z0-9._:-]{1,128}``), otherwise generate one."""
    if incoming is not None and _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return new_request_id()


def request_id_of(connection: HTTPConnection) -> str:
    """The request id assigned by :class:`RequestContextMiddleware` (assigned here when absent)."""
    scope = connection.scope
    current = scope.get(_SCOPE_KEY)
    if isinstance(current, str):
        return current
    assigned = resolve_request_id(connection.headers.get(REQUEST_ID_HEADER))
    scope[_SCOPE_KEY] = assigned
    return assigned


def merge_headers(
    raw: Iterable[tuple[bytes, bytes]], extra: Iterable[tuple[str, str]]
) -> list[tuple[bytes, bytes]]:
    """``raw`` plus each ``extra`` header whose name is not already present."""
    headers = list(raw)
    present = {name.lower() for name, _ in headers}
    for name, value in extra:
        key = name.lower().encode("latin-1")
        if key not in present:
            headers.append((key, value.encode("latin-1")))
            present.add(key)
    return headers


class RequestContextMiddleware:
    """Assign the request id, bind it to logs, and add it plus the security headers to the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in _HTTP_SCOPES:
            await self.app(scope, receive, send)
            return
        request_id = request_id_of(HTTPConnection(scope))
        extra = ((REQUEST_ID_HEADER, request_id), *SECURITY_HEADERS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = merge_headers(message.get("headers", ()), extra)
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await self.app(scope, receive, send_with_headers)


def route_label(scope: Scope) -> str:
    """Path template of the matched route (``/v1/calls/{call_id}``), or ``unmatched``."""
    route = scope.get("route")
    return route.path_format if isinstance(route, APIRoute) else UNMATCHED_ROUTE


class MetricsMiddleware:
    """Record the HTTP request counter and duration histogram; log one access line per request."""

    def __init__(self, app: ASGIApp, metrics: ServiceMetrics) -> None:
        self.app = app
        self._requests = metrics.counter("va_http_requests_total")
        self._duration = metrics.histogram("va_http_request_duration_ms")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = _SERVER_ERROR

        async def send_recording(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_recording)
        finally:
            self._record(scope, status, (time.monotonic() - started) * 1000.0)

    def _record(self, scope: Scope, status: int, duration_ms: float) -> None:
        route, method = route_label(scope), str(scope["method"])
        self._requests.labels(route=route, method=method, status=str(status)).inc()
        self._duration.labels(route=route, method=method).observe(duration_ms)
        _log.info("http.request", method=method, path=route, status=status, duration_ms=round(duration_ms, 2))
