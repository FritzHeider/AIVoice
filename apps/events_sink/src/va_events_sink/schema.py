"""ClickHouse schema bootstrap of the events sink (specs/03-data-model.md §12, specs/27-observability.md §9).

Startup does not wait for ClickHouse: a supervised task applies the migrations, retrying connection errors
with a doubling backoff, and the ``clickhouse_schema`` readiness check fails until it has succeeded. Schema
drift and SQL errors are fatal: the task fails and the supervisor stops the process with exit code 1.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Final

from clickhouse_connect.driver.exceptions import OperationalError

from va_common.logging import get_logger
from va_common.time import Clock
from va_events.clickhouse import ClickHouseCommands, apply_migrations

__all__ = ["TRANSIENT_ERRORS", "SchemaConnect", "SchemaState", "bootstrap_schema"]

# clickhouse-connect reports refused/reset connections, timeouts, and retried server errors as
# OperationalError.
TRANSIENT_ERRORS: Final = (OperationalError, OSError)

type SchemaConnect = Callable[[], AbstractAsyncContextManager[ClickHouseCommands]]

_log = get_logger(__name__)


@dataclass(slots=True)
class SchemaState:
    """Whether this process has applied the ClickHouse migrations."""

    _applied: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def applied(self) -> bool:
        return self._applied.is_set()

    def mark_applied(self) -> None:
        self._applied.set()

    async def wait(self) -> None:
        await self._applied.wait()

    async def check(self) -> None:
        """Readiness check ``clickhouse_schema``."""
        if not self.applied:
            msg = "clickhouse schema not applied"
            raise ConnectionError(msg)


async def bootstrap_schema(
    connect: SchemaConnect,
    state: SchemaState,
    *,
    clock: Clock,
    initial_backoff_s: float = 0.5,
    max_backoff_s: float = 5.0,
) -> None:
    """Apply the migrations, retrying transient errors after ``clock.sleep`` (doubling up to ``max``)."""
    backoff = initial_backoff_s
    attempt = 1
    while not state.applied:
        try:
            async with connect() as client:
                applied = await apply_migrations(client, clock=clock)
        except TRANSIENT_ERRORS as exc:
            _log.warning(
                "clickhouse.schema_retry", attempt=attempt, backoff_s=backoff, error=type(exc).__name__
            )
            await clock.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)
            attempt += 1
            continue
        state.mark_applied()
        _log.info("clickhouse.schema_ready", attempts=attempt, applied=applied)
