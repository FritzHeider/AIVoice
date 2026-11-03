"""ClickHouse inserts of the events sink (specs/03-data-model.md §12; specs/19 §1 Decision P06-T05).

:func:`insert_table` writes one table's rows (``va.<table>`` with the :data:`TABLE_COLUMNS` order) and counts
``va_events_sink_inserted_total{table}`` (rows) per insert and ``va_events_sink_errors_total{table}``
per failed one. Errors are classified for the caller: :func:`is_transient` (connection problems and retried
server errors: retry the whole batch later) versus :func:`is_rejected` (the server or the driver refused the
data: isolate the offending event).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Protocol

from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from va_common.metrics import ServiceMetrics
from va_events.clickhouse import ClickHouseCommands
from va_events_sink.rows import TABLE_COLUMNS, Row

__all__ = [
    "DATABASE",
    "TRANSIENT_ERRORS",
    "ClickHouseInserts",
    "insert_table",
    "is_rejected",
    "is_transient",
]

DATABASE: Final = "va"
TRANSIENT_ERRORS: Final = (OperationalError, OSError)
_DATA_ERRORS: Final = (ClickHouseError, ValueError, TypeError, OverflowError)


class ClickHouseInserts(ClickHouseCommands, Protocol):
    """The part of ``clickhouse_connect`` ``AsyncClient`` the sink uses (the applier's plus ``insert``)."""

    async def insert(
        self,
        table: str | None = None,
        data: Sequence[Sequence[Any]] | None = None,
        column_names: str | Sequence[str] | None = "*",
        database: str | None = None,
    ) -> object: ...


def is_transient(error: BaseException) -> bool:
    """A connection problem or a retried server error: the same insert may succeed later."""
    return isinstance(error, TRANSIENT_ERRORS)


def is_rejected(error: BaseException) -> bool:
    """The server or the driver refused the data itself (bad value, type, or SQL): retrying cannot help."""
    return not is_transient(error) and isinstance(error, _DATA_ERRORS)


async def insert_table(
    client: ClickHouseInserts, table: str, rows: Sequence[Row], metrics: ServiceMetrics
) -> None:
    """Insert ``rows`` into ``va.<table>`` (spec column order), counting the outcome; errors propagate."""
    try:
        await client.insert(table, rows, column_names=TABLE_COLUMNS[table], database=DATABASE)
    except Exception:
        metrics.counter("va_events_sink_errors_total").labels(table=table).inc()
        raise
    metrics.counter("va_events_sink_inserted_total").labels(table=table).inc(len(rows))
