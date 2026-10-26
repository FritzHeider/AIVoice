"""Drain marker in Redis and the readiness checks around it (specs/01-architecture.md §3)."""

from __future__ import annotations

import fakeredis
import pytest
from prometheus_client import CollectorRegistry, Gauge
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, build_metrics, new_registry
from va_media.drain import DRAINING_PODS_KEY, DrainingError, DrainState


def draining_gauge() -> tuple[Gauge, CollectorRegistry]:
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    return metrics.gauge("va_media_draining"), metrics.registry


async def test_begin_marks_pod_once_and_clear_removes_it() -> None:
    redis: Redis = fakeredis.FakeAsyncRedis()
    gauge, registry = draining_gauge()
    state = DrainState(gauge)
    assert registry.get_sample_value("va_media_draining") == 0
    await state.check()
    await state.begin(redis, "pod-a")
    await state.begin(redis, "pod-a")
    assert state.draining
    assert registry.get_sample_value("va_media_draining") == 1
    assert await redis.smembers(DRAINING_PODS_KEY) == {b"pod-a"}
    with pytest.raises(DrainingError):
        await state.check()
    await state.clear(redis, "pod-a")
    assert await redis.smembers(DRAINING_PODS_KEY) == set()
    await redis.aclose()


class BrokenRedis:
    async def sadd(self, *_args: object) -> int:
        raise RedisConnectionError("refused")

    async def srem(self, *_args: object) -> int:
        raise RedisConnectionError("refused")


async def test_redis_errors_are_logged_and_pod_still_drains() -> None:
    stream = json_log_stream()
    state = DrainState()
    broken: Redis = BrokenRedis()  # type: ignore[assignment]
    await state.begin(broken, "pod-b")
    await state.clear(broken, "pod-b")
    assert state.draining
    events = {e["event"]: e for e in log_events(stream)}
    assert events["media.drain_mark_failed"]["detail"] == "ConnectionError"
    assert events["media.drain_clear_failed"]["pod_id"] == "pod-b"
