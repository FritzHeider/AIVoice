"""OpenAI error bodies (specs/30 §3.4 Decision P03-T06a rules 1, 2, 9).

Every FakeLLM failure answers ``{"error": {"message", "type", "param", "code"}}`` like the OpenAI API, so the
official SDK maps the status to its exception classes (``AuthenticationError``, ``RateLimitError``, ...).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue
from starlette.responses import JSONResponse

__all__ = ["error_body", "error_type_for", "openai_error"]

_INVALID_REQUEST: Final = "invalid_request_error"
_SERVER_ERRORS: Final = 500
_TYPES: Final[Mapping[int, str]] = {
    401: "authentication_error",
    403: "permission_error",
    429: "rate_limit_error",
}


def error_type_for(status: int) -> str:
    """The OpenAI ``error.type`` of an HTTP status."""
    if status >= _SERVER_ERRORS:
        return "server_error"
    return _TYPES.get(status, _INVALID_REQUEST)


def error_body(
    status: int,
    message: str,
    *,
    error_type: str | None = None,
    code: str | None = None,
    param: str | None = None,
) -> dict[str, JsonValue]:
    return {
        "error": {
            "message": message,
            "type": error_type if error_type is not None else error_type_for(status),
            "param": param,
            "code": code,
        }
    }


def openai_error(  # noqa: PLR0913 - keyword-only body fields
    status: int,
    message: str,
    *,
    error_type: str | None = None,
    code: str | None = None,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = error_body(status, message, error_type=error_type, code=code, param=param)
    return JSONResponse(body, status_code=status, headers=dict(headers) if headers else None)
