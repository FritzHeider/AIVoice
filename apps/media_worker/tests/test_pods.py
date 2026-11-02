"""``media:pods`` load registry (specs/03 §11, specs/07 §1)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import fakeredis
from clock_helpers import ManualClock
from media_helpers import FakeMediaContainer, media_app
from service_helpers import json_log_stream, log_events, make_client

from va_media.calls import CallRegistry
from va_media.drain import DrainState
from va_media.pods import PODS_KEY, PodRegistry


def rig(
    server: fakeredis.FakeServer | None = None,
) -> tuple[fakeredis.FakeAsyncRedis, PodRegistry, CallRegistry, DrainState, ManualClock]:
    redis = fakeredis.FakeAsyncRedis(server=server or fakeredis.FakeServer())
    calls, drain, clock = CallRegistry(3), DrainState(), ManualClock()
    return redis, PodRegistry(redis, "pod-a", calls=calls, drain=drain, clock=clock), calls, drain, clock


async def entry(redis: fakeredis.FakeAsyncRedis) -> dict[str, object]:
    raw = await redis.hget(PODS_KEY, "pod-a")
    assert raw is not None
    value: dict[str, object] = json.loads(raw)
    return value


async def test_publish_writes_load() -> None:
    redis, pods, calls, drain, clock = rig()
    calls.admit("call-1")
    await pods.publish()
    value = await entry(redis)
    assert value == {"active_calls": 1, "draining": False, "updated_at": value["updated_at"]}
    assert datetime.fromisoformat(str(value["updated_at"])) == clock.now()
    drain.draining = True
    await pods.publish()
    assert (await entry(redis))["draining"] is True


async def test_run_publishes_every_2_s_and_survives_redis_errors() -> None:
    stream = json_log_stream()
    server = fakeredis.FakeServer()
    redis, pods, calls, _, clock = rig(server)
    task = asyncio.ensure_future(pods.run())
    await clock.wait_for_sleep(2.0)
    assert (await entry(redis))["active_calls"] == 0
    calls.admit("call-1")
    server.connected = False
    await clock.run_for(2.0)
    assert not task.done()
    server.connected = True
    await clock.run_for(2.0)
    assert (await entry(redis))["active_calls"] == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert any(e["event"] == "media.pods_publish_failed" for e in log_events(stream))


async def test_remove_deletes_the_field_and_logs_errors() -> None:
    stream = json_log_stream()
    server = fakeredis.FakeServer()
    redis, pods, *_ = rig(server)
    await pods.publish()
    await pods.remove()
    assert await redis.hexists(PODS_KEY, "pod-a") == 0
    server.connected = False
    await pods.remove()
    assert any(e["event"] == "media.pods_remove_failed" for e in log_events(stream))


async def test_lifespan_publishes_and_removes(models_dir: Path) -> None:
    container = FakeMediaContainer()
    clock = ManualClock()
    async with make_client(media_app(models_dir, container, clock=clock)):
        await clock.wait_for_sleep(2.0)
        assert await container.redis.hexists(PODS_KEY, "pod-test")
    assert not await container.redis.hexists(PODS_KEY, "pod-test")
