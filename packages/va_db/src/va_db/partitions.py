"""Monthly ``call_events`` partition maintenance (specs/03-data-model.md §10, specs/28 §8.1).

``call_events`` is range-partitioned on ``created_at``. Each partition is named ``call_events_pYYYYMM``
and holds the UTC month ``[month start, next month start)``: the same names and bounds the
``0001_initial`` migration uses for the current UTC month plus the next 3. RetentionWorkflow calls
:func:`ensure_call_event_partitions` to keep months ahead available and
:func:`drop_call_event_partitions_before` to drop months past the longest retention.

Creating or dropping a partition requires ownership of ``call_events`` (the migration login role), so
both functions take an ``AsyncConnection`` of an engine from ``create_engine(settings, None)``;
``va_app`` and ``va_system`` cannot run them. They run in the caller's transaction and never commit.
``DROP TABLE`` on a partition holds an ACCESS EXCLUSIVE lock on the parent until that transaction
ends, which the daily 03:17 UTC job accepts (``DETACH PARTITION CONCURRENTLY`` cannot run inside a
transaction).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from va_common.logging import get_logger
from va_common.time import ensure_utc

__all__ = [
    "CALL_EVENTS_PARTITION_PREFIX",
    "DEFAULT_MONTHS_AHEAD",
    "MonthPartition",
    "call_event_partition",
    "drop_call_event_partitions_before",
    "ensure_call_event_partitions",
    "existing_call_event_partitions",
    "expired_call_event_partitions",
    "month_start",
    "partition_month",
    "planned_call_event_partitions",
]

CALL_EVENTS_PARTITION_PREFIX: Final = "call_events_p"
DEFAULT_MONTHS_AHEAD: Final = 3

_PARTITION_NAME: Final = re.compile(r"^call_events_p(\d{4})(\d{2})$")
_MONTHS_PER_YEAR: Final = 12
_BOUND_FORMAT: Final = "%Y-%m-%d %H:%M:%S+00"
_EXISTING_SQL: Final = (
    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
    "WHERE i.inhparent = 'call_events'::regclass ORDER BY 1"
)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MonthPartition:
    """One ``call_events`` partition: ``name`` covers ``[start, end)``, both aware UTC month starts."""

    name: str
    start: datetime
    end: datetime


def month_start(moment: datetime, offset: int = 0) -> datetime:
    """The first instant of the UTC month of ``moment`` shifted by ``offset`` months; rejects naive input."""
    utc = ensure_utc(moment)
    index = utc.year * _MONTHS_PER_YEAR + utc.month - 1 + offset
    return datetime(index // _MONTHS_PER_YEAR, index % _MONTHS_PER_YEAR + 1, 1, tzinfo=UTC)


def call_event_partition(month: datetime) -> MonthPartition:
    """The partition holding the UTC month of ``month``."""
    start = month_start(month)
    return MonthPartition(f"{CALL_EVENTS_PARTITION_PREFIX}{start:%Y%m}", start, month_start(start, 1))


def planned_call_event_partitions(
    now: datetime, months_ahead: int = DEFAULT_MONTHS_AHEAD
) -> tuple[MonthPartition, ...]:
    """Partitions for the UTC month of ``now`` and the ``months_ahead`` months after it, in month order."""
    if months_ahead < 0:
        msg = f"months_ahead must not be negative: {months_ahead}"
        raise ValueError(msg)
    first = month_start(now)
    return tuple(call_event_partition(month_start(first, offset)) for offset in range(months_ahead + 1))


def partition_month(name: str) -> datetime | None:
    """The month start encoded in a ``call_events_pYYYYMM`` name, or ``None`` for any other name."""
    match = _PARTITION_NAME.match(name)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if year < 1 or not 1 <= month <= _MONTHS_PER_YEAR:
        return None
    return datetime(year, month, 1, tzinfo=UTC)


def expired_call_event_partitions(names: Iterable[str], cutoff: datetime) -> tuple[str, ...]:
    """The ``call_events_pYYYYMM`` names whose month ends at or before ``cutoff``, in month order."""
    limit = ensure_utc(cutoff)
    months = {name: month for name in names if (month := partition_month(name)) is not None}
    return tuple(
        sorted(
            (name for name, month in months.items() if month_start(month, 1) <= limit), key=months.__getitem__
        )
    )


async def existing_call_event_partitions(conn: AsyncConnection) -> tuple[str, ...]:
    """The names of every partition currently attached to ``call_events``, sorted."""
    result = await conn.execute(text(_EXISTING_SQL))
    return tuple(str(name) for name in result.scalars())


def _quoted(conn: AsyncConnection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


async def ensure_call_event_partitions(
    conn: AsyncConnection, now: datetime, months_ahead: int = DEFAULT_MONTHS_AHEAD
) -> list[str]:
    """Create the missing :func:`planned_call_event_partitions`; return the created names in order."""
    planned = planned_call_event_partitions(now, months_ahead)
    existing = set(await existing_call_event_partitions(conn))
    created: list[str] = []
    for partition in planned:
        if partition.name in existing:
            continue
        # DDL takes no bind parameters; the bounds come only from our own datetimes and the name is quoted.
        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {_quoted(conn, partition.name)} PARTITION OF call_events "
                f"FOR VALUES FROM ('{partition.start:{_BOUND_FORMAT}}') "
                f"TO ('{partition.end:{_BOUND_FORMAT}}')"
            )
        )
        _log.info("db.call_event_partition_created", partition=partition.name)
        created.append(partition.name)
    return created


async def drop_call_event_partitions_before(conn: AsyncConnection, cutoff: datetime) -> list[str]:
    """Drop each ``call_events_pYYYYMM`` partition holding only rows older than ``cutoff``, in month order."""
    expired = expired_call_event_partitions(await existing_call_event_partitions(conn), cutoff)
    for name in expired:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_quoted(conn, name)}"))
        _log.info("db.call_event_partition_dropped", partition=name)
    return list(expired)
