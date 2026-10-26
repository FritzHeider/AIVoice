"""Graceful drain marker (specs/01-architecture.md §3: Redis set ``media:pods:draining``).

On SIGTERM the pod adds its id to the ``media:pods:draining`` set and its readiness check ``draining`` fails,
so no new media WebSocket is routed to it; the id is removed again when the process exits.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import Gauge
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger

__all__ = ["DRAINING_PODS_KEY", "DrainState", "DrainingError"]

DRAINING_PODS_KEY: Final = "media:pods:draining"

_log = get_logger(__name__)


class DrainingError(RuntimeError):
    """The pod is draining (readiness check failure)."""


class DrainState:
    """Whether this pod drains; mirrored to Redis and to the ``va_media_draining`` gauge when given."""

    def __init__(self, gauge: Gauge | None = None) -> None:
        self.draining = False
        self._gauge = gauge
        if gauge is not None:
            gauge.set(0)

    async def begin(self, redis: Redis, pod_id: str) -> None:
        """Start draining (idempotent); a Redis failure is logged and the pod still drains."""
        if self.draining:
            return
        self.draining = True
        if self._gauge is not None:
            self._gauge.set(1)
        _log.info("media.drain_started", pod_id=pod_id)
        try:
            await redis.sadd(DRAINING_PODS_KEY, pod_id)
        except (RedisError, OSError) as exc:
            _log.warning("media.drain_mark_failed", pod_id=pod_id, detail=type(exc).__name__)

    async def clear(self, redis: Redis, pod_id: str) -> None:
        """Remove the pod from the draining set on exit; a Redis failure is logged."""
        try:
            await redis.srem(DRAINING_PODS_KEY, pod_id)
        except (RedisError, OSError) as exc:
            _log.warning("media.drain_clear_failed", pod_id=pod_id, detail=type(exc).__name__)

    async def check(self) -> None:
        """Readiness check ``draining``: fails once draining began."""
        if self.draining:
            msg = "pod is draining"
            raise DrainingError(msg)
