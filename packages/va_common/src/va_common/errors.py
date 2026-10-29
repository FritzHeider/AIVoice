"""Domain errors and the error-code catalog (specs/31-error-catalog.md §3).

``CODES`` mirrors the §3 table row for row, in table order; a test parses the markdown table and asserts
parity.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

__all__ = ["ALTERNATE_STATUSES", "CODES", "AppError", "status_for"]

CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "AUTH_FAILURE": 401,
        "MFA_REQUIRED": 401,
        "FORBIDDEN": 403,
        "ORG_SUSPENDED": 403,
        "NOT_FOUND": 404,
        "INVALID_INPUT": 400,
        "CONFLICT": 409,
        "IDEMPOTENCY_KEY_REUSED": 422,
        "IDEMPOTENCY_IN_PROGRESS": 409,
        "RATE_LIMITED": 429,
        "DAILY_CAP_REACHED": 429,
        "HOURLY_CAP_REACHED": 429,
        "CONCURRENCY_CAP_REACHED": 429,
        "SAME_NUMBER_TOO_SOON": 429,
        "NEW_ACCOUNT_LIMIT": 429,
        "INTERNATIONAL_NOT_ENABLED": 403,
        "INTERNATIONAL_RATE_LIMITED": 429,
        "NUMBER_BLOCKED": 403,
        "OUTSIDE_CALLING_HOURS": 400,
        "INSUFFICIENT_CREDITS": 402,
        "INVALID_PHONE_NUMBER": 400,
        "RUNNER_CONFLICT": 400,
        "AGENT_REQUIRES_ORG_KEY": 403,
        "PATHWAY_VERSION_NOT_FOUND": 404,
        "VOICE_NOT_FOUND": 404,
        "TOOL_NOT_FOUND": 404,
        "KB_ERROR": 400,
        "KB_UNSUPPORTED_FILE": 400,
        "KB_FILE_TOO_LARGE": 413,
        "INVALID_ENCRYPTED_KEY": 400,
        "CARRIER_CONNECTION_ERROR": 502,
        "CARRIER_ERROR": 502,
        "NUMBER_UNAVAILABLE": 409,
        "NO_NUMBERS_FOUND": 404,
        "RETRY_NOT_PROVIDED": 400,
        "CALL_NOT_ACTIVE": 409,
        "CALL_NOT_FOUND": 404,
        "BATCH_NOT_FOUND": 404,
        "BATCH_TOO_LARGE": 400,
        "BATCH_NOT_STOPPABLE": 409,
        "CHANNEL_NOT_SUPPORTED": 400,
        "SMS_NOT_ENABLED": 400,
        "WIDGET_DOMAIN_NOT_ALLOWED": 403,
        "WIDGET_RATE_LIMITED": 429,
        "VOICE_CLONE_LIMIT": 403,
        "VOICE_CLONE_FAILED": 502,
        "SAMPLE_INVALID": 400,
        "GUARD_RAIL_LIMIT": 400,
        "FEATURE_NOT_IN_PLAN": 403,
        "CUSTOM_CODE_ERROR": 400,
        "WEBHOOK_URL_REJECTED": 400,
        "UPSTREAM_TIMEOUT": 504,
        "SSO_REQUIRED": 403,
        "EMAIL_NOT_VERIFIED": 403,
        "INVALID_CREDENTIALS": 401,
        "INVALID_MFA_CODE": 401,
        "PASSWORD_TOO_WEAK": 400,
        "TOKEN_EXPIRED": 400,
        "REAUTH_REQUIRED": 401,
        "AUTH_LOCKED": 429,
        "INTERNAL_ERROR": 500,
        "SERVICE_UNAVAILABLE": 503,
    }
)

# Codes whose §3 cell lists more than one status: CODES holds the default, these are explicit overrides.
ALTERNATE_STATUSES: Final[Mapping[str, frozenset[int]]] = MappingProxyType({"KB_ERROR": frozenset({500})})


def status_for(code: str) -> int:
    """Default HTTP status for an error code."""
    try:
        return CODES[code]
    except KeyError:
        msg = f"unknown error code: {code!r}"
        raise ValueError(msg) from None


def _allowed_statuses(code: str) -> frozenset[int]:
    return frozenset({status_for(code)}) | ALTERNATE_STATUSES.get(code, frozenset())


class AppError(Exception):
    """A domain error carrying a catalog code; the API layer renders it in the route's envelope."""

    def __init__(  # noqa: PLR0913 - keyword-only field and headers
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        details: Mapping[str, Any] | None = None,
        *,
        field: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        status = status_for(code) if http_status is None else http_status
        if status not in _allowed_statuses(code):
            msg = f"status {status} is not allowed for error code {code}"
            raise ValueError(msg)
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = status
        self.field = field
        self.details: dict[str, Any] = dict(details) if details else {}
        # Extra response headers (e.g. ``Retry-After``); never part of the error body.
        self.headers: dict[str, str] = dict(headers) if headers else {}

    def to_error_item(self) -> dict[str, Any]:
        """One ``errors[]`` item: ``error`` and ``message``, plus ``field``/``details`` when present."""
        item: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.field is not None:
            item["field"] = self.field
        if self.details:
            item["details"] = dict(self.details)
        return item

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        return f"AppError(code={self.code!r}, message={self.message!r}, http_status={self.http_status})"
