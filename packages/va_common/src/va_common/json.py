"""orjson-backed JSON helpers with the platform's serialization rules.

Decimals serialize as strings, pydantic models via ``model_dump(mode="json")``, sets as sorted lists, and
datetimes as UTC ISO-8601, dates as ``YYYY-MM-DD`` and aware times as ISO-8601 with their offset. Naive
datetimes and naive times are rejected so they can never leave a process.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import orjson
from pydantic import BaseModel

from va_common.time import ensure_utc

__all__ = ["dumps", "dumps_str", "loads"]

_BASE_OPTIONS = orjson.OPT_PASSTHROUGH_DATETIME


def _time_iso(value: time) -> str:
    if value.utcoffset() is None:
        msg = "naive time is not allowed; attach a timezone"
        raise TypeError(msg)
    return value.isoformat()


def _default(obj: object) -> Any:
    if isinstance(obj, datetime):
        try:
            return ensure_utc(obj).isoformat()
        except ValueError as exc:
            raise TypeError(str(exc)) from None
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return _time_iso(obj)
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, set | frozenset):
        return sorted(obj)
    msg = f"Type is not JSON serializable: {type(obj).__name__}"
    raise TypeError(msg)


def dumps(obj: object, *, sort_keys: bool = False) -> bytes:
    """Serialize ``obj`` to UTF-8 JSON bytes."""
    option = _BASE_OPTIONS | (orjson.OPT_SORT_KEYS if sort_keys else 0)
    try:
        return orjson.dumps(obj, default=_default, option=option)
    except orjson.JSONEncodeError as exc:
        # orjson wraps errors raised by ``default``; surface the original reason.
        raise TypeError(str(exc.__cause__ or exc)) from exc


def dumps_str(obj: object, *, sort_keys: bool = False) -> str:
    """Serialize ``obj`` to a JSON string."""
    return dumps(obj, sort_keys=sort_keys).decode()


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON from bytes-like or str input."""
    return orjson.loads(data)
