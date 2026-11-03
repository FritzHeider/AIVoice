"""The events sink consumer: event streams → ClickHouse (specs/19 §1 Decision P06-T05; specs/03 §12).

One consumer group ``events-sink`` reads all five ``events:<stream>`` streams; the consumer name is the pod
(``POD_ID`` or the host name). Each stream runs one :class:`StreamSink`:

1. On start (and after a Redis error) it re-reads the entries delivered to its own consumer name but never
   acknowledged, batch by batch, then reads new entries (stale entries of other pods first, ``XAUTOCLAIM``).
2. Entries collect into a batch that is flushed at ``batch_rows`` (1 000) entries, when ``batch_interval_s``
   (1 s, injected clock) has passed since the batch's first entry, or when a blocking read for the rest of the
   window returns nothing.
3. A flush maps every event (:func:`~va_events_sink.rows.rows_for_event`; an unmappable event is parked with
   reason ``unmappable:<reason>``), inserts the batch table by table, and acknowledges every entry only after
   all inserts succeeded (at-least-once).
4. Transient insert errors retry the same table after ``0.5 s`` doubling up to ``5 s`` until it succeeds. When
   ClickHouse or the driver rejects a table's batch, the remaining tables are inserted event by event and an
   event whose own insert is rejected is parked with reason ``insert_failed:<table>``.
5. Parking (``events:dead``) happens before the acknowledgement. A Redis error drops the in-memory batch (its
   entries stay pending), waits with the same backoff, and starts again at step 1.

``va_events_sink_lag_seconds{stream}`` is refreshed after every read cycle (:mod:`va_events_sink.lag`).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_common.tracing import resolve_pod
from va_events import STREAMS, DeadLetter, Stream, StreamConsumer, StreamEntry, encode
from va_events_sink.lag import stream_lag
from va_events_sink.rows import Row, RowError, TableRows, rows_for_event
from va_events_sink.schema import TRANSIENT_ERRORS, SchemaState
from va_events_sink.writer import ClickHouseInserts, insert_table, is_rejected, is_transient

__all__ = ["CONSUMER_GROUP", "SinkConfig", "SinkConnect", "StreamSink", "build_sinks", "run_sink"]

CONSUMER_GROUP: Final = "events-sink"
_REDIS_ERRORS: Final = (RedisError, OSError)

type SinkConnect = Callable[[], AbstractAsyncContextManager[ClickHouseInserts]]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SinkConfig:
    """Batching, retry, and consumer-group settings of the sink."""

    group: str = CONSUMER_GROUP
    consumer: str = field(default_factory=resolve_pod)
    streams: tuple[Stream, ...] = STREAMS
    batch_rows: int = 1_000
    batch_interval_s: float = 1.0
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 5.0
    min_idle_ms: int = 60_000


class _Backoff:
    """``initial`` doubling up to ``maximum``; :meth:`reset` starts over."""

    def __init__(self, config: SinkConfig) -> None:
        self._config = config
        self.delay = config.initial_backoff_s
        self.attempt = 1

    def reset(self) -> None:
        self.delay = self._config.initial_backoff_s
        self.attempt = 1

    async def wait(self, clock: Clock) -> None:
        await clock.sleep(self.delay)
        self.delay = min(self.delay * 2, self._config.max_backoff_s)
        self.attempt += 1


def _merge(mapped: Sequence[tuple[StreamEntry, TableRows]]) -> TableRows:
    merged: TableRows = {}
    for _, rows in mapped:
        for table, table_rows in rows.items():
            merged.setdefault(table, []).extend(table_rows)
    return merged


class StreamSink:
    """Moves one stream's events into ClickHouse."""

    def __init__(  # noqa: PLR0913 - the stream's consumer plus keyword-only collaborators
        self,
        consumer: StreamConsumer,
        client: ClickHouseInserts,
        dead_letter: DeadLetter,
        *,
        redis: Redis,
        clock: Clock,
        metrics: ServiceMetrics,
        config: SinkConfig,
    ) -> None:
        self._consumer = consumer
        self._client = client
        self._dead = dead_letter
        self._redis = redis
        self._clock = clock
        self._metrics = metrics
        self._config = config
        self._stream = consumer.key.removeprefix("events:")
        self._batch: list[StreamEntry] = []
        self._first_at = 0.0
        self._flushes = 0
        self._flushed = asyncio.Condition()

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def flushes(self) -> int:
        """How many batches were flushed (inserted and acknowledged)."""
        return self._flushes

    async def wait_flushes(self, count: int) -> None:
        """Wait until at least ``count`` batches were flushed (test seam)."""
        async with self._flushed:
            await self._flushed.wait_for(lambda: self._flushes >= count)

    async def run(self) -> None:
        """Consume until cancelled; Redis errors restart the cycle after a backoff."""
        backoff = _Backoff(self._config)
        while True:
            try:
                await self._consumer.ensure_group()
                await self._recover()
                backoff.reset()
                while True:
                    await self.cycle()
            except _REDIS_ERRORS as exc:
                self._batch.clear()
                _log.warning(
                    "events_sink.redis_retry",
                    stream=self._consumer.key,
                    attempt=backoff.attempt,
                    backoff_s=backoff.delay,
                    error=type(exc).__name__,
                )
                await backoff.wait(self._clock)

    async def _recover(self) -> None:
        while entries := await self._consumer.read_own_pending(count=self._config.batch_rows):
            await self._flush(entries)

    async def cycle(self) -> None:
        """One read, a flush when the batch is due, and a lag refresh."""
        config = self._config
        window_ms = config.batch_interval_s * 1000
        if self._batch:
            window_ms -= (self._clock.monotonic() - self._first_at) * 1000
        entries = await self._consumer.read(
            count=config.batch_rows - len(self._batch),
            block_ms=max(math.ceil(window_ms), 1),
            min_idle_ms=config.min_idle_ms,
        )
        if not entries:
            # Yield once: a client whose blocking read returns early (fakeredis) must not starve the loop.
            await asyncio.sleep(0)
        elif not self._batch:
            self._first_at = self._clock.monotonic()
        self._batch.extend(entries)
        if self._due(read_nothing=not entries):
            batch, self._batch = self._batch, []
            await self._flush(batch)
        await self._refresh_lag()

    def _due(self, *, read_nothing: bool) -> bool:
        if not self._batch:
            return False
        if read_nothing or len(self._batch) >= self._config.batch_rows:
            return True
        return self._clock.monotonic() - self._first_at >= self._config.batch_interval_s

    async def _flush(self, entries: Sequence[StreamEntry]) -> None:
        mapped: list[tuple[StreamEntry, TableRows]] = []
        for entry in entries:
            try:
                mapped.append((entry, rows_for_event(entry.event)))
            except RowError as exc:
                await self._park(entry, f"unmappable:{exc.reason}")
        await self._insert_batch(mapped)
        await self._consumer.ack([entry.entry_id for entry in entries])
        async with self._flushed:
            self._flushes += 1
            self._flushed.notify_all()

    async def _insert_batch(self, mapped: Sequence[tuple[StreamEntry, TableRows]]) -> None:
        merged = _merge(mapped)
        tables = list(merged)
        for position, table in enumerate(tables):
            if not await self._insert(table, merged[table]):
                await self._insert_each(mapped, tables[position:])
                return

    async def _insert_each(
        self, mapped: Sequence[tuple[StreamEntry, TableRows]], tables: Sequence[str]
    ) -> None:
        for entry, rows in mapped:
            for table in tables:
                if table in rows and not await self._insert(table, rows[table]):
                    await self._park(entry, f"insert_failed:{table}")
                    break

    async def _insert(self, table: str, rows: Sequence[Row]) -> bool:
        """Insert, retrying transient errors; ``False`` when the data was rejected. Other errors propagate."""
        backoff = _Backoff(self._config)
        while True:
            try:
                await insert_table(self._client, table, rows, self._metrics)
            except Exception as exc:
                if is_transient(exc):
                    _log.warning(
                        "events_sink.insert_retry",
                        table=table,
                        rows=len(rows),
                        attempt=backoff.attempt,
                        backoff_s=backoff.delay,
                        error=type(exc).__name__,
                    )
                    await backoff.wait(self._clock)
                    continue
                if is_rejected(exc):
                    _log.warning(
                        "events_sink.insert_rejected", table=table, rows=len(rows), error=type(exc).__name__
                    )
                    return False
                raise
            return True

    async def _park(self, entry: StreamEntry, reason: str) -> None:
        await self._dead.park(self._consumer.key, entry.entry_id, encode(entry.event), reason)
        _log.warning(
            "events_sink.parked",
            stream=self._consumer.key,
            entry_id=entry.entry_id,
            event_type=entry.event.type.value,
            reason=reason,
        )

    async def _refresh_lag(self) -> None:
        lag = await stream_lag(self._redis, self._consumer.key, self._consumer.group, self._clock.now())
        self._metrics.gauge("va_events_sink_lag_seconds").labels(stream=self._stream).set(lag)


async def _open_client(
    stack: AsyncExitStack, connect: SinkConnect, *, clock: Clock, config: SinkConfig
) -> ClickHouseInserts:
    backoff = _Backoff(config)
    while True:
        try:
            return await stack.enter_async_context(connect())
        except TRANSIENT_ERRORS as exc:
            _log.warning(
                "events_sink.connect_retry",
                attempt=backoff.attempt,
                backoff_s=backoff.delay,
                error=type(exc).__name__,
            )
            await backoff.wait(clock)


def build_sinks(  # noqa: PLR0913 - the shared collaborators of every stream's sink
    redis: Redis,
    client: ClickHouseInserts,
    *,
    clock: Clock,
    metrics: ServiceMetrics,
    config: SinkConfig,
    dead_letter: DeadLetter | None = None,
) -> list[StreamSink]:
    """One :class:`StreamSink` per configured stream, all parking into one ``events:dead``."""
    dead = dead_letter if dead_letter is not None else DeadLetter(redis)
    return [
        StreamSink(
            StreamConsumer(redis, stream, config.group, config.consumer, dead_letter=dead),
            client,
            dead,
            redis=redis,
            clock=clock,
            metrics=metrics,
            config=config,
        )
        for stream in config.streams
    ]


async def run_sink(  # noqa: PLR0913 - the sink's collaborators, keyword-only after the inputs
    redis: Redis,
    connect: SinkConnect,
    schema: SchemaState,
    *,
    clock: Clock,
    metrics: ServiceMetrics,
    config: SinkConfig | None = None,
    started: Callable[[Sequence[StreamSink]], None] | None = None,
) -> None:
    """After the schema is applied: open a ClickHouse client (transient errors retried) and run each stream's
    sink until cancelled. ``started`` receives the sinks once they run (test seam)."""
    cfg = config if config is not None else SinkConfig()
    await schema.wait()
    async with AsyncExitStack() as stack:
        client = await _open_client(stack, connect, clock=clock, config=cfg)
        sinks = build_sinks(redis, client, clock=clock, metrics=metrics, config=cfg)
        _log.info("events_sink.started", consumer=cfg.consumer, streams=list(cfg.streams))
        async with asyncio.TaskGroup() as group:
            for sink in sinks:
                group.create_task(sink.run(), name=f"events-sink-{sink.stream}")
            if started is not None:
                started(sinks)
