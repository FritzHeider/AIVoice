"""The per-call ownership lock ``call:{<id>}:owner`` (specs/07-media-pipeline.md §3 step 2, §19).

The pod that accepts a call's primary media stream takes the lock with ``SET NX PX 15000`` (value = its
``POD_ID``) and renews it every 5 s with a compare-and-``PEXPIRE``; release is a compare-and-``DEL``, so a
pod never extends or deletes a lock another pod has taken over.
"""

from __future__ import annotations

import uuid
from typing import Final

from redis.asyncio import Redis

from va_domain.carrier import owner_key

__all__ = ["OWNER_TTL_MS", "RENEW_INTERVAL_S", "CallOwnership"]

OWNER_TTL_MS: Final = 15_000
RENEW_INTERVAL_S: Final = 5.0

_RENEW: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class CallOwnership:
    """The ownership lock of one call held by ``pod_id``; Redis errors propagate to the caller."""

    def __init__(self, redis: Redis, call_id: uuid.UUID, pod_id: str) -> None:
        self.call_id = call_id
        self.key = owner_key(call_id)
        self.pod_id = pod_id
        self._redis = redis
        self._renew = redis.register_script(_RENEW)
        self._release = redis.register_script(_RELEASE)

    async def acquire(self) -> bool:
        """Take the lock; ``False`` when any pod (this one included) already holds it."""
        return bool(await self._redis.set(self.key, self.pod_id, nx=True, px=OWNER_TTL_MS))

    async def renew(self) -> bool:
        """Extend the lock to 15 s; ``False`` when it is gone or held by another pod."""
        return bool(await self._renew(keys=[self.key], args=[self.pod_id, OWNER_TTL_MS]))

    async def release(self) -> bool:
        """Delete the lock if this pod holds it; ``False`` when it is gone or held by another pod."""
        return bool(await self._release(keys=[self.key], args=[self.pod_id]))
