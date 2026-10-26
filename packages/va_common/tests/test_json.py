from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

import pytest
from pydantic import BaseModel

from va_common.json import dumps, dumps_str, loads


class Color(StrEnum):
    RED = "red"


class Model(BaseModel):
    id: uuid.UUID
    at: datetime


ID = uuid.UUID("0190f1b2-3c4d-7e5f-8a6b-9c0d1e2f3a4b")
AT = datetime(2026, 3, 1, 12, 0, 0, 500000, tzinfo=UTC)


def test_dumps_returns_bytes() -> None:
    assert dumps({"a": 1}) == b'{"a":1}'


def test_dumps_platform_types() -> None:
    payload = {
        "id": ID,
        "at": AT,
        "amount": Decimal("1.500000"),
        "model": Model(id=ID, at=AT),
        "tags": {"b", "a"},
        "frozen": frozenset({2, 1}),
        "color": Color.RED,
    }
    assert loads(dumps(payload)) == {
        "id": str(ID),
        "at": "2026-03-01T12:00:00.500000+00:00",
        "amount": "1.500000",
        "model": {"id": str(ID), "at": "2026-03-01T12:00:00.500000Z"},
        "tags": ["a", "b"],
        "frozen": [1, 2],
        "color": "red",
    }


def test_dumps_converts_offset_datetimes_to_utc() -> None:
    local = datetime(2026, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert loads(dumps(local)) == "2026-03-01T12:00:00+00:00"


def test_dumps_rejects_naive_datetimes() -> None:
    with pytest.raises(TypeError, match="naive"):
        dumps({"at": datetime(2026, 3, 1)})  # noqa: DTZ001


def test_dumps_serializes_dates() -> None:
    assert dumps(date(2026, 1, 2)) == b'"2026-01-02"'


def test_dumps_serializes_dates_nested_in_dicts_and_models() -> None:
    class Period(BaseModel):
        start: date

    payload = {"usage_day": date(2026, 1, 2), "period": Period(start=date(2026, 2, 1))}
    assert loads(dumps(payload)) == {"usage_day": "2026-01-02", "period": {"start": "2026-02-01"}}


def test_dumps_serializes_aware_times_with_their_offset() -> None:
    assert dumps(time(1, 2, tzinfo=UTC)) == b'"01:02:00+00:00"'
    assert dumps(time(9, 30, tzinfo=timezone(timedelta(hours=-5)))) == b'"09:30:00-05:00"'


def test_dumps_rejects_naive_times() -> None:
    with pytest.raises(TypeError, match="naive time"):
        dumps({"opens": time(1, 2)})


def test_dumps_rejects_unknown_types() -> None:
    with pytest.raises(TypeError, match="object"):
        dumps({"x": object()})


def test_sort_keys_orders_nested_keys() -> None:
    assert dumps({"b": {"z": 1, "y": 2}, "a": 0}, sort_keys=True) == b'{"a":0,"b":{"y":2,"z":1}}'
    assert dumps_str({"b": 1, "a": 2}, sort_keys=True) == '{"a":2,"b":1}'


def test_dumps_str_and_loads_round_trip() -> None:
    data = {"name": "Zoë", "values": [1, 2.5, None, True]}
    text = dumps_str(data)
    assert isinstance(text, str)
    assert loads(text) == data
    raw = dumps(data)
    assert loads(raw) == loads(bytearray(raw)) == loads(memoryview(raw)) == data
