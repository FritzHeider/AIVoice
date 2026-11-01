"""Limit keys match the specs/03 Redis table, with UTC date parts."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from va_billing import keys

ORG = uuid.UUID("0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d")
# 2026-11-02 01:30 in UTC-05:00 is 06:30 UTC; 2026-11-01 23:30 in UTC-05:00 is 2026-11-02 04:30 UTC.
LOCAL = datetime(2026, 11, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))


def test_keys() -> None:
    assert keys.same_number_key(ORG, "+12125550100") == f"cap:dest:{ORG}:+12125550100"
    assert keys.frequency_key(ORG, "+12125550100", LOCAL) == f"freq:{ORG}:+12125550100:20261102"
    assert keys.daily_key(ORG, LOCAL) == f"cap:org:{ORG}:daily:20261102"
    assert keys.hourly_key(ORG, LOCAL) == f"cap:org:{ORG}:hourly:2026110204"
    assert keys.international_key(ORG, LOCAL) == f"cap:org:{ORG}:intl:2026110204"
    assert keys.concurrency_key(ORG) == f"cap:org:{ORG}:concurrency"


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        keys.daily_key(ORG, datetime(2026, 11, 2))  # noqa: DTZ001 - the naive value under test
