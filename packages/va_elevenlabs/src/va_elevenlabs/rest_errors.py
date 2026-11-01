"""ElevenLabs REST client errors and their platform mapping (specs/15-voices.md §7; Decision 2026-11-01
P03-T12 rule 4).

ElevenLabs answers errors in two shapes: ``{"detail": {"status", "message"}}`` (``code`` = ``status``) and the
422 validation list ``{"detail": [{"loc", "msg", "type"}]}`` (``code`` = ``validation_error``, message = the
first ``msg``). Any other body becomes ``code`` = ``http_<status>`` with the first 200 characters as message.
Messages never contain the API key (it travels only in the ``xi-api-key`` header).
"""

from __future__ import annotations

from typing import Any, Final

import orjson

from va_common.errors import AppError

__all__ = [
    "ElevenLabsApiError",
    "ElevenLabsAuthError",
    "ElevenLabsError",
    "ElevenLabsNotFoundError",
    "ElevenLabsProtocolError",
    "ElevenLabsRateLimitedError",
    "ElevenLabsTransportError",
    "api_error",
    "parse_error_body",
]

VALIDATION_CODE: Final = "validation_error"
_MESSAGE_CHARS: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_HTTP_NOT_FOUND: Final = 404
_HTTP_UNPROCESSABLE: Final = 422
_HTTP_TOO_MANY: Final = 429
_HTTP_SERVER: Final = 500


class ElevenLabsError(Exception):
    """Any ElevenLabs REST failure of ``operation`` (the client method name)."""

    def __init__(self, message: str, operation: str) -> None:
        super().__init__(message)
        self.operation = operation

    def to_app_error(self) -> AppError:
        """The platform error for this failure: a vendor outage is ``UPSTREAM_TIMEOUT``."""
        return AppError("UPSTREAM_TIMEOUT", "ElevenLabs did not respond in time")


def _detail_text(value: object) -> str:
    return "" if value is None else str(value)[:_MESSAGE_CHARS]


def _from_detail(status: int, detail: object) -> tuple[str, str] | None:
    """``(code, message)`` of a recognised ``detail`` value, else ``None``."""
    if isinstance(detail, dict) and ("status" in detail or "message" in detail):
        return _detail_text(detail.get("status")) or f"http_{status}", _detail_text(detail.get("message"))
    if isinstance(detail, list) and detail:
        first = detail[0]
        return VALIDATION_CODE, _detail_text(first.get("msg") if isinstance(first, dict) else first)
    if isinstance(detail, str):
        return f"http_{status}", _detail_text(detail)
    return None


def parse_error_body(status: int, content: bytes) -> tuple[str, str, Any]:
    """``(code, message, detail)`` of an error response body; ``detail`` is the parsed ``detail`` value."""
    try:
        document = orjson.loads(content)
    except orjson.JSONDecodeError:
        document = None
    detail = document.get("detail") if isinstance(document, dict) else None
    parsed = _from_detail(status, detail)
    if parsed is None:
        return f"http_{status}", content[:_MESSAGE_CHARS].decode("utf-8", errors="replace"), None
    return parsed[0], parsed[1], detail


class ElevenLabsApiError(ElevenLabsError):
    """ElevenLabs answered with a non-2xx ``status`` (after any retries)."""

    def __init__(self, status: int, code: str, message: str, detail: Any, operation: str) -> None:
        text = f"ElevenLabs {operation} failed with HTTP {status} ({code})"
        super().__init__(f"{text}: {message}" if message else text, operation)
        self.status = status
        self.code = code
        self.provider_message = message
        self.detail = detail

    def to_app_error(self) -> AppError:
        """specs/15 §7: 401/403 ``INTERNAL_ERROR``; 400/422 ``VOICE_CLONE_FAILED`` (with
        ``details.provider_message``); 404 ``VOICE_NOT_FOUND``; 429/5xx ``UPSTREAM_TIMEOUT``; any other status
        ``INTERNAL_ERROR``."""
        if self.status in {_HTTP_BAD_REQUEST, _HTTP_UNPROCESSABLE}:
            return AppError(
                "VOICE_CLONE_FAILED",
                "ElevenLabs rejected the request",
                details={"provider_code": self.code, "provider_message": self.provider_message},
            )
        if self.status == _HTTP_NOT_FOUND:
            return AppError("VOICE_NOT_FOUND", "Voice not found")
        if self.status == _HTTP_TOO_MANY or self.status >= _HTTP_SERVER:
            return super().to_app_error()
        return AppError("INTERNAL_ERROR", "Internal server error")


class ElevenLabsAuthError(ElevenLabsApiError):
    """401/403: the platform credential is wrong or lacks permission (never retried; alert)."""


class ElevenLabsNotFoundError(ElevenLabsApiError):
    """404 (``voice_not_found`` and friends)."""


class ElevenLabsRateLimitedError(ElevenLabsApiError):
    """429 after every retry."""


class ElevenLabsTransportError(ElevenLabsError):
    """A timeout or network failure after every retry."""

    def __init__(self, operation: str, cause: Exception) -> None:
        super().__init__(f"ElevenLabs {operation} failed: {type(cause).__name__}", operation)
        self.cause = cause


class ElevenLabsProtocolError(ElevenLabsError):
    """A success response whose body does not have the documented shape."""

    def __init__(self, operation: str, reason: str) -> None:
        super().__init__(f"ElevenLabs {operation} returned an unexpected body: {reason}", operation)
        self.reason = reason

    def to_app_error(self) -> AppError:
        return AppError("INTERNAL_ERROR", "Internal server error")


def api_error(operation: str, status: int, content: bytes) -> ElevenLabsApiError:
    """The error for a non-2xx response."""
    code, message, detail = parse_error_body(status, content)
    if status in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
        return ElevenLabsAuthError(status, code, message, detail, operation)
    if status == _HTTP_NOT_FOUND:
        return ElevenLabsNotFoundError(status, code, message, detail, operation)
    if status == _HTTP_TOO_MANY:
        return ElevenLabsRateLimitedError(status, code, message, detail, operation)
    return ElevenLabsApiError(status, code, message, detail, operation)
