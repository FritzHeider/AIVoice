"""Session expiry arithmetic and the ``sess:`` cache form (specs/04-auth-tenancy-rbac.md §4.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from va_api.auth.sessions import (
    ABSOLUTE_TTL,
    EXTEND_AFTER,
    SESSION_CACHE_TTL_S,
    SLIDING_TTL,
    ActiveSession,
    cache_ttl_s,
    next_expiry,
    session_cache_key,
)

CREATED = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
FIRST_EXPIRY = CREATED + timedelta(days=14)


def test_constants_match_spec() -> None:
    assert timedelta(days=14) == SLIDING_TTL
    assert timedelta(hours=1) == EXTEND_AFTER
    assert timedelta(days=90) == ABSOLUTE_TTL
    assert SESSION_CACHE_TTL_S == 300


@pytest.mark.parametrize("elapsed", [timedelta(0), timedelta(minutes=30), timedelta(hours=1)])
def test_no_extension_within_an_hour(elapsed: timedelta) -> None:
    assert next_expiry(created_at=CREATED, expires_at=FIRST_EXPIRY, now=CREATED + elapsed) is None


def test_extends_after_an_hour() -> None:
    now = CREATED + timedelta(hours=1, seconds=1)
    assert next_expiry(created_at=CREATED, expires_at=FIRST_EXPIRY, now=now) == now + SLIDING_TTL


def test_capped_at_absolute_lifetime() -> None:
    cap = CREATED + ABSOLUTE_TTL
    now = cap - timedelta(days=3)
    expires_at = cap - timedelta(days=5)
    assert next_expiry(created_at=CREATED, expires_at=expires_at, now=now) == cap
    assert next_expiry(created_at=CREATED, expires_at=cap, now=now) is None
    assert next_expiry(created_at=CREATED, expires_at=cap - timedelta(minutes=30), now=now) is None


def test_a_shorter_window_shortens_the_expiry() -> None:
    now = CREATED + timedelta(minutes=10)
    window = timedelta(days=1)
    assert next_expiry(created_at=CREATED, expires_at=FIRST_EXPIRY, now=now, window=window) == now + window


def test_a_longer_window_extends_past_the_default() -> None:
    now = CREATED + timedelta(hours=2)
    window = timedelta(days=30)
    assert next_expiry(created_at=CREATED, expires_at=FIRST_EXPIRY, now=now, window=window) == now + window


@pytest.mark.parametrize("days", [1, 30])
def test_no_write_within_an_hour_of_the_windowed_expiry(days: int) -> None:
    window = timedelta(days=days)
    expires_at = CREATED + window
    now = CREATED + timedelta(minutes=45)
    assert next_expiry(created_at=CREATED, expires_at=expires_at, now=now, window=window) is None


def test_the_absolute_cap_wins_over_a_long_window_and_never_shortens() -> None:
    cap = CREATED + ABSOLUTE_TTL
    now = cap - timedelta(days=10)
    window = timedelta(days=90)
    assert next_expiry(created_at=CREATED, expires_at=now, now=now, window=window) == cap
    assert next_expiry(created_at=CREATED, expires_at=cap, now=now, window=window) is None


def test_the_default_window_is_the_sliding_ttl() -> None:
    now = CREATED + timedelta(hours=3)
    assert next_expiry(created_at=CREATED, expires_at=FIRST_EXPIRY, now=now) == next_expiry(
        created_at=CREATED, expires_at=FIRST_EXPIRY, now=now, window=SLIDING_TTL
    )


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(days=1), 300),
        (timedelta(seconds=300), 300),
        (timedelta(seconds=42), 42),
        (timedelta(milliseconds=200), 1),
        (-timedelta(seconds=5), 1),
    ],
)
def test_cache_ttl(remaining: timedelta, expected: int) -> None:
    assert cache_ttl_s(CREATED + remaining, CREATED) == expected


def test_cache_key_uses_hash_tag() -> None:
    assert session_cache_key("ab12") == "sess:{ab12}"


@pytest.mark.parametrize(
    ("active_org_id", "mfa_passed_at"),
    [(uuid.uuid4(), CREATED + timedelta(minutes=3)), (None, None)],
)
def test_active_session_json_round_trip(
    active_org_id: uuid.UUID | None, mfa_passed_at: datetime | None
) -> None:
    active = ActiveSession(
        session_id=uuid.uuid4(),
        token_hash="f" * 64,
        user_id=uuid.uuid4(),
        active_org_id=active_org_id,
        mfa_passed=mfa_passed_at is not None,
        mfa_passed_at=mfa_passed_at,
        created_at=CREATED,
        expires_at=FIRST_EXPIRY,
    )
    data = active.to_json()
    assert set(data) == {
        "session_id",
        "user_id",
        "active_org_id",
        "mfa_passed",
        "mfa_passed_at",
        "created_at",
        "expires_at",
    }
    assert ActiveSession.from_json(data, "f" * 64) == active


def test_cache_entry_without_mfa_passed_at_reads_as_none() -> None:
    data = {
        "session_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "active_org_id": None,
        "mfa_passed": True,
        "created_at": CREATED.isoformat(),
        "expires_at": FIRST_EXPIRY.isoformat(),
    }
    active = ActiveSession.from_json(data, "e" * 64)
    assert active.mfa_passed is True
    assert active.mfa_passed_at is None
