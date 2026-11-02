"""``CallSession``: heartbeat, ownership renewal, lock loss, end requests, cleanup (specs/07 §19)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

import fakeredis
import pytest
from clock_helpers import ManualClock
from media_helpers import FakeSocket, call_context, stop_msg
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, ServiceMetrics, build_metrics, new_registry
from va_domain.carrier import heartbeat_key, owner_key
from va_media.ownership import CallOwnership
from va_media.session import OWNERSHIP_LOST, STREAM_CLOSED, CallSession, SessionEnd, StreamStart
from va_media.stream import read_until_stop
from va_telnyx.media import MediaFormat

START = StreamStart(
    stream_id="stream-1",
    call_control_id="cc-1",
    media_format=MediaFormat(encoding="PCMU", sample_rate=8000, channels=1),
)


class FlakyOwnership(CallOwnership):
    """Renewals raise a Redis connection error while ``failing``."""

    failing = False

    async def renew(self) -> bool:
        if self.failing:
            msg = "redis down"
            raise RedisConnectionError(msg)
        return await super().renew()


@dataclass
class Rig:
    server: fakeredis.FakeServer
    redis: fakeredis.FakeAsyncRedis
    clock: ManualClock
    socket: FakeSocket
    session: CallSession
    ownership: CallOwnership
    metrics: ServiceMetrics

    def ended(self, label: str) -> float | None:
        return self.metrics.registry.get_sample_value("va_media_calls_ended_total", {"end_reason": label})


def stamp(raw: object) -> datetime:
    assert isinstance(raw, bytes)
    return datetime.fromisoformat(raw.decode())


async def forever(session: CallSession) -> SessionEnd:
    await asyncio.Event().wait()
    return STREAM_CLOSED


@asynccontextmanager
async def rig(*, flaky: bool = False) -> AsyncIterator[Rig]:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server)
    call_id = uuid.uuid4()
    ownership_cls = FlakyOwnership if flaky else CallOwnership
    ownership = ownership_cls(redis, call_id, "pod-test")
    assert await ownership.acquire()
    clock = ManualClock()
    socket = FakeSocket()
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    session = CallSession(
        context=call_context(call_id),
        socket=socket,
        start=START,
        ownership=ownership,
        redis=redis,
        clock=clock,
        metrics=metrics,
    )
    yield Rig(server, redis, clock, socket, session, ownership, metrics)


async def test_heartbeat_written_at_start_and_every_3_s_then_deleted() -> None:
    async with rig() as r:
        running = asyncio.ensure_future(r.session.run(read_until_stop))
        await r.clock.wait_for_sleep(3.0)
        key = heartbeat_key(r.session.call_id)
        first = await r.redis.get(key)
        assert first is not None
        assert stamp(first) == r.clock.now()
        assert 0 < await r.redis.ttl(key) <= 10
        await r.clock.run_for(3.0)
        second = await r.redis.get(key)
        assert second is not None
        assert stamp(second) == r.clock.now()
        assert second != first
        r.socket.feed(stop_msg())
        assert await running == STREAM_CLOSED
        assert await r.redis.exists(key) == 0
        assert await r.redis.exists(owner_key(r.session.call_id)) == 0, "released at the end"
        assert r.socket.closed_with == 1000
        assert r.ended("stream_closed") == 1
        assert r.metrics.registry.get_sample_value("va_media_calls_started_total") == 1


async def test_lock_taken_by_another_pod_ends_with_worker_lost() -> None:
    async with rig() as r:
        running = asyncio.ensure_future(r.session.run(forever))
        await r.clock.wait_for_sleep(5.0)
        await r.redis.set(owner_key(r.session.call_id), "pod-other")
        await r.clock.run_for(5.0)
        end = await running
        assert end == OWNERSHIP_LOST
        assert (end.end_reason, end.ended_by, end.label) == ("worker_lost", "error", "worker_lost")
        assert r.session.ownership_lost
        assert await r.redis.get(owner_key(r.session.call_id)) == b"pod-other", "never released"
        assert await r.redis.exists(heartbeat_key(r.session.call_id)) == 0
        assert r.socket.closed_with == 1000
        assert r.ended("worker_lost") == 1


async def test_renewal_keeps_the_lock_alive() -> None:
    async with rig() as r:
        running = asyncio.ensure_future(r.session.run(forever))
        await r.clock.wait_for_sleep(5.0)
        key = owner_key(r.session.call_id)
        await r.redis.pexpire(key, 100)
        await r.clock.run_for(5.0)
        assert await r.redis.pttl(key) > 14_000
        assert not running.done()
        r.session.request_end("completed", "agent")
        assert await running == SessionEnd("ended", end_reason="completed", ended_by="agent")


async def test_renew_errors_tolerated_below_the_lock_ttl() -> None:
    async with rig(flaky=True) as r:
        assert isinstance(r.ownership, FlakyOwnership)
        running = asyncio.ensure_future(r.session.run(forever))
        await r.clock.wait_for_sleep(5.0)
        r.ownership.failing = True
        await r.clock.run_for(10.0)  # failures at 5 s and 10 s
        assert not running.done()
        r.ownership.failing = False
        await r.clock.run_for(5.0)  # renewed at 15 s
        r.ownership.failing = True
        await r.clock.run_for(10.0)  # failures at 20 s and 25 s: 10 s since the last renewal
        assert not running.done()
        await r.clock.run_for(5.0)  # 30 s: 15 s without a renewal
        assert await running == OWNERSHIP_LOST
        assert await r.redis.get(owner_key(r.session.call_id)) == b"pod-test", "lost: not released"


async def test_request_end_first_wins() -> None:
    async with rig() as r:
        running = asyncio.ensure_future(r.session.run(forever))
        await r.clock.wait_for_sleep(3.0)
        r.session.request_end("voicemail", "agent")
        r.session.request_end("completed", "user")
        end = await running
        assert (end.cause, end.end_reason, end.ended_by, end.label) == (
            "ended",
            "voicemail",
            "agent",
            "voicemail",
        )
        assert r.ended("voicemail") == 1


async def test_body_exception_propagates_after_cleanup() -> None:
    async def broken(session: CallSession) -> SessionEnd:
        msg = "pipeline failed"
        raise RuntimeError(msg)

    async with rig() as r:
        with pytest.raises(RuntimeError, match="pipeline failed"):
            await r.session.run(broken)
        assert await r.redis.exists(heartbeat_key(r.session.call_id)) == 0
        assert await r.redis.exists(owner_key(r.session.call_id)) == 0
        assert r.socket.closed_with == 1011
        assert r.ended("error") == 1


async def test_disconnect_ends_with_stream_closed() -> None:
    async with rig() as r:
        r.socket.feed(b"not json")
        r.socket.hang_up()
        assert await r.session.run(read_until_stop) == STREAM_CLOSED


async def test_redis_errors_during_heartbeat_and_cleanup_are_logged() -> None:
    stream = json_log_stream()
    async with rig() as r:
        running = asyncio.ensure_future(r.session.run(read_until_stop))
        await r.clock.wait_for_sleep(3.0)
        r.server.connected = False
        await r.clock.run_for(3.0)
        assert not running.done(), "a failed heartbeat does not end the call"
        r.socket.hang_up()
        assert await running == STREAM_CLOSED
        assert r.ended("stream_closed") == 1
    events = {e["event"] for e in log_events(stream)}
    assert {
        "media.heartbeat_failed",
        "media.heartbeat_delete_failed",
        "media.ownership_release_failed",
    } <= events
