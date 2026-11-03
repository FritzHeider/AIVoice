"""Redis consumer-group reader of the event streams (specs/19-events-and-webhooks.md §1).

``StreamConsumer(redis, stream, group, consumer)`` reads with ``XREADGROUP GROUP <group> <consumer>``; each
:meth:`read` first claims entries other consumers left pending longer than ``min_idle_ms`` (60 s) with
``XAUTOCLAIM``, then reads new ones. Callers :meth:`ack` after processing. A payload that is not a
:class:`DomainEvent` is acknowledged, logged ``events.stream_poison`` (entry id and stream only), and skipped;
with a ``dead_letter`` it is first parked in ``events:dead`` with reason ``undecodable`` (a parking failure
propagates and leaves the entry pending). Entries deleted from the stream while pending (reported by
``XAUTOCLAIM``, or returned without fields by :meth:`read_own_pending`) are acknowledged away.
:meth:`read_own_pending` re-reads the entries delivered to this consumer name but never acknowledged (a
restarted pod picks up its own work at once instead of waiting for the 60 s claim).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from va_common.logging import get_logger
from va_events.catalog import Stream
from va_events.dead_letter import DeadLetter
from va_events.keys import stream_key
from va_events.model import DomainEvent, decode
from va_events.publisher import PAYLOAD_FIELD

__all__ = ["DEFAULT_MIN_IDLE_MS", "StreamConsumer", "StreamEntry"]

DEFAULT_MIN_IDLE_MS: Final = 60_000
DEFAULT_COUNT: Final = 100
DEFAULT_BLOCK_MS: Final = 1_000
_START: Final = "0-0"
_OWN_PENDING: Final = "0"
UNDECODABLE: Final = "undecodable"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """One stream entry: its id (for :meth:`StreamConsumer.ack`) and the decoded event."""

    entry_id: str
    event: DomainEvent


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class StreamConsumer:
    """One consumer of a consumer group on an ``events:<stream>`` stream."""

    def __init__(
        self,
        redis: Redis,
        stream: Stream,
        group: str,
        consumer: str,
        *,
        dead_letter: DeadLetter | None = None,
    ) -> None:
        self._redis = redis
        self._key = stream_key(stream)
        self._group = group
        self._consumer = consumer
        self._dead_letter = dead_letter

    @property
    def group(self) -> str:
        return self._group

    @property
    def key(self) -> str:
        return self._key

    async def ensure_group(self) -> None:
        """Create the group at the stream's end (and the stream itself); an existing group is kept."""
        try:
            await self._redis.xgroup_create(self._key, self._group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def claim_stale(
        self, *, min_idle_ms: int = DEFAULT_MIN_IDLE_MS, count: int = DEFAULT_COUNT
    ) -> list[StreamEntry]:
        """Take over up to ``count`` entries pending longer than ``min_idle_ms`` in other consumers."""
        entries: list[StreamEntry] = []
        cursor: str = _START
        while len(entries) < count:
            reply: Any = await self._redis.xautoclaim(
                self._key,
                self._group,
                self._consumer,
                min_idle_time=min_idle_ms,
                start_id=cursor,
                count=count - len(entries),
            )
            next_cursor, claimed, *rest = reply
            cursor = _text(next_cursor)
            entries.extend(await self._decode(claimed))
            deleted = [_text(entry_id) for entry_id in rest[0]] if rest else []
            if deleted:
                await self.ack(deleted)
            if cursor == _START:
                break
        return entries

    async def read(
        self,
        *,
        count: int = DEFAULT_COUNT,
        block_ms: int = DEFAULT_BLOCK_MS,
        min_idle_ms: int = DEFAULT_MIN_IDLE_MS,
    ) -> list[StreamEntry]:
        """Stale entries claimed from other consumers first, else new entries (blocking ``block_ms``)."""
        claimed = await self.claim_stale(min_idle_ms=min_idle_ms, count=count)
        if claimed:
            return claimed
        reply: Any = await self._redis.xreadgroup(
            self._group, self._consumer, {self._key: ">"}, count=count, block=block_ms
        )
        if not reply:
            return []
        return await self._decode(reply[0][1])

    async def read_own_pending(self, *, count: int = DEFAULT_COUNT) -> list[StreamEntry]:
        """Up to ``count`` entries already delivered to this consumer and not acknowledged (no blocking)."""
        reply: Any = await self._redis.xreadgroup(
            self._group, self._consumer, {self._key: _OWN_PENDING}, count=count
        )
        if not reply:
            return []
        raw_entries = reply[0][1]
        deleted = [_text(raw_id) for raw_id, fields in raw_entries if fields is None]
        if deleted:
            await self.ack(deleted)
        return await self._decode([entry for entry in raw_entries if entry[1] is not None])

    async def ack(self, entry_ids: Sequence[str]) -> int:
        """Acknowledge processed entries; returns how many were pending."""
        if not entry_ids:
            return 0
        return int(await self._redis.xack(self._key, self._group, *entry_ids))

    async def _decode(self, raw_entries: Sequence[Any]) -> list[StreamEntry]:
        entries: list[StreamEntry] = []
        poison: list[str] = []
        for raw_id, fields in raw_entries:
            entry_id = _text(raw_id)
            payload = _payload(fields)
            event = _parse(payload)
            if event is None:
                if self._dead_letter is not None:
                    await self._dead_letter.park(self._key, entry_id, payload or b"", UNDECODABLE)
                poison.append(entry_id)
                _log.warning("events.stream_poison", stream=self._key, entry_id=entry_id)
            else:
                entries.append(StreamEntry(entry_id, event))
        if poison:
            await self.ack(poison)
        return entries


def _payload(fields: dict[Any, Any] | None) -> bytes | None:
    fields = fields or {}
    payload = fields.get(PAYLOAD_FIELD.encode())
    if payload is None:  # a client built with decode_responses=True returns str field names
        payload = fields.get(PAYLOAD_FIELD)
    if payload is None:
        return None
    return payload.encode() if isinstance(payload, str) else bytes(payload)


def _parse(payload: bytes | None) -> DomainEvent | None:
    if payload is None:
        return None
    try:
        return decode(payload)
    except ValidationError:
        return None
