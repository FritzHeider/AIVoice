"""The carrier state stored before a session subscribed (specs/07-media-pipeline.md §3 step 6).

The telephony edge keeps the latest carrier facts of a call in the hash ``call:{<id>}:carrier_state`` (field
names written by ``va_edge.mapping``: ``answered``, ``answered_at``, ``amd_result``, ``answered_by``,
``hangup_cause``, ...). The session reads it once, right after its carrier channel subscription is confirmed,
so an event stored before the subscription is in the snapshot and one published after it arrives on the
channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from redis.asyncio import Redis

from va_domain.carrier import carrier_state_key

__all__ = ["CarrierSnapshot", "read_carrier_state"]


def _text(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


@dataclass(frozen=True, slots=True)
class CarrierSnapshot:
    """The carrier state hash of one call (empty when the edge stored nothing yet)."""

    fields: Mapping[str, str] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.fields.get("answered") == "1"

    @property
    def answered_at(self) -> datetime | None:
        raw = self.fields.get("answered_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @property
    def amd_result(self) -> str | None:
        return self.fields.get("amd_result") or None

    @property
    def answered_by(self) -> str | None:
        return self.fields.get("answered_by") or None

    @property
    def hangup_cause(self) -> str | None:
        return self.fields.get("hangup_cause") or None


async def read_carrier_state(redis: Redis, call_id: uuid.UUID) -> CarrierSnapshot:
    """``HGETALL call:{<id>}:carrier_state``; Redis errors propagate."""
    raw = await redis.hgetall(carrier_state_key(call_id))
    return CarrierSnapshot({_text(key): _text(value) for key, value in raw.items()})
