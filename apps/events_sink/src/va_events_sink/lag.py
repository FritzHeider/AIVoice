"""``va_events_sink_lag_seconds{stream}`` (specs/27-observability.md §3.5; specs/19 §1 Decision P06-T05).

The lag of a stream is the age of the oldest entry the group has not acknowledged: the oldest pending entry
(delivered, unacknowledged) or the first entry not yet delivered to the group, whichever is older; 0 when
neither exists. An entry's time is the millisecond part of its id (``<ms>-<seq>``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

__all__ = ["entry_ms", "lag_seconds", "oldest_unacked_ids", "stream_lag"]


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def entry_ms(entry_id: str) -> int:
    """The Unix time in ms of a stream entry id ``<ms>-<seq>``."""
    return int(entry_id.partition("-")[0])


def lag_seconds(entry_ids: Iterable[str], now: datetime) -> float:
    """Age in seconds of the oldest id at ``now`` (never below 0); 0.0 without ids."""
    millis = [entry_ms(entry_id) for entry_id in entry_ids]
    if not millis:
        return 0.0
    age_ms = now.timestamp() * 1000 - min(millis)
    return max(age_ms, 0.0) / 1000


async def oldest_unacked_ids(redis: Redis, key: str, group: str) -> list[str]:
    """The group's oldest pending id and its first undelivered id (each only when it exists; none without
    the stream or the group)."""
    ids: list[str] = []
    try:
        summary: Any = await redis.xpending(key, group)
    except ResponseError:  # NOGROUP: the stream or the group does not exist yet
        return ids
    if summary and summary.get("pending"):
        ids.append(_text(summary["min"]))
    last_delivered = await _last_delivered(redis, key, group)
    if last_delivered is not None:
        nxt: Any = await redis.xrange(key, min=f"({last_delivered}", count=1)
        if nxt:
            ids.append(_text(nxt[0][0]))
    return ids


async def _last_delivered(redis: Redis, key: str, group: str) -> str | None:
    groups: Any = await redis.xinfo_groups(key)
    for info in groups:
        if _text(info.get("name")) == group:
            return _text(info.get("last-delivered-id"))
    return None


async def stream_lag(redis: Redis, key: str, group: str, now: datetime) -> float:
    """The lag in seconds of ``group`` on ``key`` at ``now``."""
    return lag_seconds(await oldest_unacked_ids(redis, key, group), now)
