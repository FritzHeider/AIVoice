"""Parking of stream entries a consumer cannot process (``events:dead``, specs/03-data-model.md §11).

``DeadLetter(redis).park(stream_key, entry_id, payload, reason)`` appends one entry to ``events:dead``
(``MAXLEN ~ 100 000``) with the fields ``stream``, ``entry_id``, ``reason``, ``payload`` (the raw bytes of the
original entry, empty when it had none). Callers acknowledge the original entry only after ``park`` returned;
a Redis failure propagates so the entry stays pending and is read again.
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

from va_events.keys import DEAD_STREAM_KEY, DEAD_STREAM_MAXLEN

__all__ = ["DEAD_FIELDS", "DeadLetter"]

DEAD_FIELDS: Final = ("stream", "entry_id", "reason", "payload")


class DeadLetter:
    """Writes unprocessable entries to ``events:dead``."""

    def __init__(self, redis: Redis, *, key: str = DEAD_STREAM_KEY, maxlen: int = DEAD_STREAM_MAXLEN) -> None:
        self._redis = redis
        self._key = key
        self._maxlen = maxlen

    @property
    def key(self) -> str:
        return self._key

    async def park(self, stream_key: str, entry_id: str, payload: bytes, reason: str) -> str:
        """Append the entry; returns the ``events:dead`` entry id."""
        dead_id = await self._redis.xadd(
            self._key,
            {"stream": stream_key, "entry_id": entry_id, "reason": reason, "payload": payload},
            maxlen=self._maxlen,
            approximate=True,
        )
        return dead_id.decode() if isinstance(dead_id, bytes) else str(dead_id)
