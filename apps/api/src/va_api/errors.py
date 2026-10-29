"""Error rendering in the route's envelope (specs/31-error-catalog.md §1 and §4).

``install_handlers`` maps ``AppError``, request validation errors (re-rendered as 400), Starlette HTTP errors,
and unhandled exceptions (500, no internals in the body) to the legacy or envelope error body chosen by
:func:`va_api.envelopes.route_envelope`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from va_api.envelopes import Envelope, route_envelope
from va_api.middleware import REQUEST_ID_HEADER, SECURITY_HEADERS, request_id_of, route_label
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = [
    "HTTP_STATUS_CODES",
    "ErrorEnvelope",
    "ErrorItem",
    "ErrorLegacy",
    "error_body",
    "error_response",
    "http_exception_item",
    "install_handlers",
    "validation_items",
]

HTTP_STATUS_CODES: Final[Mapping[int, str]] = {
    400: "INVALID_INPUT",
    401: "AUTH_FAILURE",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}
_HTTP_MESSAGES: Final[Mapping[int, str]] = {404: "Not found.", 405: "Method not allowed."}
_LOCATIONS: Final = frozenset({"body", "query", "path", "header", "cookie"})
_VALUE_ERROR_PREFIX: Final = "Value error, "
_FIRST_SERVER_STATUS: Final = 500
_INTERNAL_MESSAGE: Final = "Internal server error"

_log = get_logger(__name__)


class ErrorItem(BaseModel):
    """One ``errors[]`` entry."""

    model_config = ConfigDict(
        title="ErrorItem",
        json_schema_extra={
            "examples": [
                {"error": "INVALID_INPUT", "message": "must be E.164", "field": "phone_number"},
            ]
        },
    )

    error: str = Field(description="Error code from the error catalog")
    message: str = Field(description="Human-readable message")
    field: str | None = Field(default=None, description="Dotted path of the invalid field")
    details: dict[str, Any] | None = Field(default=None, description="Structured error details")


class ErrorLegacy(BaseModel):
    """Error body of legacy (Bland-compatible) endpoints."""

    model_config = ConfigDict(
        title="ErrorLegacy",
        json_schema_extra={
            "examples": [
                {
                    "status": "error",
                    "message": "Not found.",
                    "errors": [{"error": "NOT_FOUND", "message": "Not found."}],
                    "request_id": "req_0192f3a4b5c67d8e9f0a1b2c3d4e5f60",
                }
            ]
        },
    )

    status: Literal["error"]
    message: str
    errors: list[ErrorItem]
    request_id: str


class ErrorEnvelope(BaseModel):
    """Error body of envelope endpoints."""

    model_config = ConfigDict(
        title="ErrorEnvelope",
        json_schema_extra={
            "examples": [
                {
                    "data": None,
                    "errors": [{"error": "NOT_FOUND", "message": "Not found."}],
                    "request_id": "req_0192f3a4b5c67d8e9f0a1b2c3d4e5f60",
                }
            ]
        },
    )

    data: None
    errors: list[ErrorItem]
    request_id: str


def error_body(envelope: Envelope, items: Sequence[Mapping[str, Any]], request_id: str) -> dict[str, Any]:
    """The §1 error body; the legacy top-level ``message`` equals the first item's message."""
    errors = [dict(item) for item in items]
    if envelope is Envelope.LEGACY:
        return {
            "status": "error",
            "message": errors[0]["message"],
            "errors": errors,
            "request_id": request_id,
        }
    return {"data": None, "errors": errors, "request_id": request_id}


def error_response(
    request: Request,
    status: int,
    items: Sequence[Mapping[str, Any]],
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render ``items`` in the request's envelope with the request id and security headers already set.

    The 500 handler runs in Starlette's outermost ``ServerErrorMiddleware``, outside the request-context
    middleware, so the headers are set here rather than relying on it.
    """
    request_id = request_id_of(request)
    all_headers = {**(headers or {}), REQUEST_ID_HEADER: request_id, **dict(SECURITY_HEADERS)}
    body = error_body(route_envelope(request.scope), items, request_id)
    return JSONResponse(body, status_code=status, headers=all_headers)


def _field(loc: Sequence[str | int]) -> str | None:
    parts = list(loc)
    if parts and parts[0] in _LOCATIONS:
        parts = parts[1:]
    return ".".join(str(part) for part in parts) or None


def _validation_item(error: Mapping[str, Any]) -> dict[str, Any]:
    message = str(error.get("msg", "Invalid input"))
    item: dict[str, Any] = {"error": "INVALID_INPUT", "message": message.removeprefix(_VALUE_ERROR_PREFIX)}
    field = None if error.get("type") == "json_invalid" else _field(error.get("loc", ()))
    if field is not None:
        item["field"] = field
    return item


def validation_items(exc: RequestValidationError) -> list[dict[str, Any]]:
    """One ``INVALID_INPUT`` item per validation error, ``field`` as a dotted path without the location."""
    items = [_validation_item(error) for error in exc.errors()]
    return items or [{"error": "INVALID_INPUT", "message": "Invalid input"}]


def http_exception_item(exc: StarletteHTTPException) -> dict[str, Any]:
    """Catalog code and message for a framework ``HTTPException``."""
    code = HTTP_STATUS_CODES.get(exc.status_code)
    if code is None:
        code = "INTERNAL_ERROR" if exc.status_code >= _FIRST_SERVER_STATUS else "INVALID_INPUT"
    message = _HTTP_MESSAGES.get(exc.status_code)
    if message is None:
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else code
    return {"error": code, "message": message}


type _Render[E: Exception] = Callable[[Request, E], JSONResponse]
type _Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _handler[E: Exception](kind: type[E], render: _Render[E]) -> _Handler:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, kind):  # Starlette dispatches by type; this only narrows for the type checker
            raise exc
        return render(request, exc)

    return handle


def _render_app_error(request: Request, exc: AppError) -> JSONResponse:
    return error_response(request, exc.http_status, [exc.to_error_item()], exc.headers or None)


def _render_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(request, 400, validation_items(exc))


def _render_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(request, exc.status_code, [http_exception_item(exc)], exc.headers)


def _render_unhandled(request: Request, exc: Exception) -> JSONResponse:
    response = error_response(request, 500, [{"error": "INTERNAL_ERROR", "message": _INTERNAL_MESSAGE}])
    _log.error(
        "api.unhandled_exception",
        exc_info=exc,
        request_id=request_id_of(request),
        method=request.method,
        route=route_label(request.scope),
    )
    return response


def install_handlers(app: FastAPI) -> None:
    """Register the envelope-aware exception handlers on ``app``."""
    app.add_exception_handler(AppError, _handler(AppError, _render_app_error))
    app.add_exception_handler(
        RequestValidationError, _handler(RequestValidationError, _render_validation_error)
    )
    app.add_exception_handler(StarletteHTTPException, _handler(StarletteHTTPException, _render_http_error))
    app.add_exception_handler(Exception, _handler(Exception, _render_unhandled))
