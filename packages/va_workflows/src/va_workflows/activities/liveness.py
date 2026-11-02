"""Liveness activities of ``CallWorkflow``: the heartbeat watch and the presence probe
(specs/01-architecture.md §3 "Media worker crash", §2.1 Decision P05-T06c; specs/07 §19).

``watch_call_heartbeat`` runs for the whole answered phase. It heartbeats to Temporal on every probe (so a
crashed activity worker is noticed within ``WATCH_HEARTBEAT_TIMEOUT`` and the watch restarts) and returns
only once ``call:{<id>}:hb`` has been absent for ``missing_after_s`` seconds. The clock starts when the
watch starts, so a media worker that never attaches is caught too. A Redis error counts as "seen": a Redis
outage must not hang up every call in progress.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError
from temporalio import activity

from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_domain.carrier import heartbeat_key, owner_key
from va_workflows.activities.errors import STATE_UNAVAILABLE, retryable
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.liveness_inputs import CallPresence, WatchInput, WatchResult

__all__ = ["PRESENCE_UNAVAILABLE_MESSAGE", "CallLivenessActivities"]

PRESENCE_UNAVAILABLE_MESSAGE = "The call presence keys could not be read."

_log = get_logger(__name__)


class CallLivenessActivities:
    """Reads the media worker's ownership and heartbeat keys; one instance per worker process."""

    def __init__(self, *, redis: Redis, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    @activity.defn(name="watch_call_heartbeat")
    async def watch_call_heartbeat(self, request: WatchInput) -> WatchResult:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            key = heartbeat_key(request.call_id)
            last_seen = self._clock.monotonic()
            while True:
                activity.heartbeat()
                now = self._clock.monotonic()
                if await self._present(key):
                    last_seen = now
                missing_s = now - last_seen
                if missing_s >= request.missing_after_s:
                    _log.warning("calls.heartbeat_lost", missing_s=missing_s)
                    return WatchResult(reason="heartbeat_lost", missing_s=missing_s)
                await self._clock.sleep(request.poll_interval_s)

    @activity.defn(name="call_presence")
    async def call_presence(self, request: CallActivityInput) -> CallPresence:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            try:
                owner = await self._redis.exists(owner_key(request.call_id))
                heartbeat = await self._redis.exists(heartbeat_key(request.call_id))
            except (RedisError, OSError) as exc:
                raise retryable(PRESENCE_UNAVAILABLE_MESSAGE, STATE_UNAVAILABLE) from exc
            return CallPresence(owner=owner > 0, heartbeat=heartbeat > 0)

    async def _present(self, key: str) -> bool:
        """Whether the heartbeat key exists; a Redis failure is logged and counts as present."""
        try:
            return await self._redis.exists(key) > 0
        except (RedisError, OSError) as exc:
            _log.warning("calls.heartbeat_probe_failed", error=type(exc).__name__)
            return True
