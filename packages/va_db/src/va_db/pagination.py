"""Keyset cursor pagination for envelope list endpoints (specs/31-error-catalog.md §2).

A cursor is the URL-safe base64 (without ``=`` padding) of
``"<created_at UTC ISO-8601 with microseconds>|<id>"``.
Lists are ordered ``created_at DESC, id DESC`` and the next page holds the rows strictly below the cursor's
``(created_at, id)`` key, so rows inserted after the first page never shift or duplicate later pages.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from va_common.errors import AppError
from va_common.time import ensure_utc

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "Cursor",
    "Page",
    "decode_cursor",
    "encode_cursor",
    "validate_limit",
]

DEFAULT_PAGE_LIMIT: Final = 25
MAX_PAGE_LIMIT: Final = 100

_SEPARATOR: Final = "|"


@dataclass(frozen=True, slots=True)
class Cursor:
    """The ``(created_at, id)`` key of the last row of the previous page."""

    created_at: datetime
    id: uuid.UUID


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    """Opaque cursor for the row ``(created_at, row_id)``; a naive ``created_at`` raises ``ValueError``."""
    stamp = ensure_utc(created_at).isoformat(timespec="microseconds")
    raw = f"{stamp}{_SEPARATOR}{row_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _invalid_cursor() -> AppError:
    return AppError("INVALID_INPUT", "Invalid cursor.", field="cursor")


def _parse(value: str) -> Cursor:
    padded = value + "=" * (-len(value) % 4)
    raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
    stamp, row_id = raw.split(_SEPARATOR)
    return Cursor(created_at=ensure_utc(datetime.fromisoformat(stamp)), id=uuid.UUID(row_id))


def decode_cursor(value: str) -> Cursor:
    """Parse a cursor from :func:`encode_cursor`; anything else raises ``AppError("INVALID_INPUT")``."""
    if not value:
        raise _invalid_cursor()
    try:
        return _parse(value)
    except (ValueError, binascii.Error) as exc:
        raise _invalid_cursor() from exc


def validate_limit(limit: int) -> int:
    """``limit`` when it is within 1-100, else ``AppError("INVALID_INPUT")`` on field ``limit``."""
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise AppError("INVALID_INPUT", f"limit must be between 1 and {MAX_PAGE_LIMIT}.", field="limit")
    return limit


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a list: its items, the cursor of the next page (``None`` on the last), and ``has_more``."""

    items: tuple[T, ...]
    next_cursor: str | None
    has_more: bool

    def pagination(self) -> dict[str, str | bool | None]:
        """The response ``pagination`` object: ``{"next_cursor": ..., "has_more": ...}``."""
        return {"next_cursor": self.next_cursor, "has_more": self.has_more}
