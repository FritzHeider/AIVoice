"""Redis keys of the dispatch limits, exactly as listed in the specs/03 Redis table (UTC date parts)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

__all__ = [
    "concurrency_key",
    "daily_key",
    "frequency_key",
    "hourly_key",
    "international_key",
    "same_number_key",
]


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        msg = "limit keys need a timezone-aware datetime"
        raise ValueError(msg)
    return at.astimezone(UTC)


def same_number_key(org_id: uuid.UUID, e164: str) -> str:
    """``cap:dest:{org_id}:{e164}``: the same-number 10-second rule."""
    return f"cap:dest:{org_id}:{e164}"


def frequency_key(org_id: uuid.UUID, e164: str, at: datetime) -> str:
    """``freq:{org_id}:{e164}:{yyyymmdd}``: calls to one number on the UTC day of ``at``."""
    return f"freq:{org_id}:{e164}:{_utc(at):%Y%m%d}"


def daily_key(org_id: uuid.UUID, at: datetime) -> str:
    """``cap:org:{org_id}:daily:{yyyymmdd}``."""
    return f"cap:org:{org_id}:daily:{_utc(at):%Y%m%d}"


def hourly_key(org_id: uuid.UUID, at: datetime) -> str:
    """``cap:org:{org_id}:hourly:{yyyymmddhh}``."""
    return f"cap:org:{org_id}:hourly:{_utc(at):%Y%m%d%H}"


def international_key(org_id: uuid.UUID, at: datetime) -> str:
    """``cap:org:{org_id}:intl:{yyyymmddhh}``."""
    return f"cap:org:{org_id}:intl:{_utc(at):%Y%m%d%H}"


def concurrency_key(org_id: uuid.UUID) -> str:
    """``cap:org:{org_id}:concurrency``: sorted set call id → expiry unix ms."""
    return f"cap:org:{org_id}:concurrency"
