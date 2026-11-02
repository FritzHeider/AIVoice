"""The ``media:pods`` load registry (specs/03-data-model.md §11, specs/07-media-pipeline.md §1).

Every 2 s the pod writes ``HSET media:pods <pod_id> {"active_calls": n, "draining": bool, "updated_at": iso}``
and removes its field at shutdown. Redis errors are logged; the loop keeps going.
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.json import dumps_str
from va_common.logging import get_logger
from va_common.time import Clock
from va_media.calls import CallRegistry
from va_media.drain import DrainState

__all__ = ["PODS_KEY", "PUBLISH_INTERVAL_S", "PodRegistry"]

PODS_KEY: Final = "media:pods"
PUBLISH_INTERVAL_S: Final = 2.0

_log = get_logger(__name__)


class PodRegistry:
    """Publishes this pod's load to ``media:pods``."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators
        self,
        redis: Redis,
        pod_id: str,
        *,
        calls: CallRegistry,
        drain: DrainState,
        clock: Clock,
        interval_s: float = PUBLISH_INTERVAL_S,
    ) -> None:
        self._redis = redis
        self._pod_id = pod_id
        self._calls = calls
        self._drain = drain
        self._clock = clock
        self._interval_s = interval_s

    def entry(self) -> str:
        """The JSON value of this pod's field."""
        return dumps_str(
            {
                "active_calls": self._calls.active,
                "draining": self._drain.draining,
                "updated_at": self._clock.now().isoformat(),
            }
        )

    async def publish(self) -> None:
        """Write the current load once; a Redis failure is logged."""
        try:
            await self._redis.hset(PODS_KEY, self._pod_id, self.entry())
        except (RedisError, OSError) as exc:
            _log.warning("media.pods_publish_failed", pod_id=self._pod_id, detail=type(exc).__name__)

    async def run(self) -> None:
        """Publish every ``interval_s`` until cancelled."""
        while True:
            await self.publish()
            await self._clock.sleep(self._interval_s)

    async def remove(self) -> None:
        """Delete this pod's field (shutdown); a Redis failure is logged."""
        try:
            await self._redis.hdel(PODS_KEY, self._pod_id)
        except (RedisError, OSError) as exc:
            _log.warning("media.pods_remove_failed", pod_id=self._pod_id, detail=type(exc).__name__)
