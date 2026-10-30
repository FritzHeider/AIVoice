"""FakeElevenLabs authentication (specs/30 §3.3; Decision P03-T04 rules 3, 4, 11).

The WebSocket takes the ``xi-api-key`` header, else ``xi_api_key`` or ``authorization`` (``Bearer <key>`` or
the raw key) in the first message; REST takes the ``xi-api-key`` header. REST failures use ElevenLabs'
``{"detail": {"status", "message"}}`` body with HTTP 401.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from starlette.responses import JSONResponse

__all__ = [
    "API_KEY_HEADER",
    "INVALID_API_KEY",
    "detail_error",
    "key_from_message",
    "rest_auth_error",
]

API_KEY_HEADER: Final = "xi-api-key"
INVALID_API_KEY: Final = "Invalid API key"
_BEARER: Final = "bearer "
_MISSING_KEY: Final = "Neither authorization header nor xi-api-key received, please provide one."


def key_from_message(message: Mapping[str, object]) -> str | None:
    """The API key a first WebSocket message carries, if any."""
    key = message.get("xi_api_key")
    if isinstance(key, str) and key:
        return key
    authorization = message.get("authorization")
    if not isinstance(authorization, str):
        return None
    if authorization.lower().startswith(_BEARER):
        authorization = authorization[len(_BEARER) :]
    return authorization.strip() or None


def detail_error(status_code: int, status: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"status": status, "message": message}}, status_code=status_code)


def rest_auth_error(key: str | None, accepted: bool) -> JSONResponse | None:
    """The 401 response for a REST request with ``key``, or ``None`` when it authenticates."""
    if not key:
        return detail_error(401, "needs_authorization", _MISSING_KEY)
    if not accepted:
        return detail_error(401, "invalid_api_key", INVALID_API_KEY)
    return None
