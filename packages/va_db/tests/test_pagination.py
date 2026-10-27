"""Cursor encoding, limit bounds, and the page shape (specs/31-error-catalog.md §2)."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    Cursor,
    Page,
    decode_cursor,
    encode_cursor,
    validate_limit,
)

_ROW_ID = uuid.UUID("01900000-0000-7000-8000-000000000001")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_defaults_follow_the_spec() -> None:
    assert (DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT) == (25, 100)


def test_round_trip_keeps_microseconds() -> None:
    created_at = datetime(2026, 10, 27, 12, 30, 45, 123456, tzinfo=UTC)
    row_id = new_id()
    assert decode_cursor(encode_cursor(created_at, row_id)) == Cursor(created_at=created_at, id=row_id)


def test_non_utc_input_is_normalised_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    cursor = decode_cursor(encode_cursor(datetime(2026, 10, 27, 14, 0, tzinfo=plus_two), _ROW_ID))
    assert cursor.created_at == datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    assert cursor.created_at.utcoffset() == timedelta(0)


def test_encoded_cursor_is_url_safe_without_padding() -> None:
    # 2026-...+00:00|<uuid> is 69 bytes, so standard base64 would end in "=".
    value = encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), _ROW_ID)
    assert "=" not in value
    assert set(value) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    assert raw == f"2026-01-01T00:00:00.000000+00:00|{_ROW_ID}"


def test_encode_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        encode_cursor(datetime(2026, 1, 1), _ROW_ID)  # noqa: DTZ001  (the naive input under test)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "!!!",
        "é",
        _b64(b"no-separator"),
        _b64(b"2026-01-01T00:00:00+00:00|not-a-uuid"),
        _b64(f"2026-01-01T00:00:00|{_ROW_ID}".encode()),
        _b64(f"yesterday|{_ROW_ID}".encode()),
        _b64(f"2026-01-01T00:00:00+00:00|{_ROW_ID}|extra".encode()),
        _b64(b"\xff\xfe"),
    ],
)
def test_invalid_cursors_are_invalid_input(value: str) -> None:
    with pytest.raises(AppError) as caught:
        decode_cursor(value)
    error = caught.value
    assert (error.code, error.field, error.http_status, error.message) == (
        "INVALID_INPUT",
        "cursor",
        400,
        "Invalid cursor.",
    )


@pytest.mark.parametrize("limit", [1, 25, 100])
def test_limits_within_bounds_are_accepted(limit: int) -> None:
    assert validate_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_limits_outside_bounds_are_invalid_input(limit: int) -> None:
    with pytest.raises(AppError) as caught:
        validate_limit(limit)
    assert (caught.value.code, caught.value.field, caught.value.http_status) == (
        "INVALID_INPUT",
        "limit",
        400,
    )
    assert caught.value.message == "limit must be between 1 and 100."


def test_page_pagination_object() -> None:
    assert Page(items=(1, 2), next_cursor="abc", has_more=True).pagination() == {
        "next_cursor": "abc",
        "has_more": True,
    }
    assert Page[int](items=(), next_cursor=None, has_more=False).pagination() == {
        "next_cursor": None,
        "has_more": False,
    }
