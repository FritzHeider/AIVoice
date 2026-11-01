"""Telnyx client errors and their mapping to platform error codes (specs/06-telephony-telnyx.md §2, §2.2;
specs/31-error-catalog.md).

Telnyx error bodies are ``{"errors": [{"code", "title", "detail"?, "source"?}]}``. Every client failure is a
:class:`TelnyxError`; :meth:`TelnyxError.to_app_error` turns it into the :class:`AppError` a synchronous
endpoint renders (``CARRIER_ERROR`` with ``details.telnyx_errors``, or ``SERVICE_UNAVAILABLE`` for the
client-side rate limit).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import orjson

from va_common.errors import AppError

__all__ = [
    "CALL_ENDED_CODE",
    "CallAlreadyEnded",
    "TelnyxApiError",
    "TelnyxError",
    "TelnyxErrorItem",
    "TelnyxRateLimited",
    "TelnyxTransportError",
    "parse_error_body",
]

CALL_ENDED_CODE: Final = "90018"
_DETAIL_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class TelnyxErrorItem:
    """One Telnyx ``errors[]`` entry."""

    code: str
    title: str
    detail: str | None = None
    source: Mapping[str, Any] | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, Any]:
        """``code``, ``title``, and ``detail`` (when present): the shape of ``details.telnyx_errors[]``."""
        item: dict[str, Any] = {"code": self.code, "title": self.title}
        if self.detail is not None:
            item["detail"] = self.detail
        return item


def _item(raw: Mapping[str, Any]) -> TelnyxErrorItem:
    source = raw.get("source")
    detail = raw.get("detail")
    return TelnyxErrorItem(
        code=str(raw.get("code", "")),
        title=str(raw.get("title", "")),
        detail=None if detail is None else str(detail),
        source=source if isinstance(source, Mapping) else None,
    )


def parse_error_body(status: int, reason: str, content: bytes) -> tuple[TelnyxErrorItem, ...]:
    """The ``errors[]`` of an error response; a body that is not a Telnyx error document becomes one synthetic
    item ``{"code": "http_<status>", "title": <reason phrase>, "detail": <first 200 characters>}``."""
    try:
        document = orjson.loads(content)
    except orjson.JSONDecodeError:
        document = None
    raw_items = document.get("errors") if isinstance(document, dict) else None
    if isinstance(raw_items, list):
        items = tuple(_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        if items:
            return items
    detail = content[:_DETAIL_CHARS].decode("utf-8", errors="replace") or None
    return (TelnyxErrorItem(code=f"http_{status}", title=reason or f"HTTP {status}", detail=detail),)


class TelnyxError(Exception):
    """Base class of every Telnyx client failure."""

    def __init__(self, message: str, operation: str) -> None:
        super().__init__(message)
        self.operation = operation

    def to_app_error(self) -> AppError:
        """The platform error for this failure (``CARRIER_ERROR`` unless a subclass says otherwise)."""
        return AppError("CARRIER_ERROR", str(self), details={"telnyx_errors": []})


class TelnyxApiError(TelnyxError):
    """Telnyx answered with a non-2xx status (after any retries)."""

    def __init__(self, status: int, errors: tuple[TelnyxErrorItem, ...], operation: str) -> None:
        first = errors[0].title if errors else ""
        super().__init__(f"Telnyx {operation} failed with HTTP {status}: {first}".rstrip(": "), operation)
        self.status = status
        self.errors = errors

    def to_app_error(self) -> AppError:
        return AppError(
            "CARRIER_ERROR",
            str(self),
            details={"telnyx_errors": [item.to_json() for item in self.errors]},
        )


class CallAlreadyEnded(TelnyxApiError):  # noqa: N818 - the specs/06 §2.2 name
    """The call is gone: ``422`` with code ``90018``, or ``404`` on a command for a call. ``hangup`` treats
    it as success; callers ignore it for every other command."""


class TelnyxRateLimited(TelnyxError):  # noqa: N818 - the specs/06 §2.2 name
    """The client-side token bucket had no token within the wait budget (``503 SERVICE_UNAVAILABLE`` in
    synchronous endpoints, a retry in activities)."""

    def __init__(self, *, account: str, category: str, retry_after_s: float, operation: str) -> None:
        super().__init__(f"Telnyx {category} rate limit reached for account {account}", operation)
        self.account = account
        self.category = category
        self.retry_after_s = retry_after_s

    def to_app_error(self) -> AppError:
        return AppError(
            "SERVICE_UNAVAILABLE",
            "The carrier is busy; retry shortly.",
            headers={"Retry-After": str(max(1, math.ceil(self.retry_after_s)))},
        )


class TelnyxTransportError(TelnyxError):
    """No usable response: connection, timeout, or protocol failure after the allowed retries."""

    def __init__(self, operation: str, cause: Exception) -> None:
        super().__init__(f"Telnyx {operation} failed: {type(cause).__name__}", operation)
        self.cause_type = type(cause).__name__

    def to_app_error(self) -> AppError:
        return AppError(
            "CARRIER_ERROR", str(self), details={"telnyx_errors": [], "transport": self.cause_type}
        )
