"""In-process LRU + TTL cache of unwrapped data keys (specs/28-security.md §3: 10 000 entries, 10 min)."""

from __future__ import annotations

from collections import OrderedDict
from uuid import UUID

from va_common.crypto.kms import WrappedDataKey
from va_common.time import Clock, SystemClock

__all__ = ["DataKeyCache"]

type _CacheKey = tuple[UUID, str, bytes]


class DataKeyCache:
    """Maps ``(org_id, kek_id, encrypted_data_key)`` to the unwrapped key; evicts the LRU entry first."""

    def __init__(
        self, *, max_entries: int = 10_000, ttl_seconds: float = 600.0, clock: Clock | None = None
    ) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            msg = "max_entries must be >= 1 and ttl_seconds must be > 0"
            raise ValueError(msg)
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._entries: OrderedDict[_CacheKey, tuple[float, bytes]] = OrderedDict()

    @staticmethod
    def _key(org_id: UUID, wrapped: WrappedDataKey) -> _CacheKey:
        return (org_id, wrapped.kek_id, wrapped.encrypted_data_key)

    def get(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes | None:
        key = self._key(org_id, wrapped)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, plaintext = entry
        if self._clock.monotonic() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return plaintext

    def put(self, org_id: UUID, wrapped: WrappedDataKey, plaintext: bytes) -> None:
        key = self._key(org_id, wrapped)
        self._entries[key] = (self._clock.monotonic() + self._ttl, plaintext)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
