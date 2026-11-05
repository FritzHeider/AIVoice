"""``CallSession``: heartbeat, ownership renewal, lock loss, end requests, cleanup (specs/07 §19)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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
from va_domain.control import ControlMessage, DrainNoticeControl
from va_media.control import ControlHandler
from va_media.ownership import OWNER_TTL_MS, CallOwnership
from va_media.session import (
    HEARTBEAT_INTERVAL_S,
    OWNERSHIP_LOST,
    STREAM_CLOSED,
    TEARDOWN_KEEPALIVE_S,
    CallSession,
    SessionEnd,
    StreamStart,
)
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


class RecordingEnder:
    """Records what it saw when the session ended (lock and heartbeat still present?)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.redis: fakeredis.FakeAsyncRedis | None = None
        self.seen: list[tuple[SessionEnd | None, bool, bool, int | None]] = []

    async def finish(self, session: CallSession, end: SessionEnd | None) -> None:
        assert self.redis is not None
        assert isinstance(session.socket, FakeSocket)
        locked = await self.redis.exists(owner_key(session.call_id)) == 1
        beating = await self.redis.exists(heartbeat_key(session.call_id)) == 1
        self.seen.append((end, locked, beating, session.socket.closed_with))
        if self.fail:
            msg = "ender failed"
            raise RuntimeError(msg)


@asynccontextmanager
async def rig(
    *,
    flaky: bool = False,
    ender: RecordingEnder | None = None,
    control_handler: ControlHandler | None = None,
) -> AsyncIterator[Rig]:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server)
    call_id = uuid.uuid4()
    ownership_cls = FlakyOwnership if flaky else CallOwnership
    ownership = ownership_cls(redis, call_id, "pod-test")
    assert await ownership.acquire()
    clock = ManualClock()
    socket = FakeSocket()
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    if ender is not None:
        ender.redis = redis
    session = CallSession(
        context=call_context(call_id),
        socket=socket,
        start=START,
        ownership=ownership,
        redis=redis,
        clock=clock,
        metrics=metrics,
        control_handler=control_handler,
        ender=ender,
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


async def test_the_ender_runs_before_the_lock_release_and_the_heartbeat_delete() -> None:
    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        running = asyncio.ensure_future(r.session.run(read_until_stop))
        await r.clock.wait_for_sleep(3.0)
        r.socket.feed(stop_msg())
        assert await running == STREAM_CLOSED
        assert ender.seen == [(STREAM_CLOSED, True, True, None)]
        assert await r.redis.exists(owner_key(r.session.call_id)) == 0
        assert await r.redis.exists(heartbeat_key(r.session.call_id)) == 0
        assert r.socket.closed_with == 1000


async def test_the_ender_gets_none_when_the_body_raised() -> None:
    async def broken(session: CallSession) -> SessionEnd:
        msg = "pipeline failed"
        raise RuntimeError(msg)

    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        with pytest.raises(RuntimeError, match="pipeline failed"):
            await r.session.run(broken)
        assert [seen[0] for seen in ender.seen] == [None]
        assert r.socket.closed_with == 1011


async def test_a_failing_ender_is_logged_and_the_cleanup_still_runs() -> None:
    stream = json_log_stream()
    ender = RecordingEnder(fail=True)
    async with rig(ender=ender) as r:
        r.session.request_end("api_stop", "api")
        end = await r.session.run(forever)
        assert end == SessionEnd("ended", end_reason="api_stop", ended_by="api")
        assert len(ender.seen) == 1
        assert await r.redis.exists(owner_key(r.session.call_id)) == 0
        assert await r.redis.exists(heartbeat_key(r.session.call_id)) == 0
        assert r.socket.closed_with == 1000
    failed = [e for e in log_events(stream) if e["event"] == "media.end_routine_failed"]
    assert [e["error_type"] for e in failed] == ["RuntimeError"]


class GatedTeardown:
    """A body that runs until cancelled, then holds its teardown until ``gate`` is set."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.tearing = asyncio.Event()

    async def __call__(self, session: CallSession) -> SessionEnd:  # noqa: ARG002 - the SessionBody shape
        try:
            await asyncio.Event().wait()
        finally:
            self.tearing.set()
            await self.gate.wait()
        return STREAM_CLOSED


async def test_the_heartbeat_and_lock_are_kept_while_the_body_tears_down() -> None:
    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        body = GatedTeardown()
        running = asyncio.ensure_future(r.session.run(body))
        await r.clock.wait_for_sleep(3.0)
        r.session.request_end("completed", "user")
        await body.tearing.wait()
        heartbeat = heartbeat_key(r.session.call_id)
        owner = owner_key(r.session.call_id)
        await r.redis.pexpire(owner, 100)
        await r.clock.run_for(12.0)  # heartbeats at 3, 6, 9, 12 s; renewals at 5, 10 s
        beat = await r.redis.get(heartbeat)
        assert beat is not None
        assert stamp(beat) == r.clock.now(), "the heartbeat kept beating through the teardown"
        assert await r.redis.pttl(owner) > 14_000, "the lock kept being renewed"
        assert ender.seen == [], "the end routine waits for the body's teardown"
        assert not running.done()
        body.gate.set()
        assert await running == SessionEnd("ended", end_reason="completed", ended_by="user")
        assert ender.seen == [
            (SessionEnd("ended", end_reason="completed", ended_by="user"), True, True, None)
        ]
        assert await r.redis.exists(heartbeat) == 0
        assert await r.redis.exists(owner) == 0
        assert r.socket.closed_with == 1000


async def _lose_lock(r: Rig) -> None:
    """Another pod takes the lock; the renewal due in 5 s finds it gone (the renewer must be 5 s from it)."""
    await r.redis.set(owner_key(r.session.call_id), "pod-other")
    await r.clock.run_for(5.0)
    assert r.session.ownership_lost


async def _overrun(r: Rig) -> None:
    """The body's teardown runs past the keepalive: the renewer and the heartbeat stop."""
    await r.clock.run_for(TEARDOWN_KEEPALIVE_S)
    assert r.clock.sleepers == 0, "no keeper is left sleeping"


_ABANDON: dict[str, Callable[[Rig], Awaitable[None]]] = {"lost": _lose_lock, "overran": _overrun}


async def _in_teardown(
    r: Rig, decide: Callable[[CallSession], None]
) -> tuple[GatedTeardown, asyncio.Future[SessionEnd]]:
    """Run the session over a gated body, let ``decide`` end its race (the renewer is then 5 s from its next
    renewal) and return once the body's teardown is being held."""
    body = GatedTeardown()
    running = asyncio.ensure_future(r.session.run(body))
    await r.clock.wait_for_sleep(5.0)
    decide(r.session)
    await body.tearing.wait()
    return body, running


async def test_a_lock_lost_during_the_body_teardown_ends_the_session_worker_lost() -> None:
    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        body, running = await _in_teardown(r, lambda session: session.request_end("completed", "agent"))
        await _lose_lock(r)  # the renewal at 5 s finds the lock taken
        beat = await r.redis.get(heartbeat_key(r.session.call_id))
        assert beat is not None
        await r.clock.run_for(9.0)
        assert await r.redis.get(heartbeat_key(r.session.call_id)) == beat, "no beat after the lock was lost"
        assert not running.done()
        assert ender.seen == []
        body.gate.set()
        assert await running == OWNERSHIP_LOST, "the requested end does not stay: the workflow ends the call"
        assert [seen[0] for seen in ender.seen] == [OWNERSHIP_LOST]
        assert r.ended("worker_lost") == 1
        assert r.ended("completed") is None
        assert await r.redis.get(owner_key(r.session.call_id)) == b"pod-other", "never released"
        assert await r.redis.exists(heartbeat_key(r.session.call_id)) == 0
        assert r.socket.closed_with == 1000


@pytest.mark.parametrize("how", ["lost", "overran"])
async def test_a_transferred_end_stays_when_the_session_stops_vouching_for_the_call(how: str) -> None:
    transferred = SessionEnd("ended", end_reason="transferred", ended_by="system")
    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        body, running = await _in_teardown(r, lambda session: session.request_end("transferred", "system"))
        await _ABANDON[how](r)
        assert not running.done()
        body.gate.set()
        assert await running == transferred, "the caller and the target stay bridged"
        assert [seen[0] for seen in ender.seen] == [transferred]
        assert r.ended("transferred") == 1
        assert r.ended("worker_lost") is None
        assert r.socket.closed_with == 1000


class Exploding:
    """A control handler that fails the session."""

    async def handle(self, session: CallSession, message: ControlMessage) -> bool:  # noqa: ARG002
        msg = "handler bug"
        raise RuntimeError(msg)


async def test_a_failed_session_that_lost_the_lock_gives_the_ender_worker_lost() -> None:
    ender = RecordingEnder()
    async with rig(ender=ender, control_handler=Exploding()) as r:
        notice = DrainNoticeControl(id=uuid.uuid4())
        body, running = await _in_teardown(r, lambda session: session.notify(notice))
        await _lose_lock(r)
        assert not running.done()
        body.gate.set()
        with pytest.raises(RuntimeError, match="handler bug"):
            await running
        assert [seen[0] for seen in ender.seen] == [OWNERSHIP_LOST], "not None: the routine must not hang up"
        assert await r.redis.get(owner_key(r.session.call_id)) == b"pod-other", "never released"
        assert r.socket.closed_with == 1011
        assert r.ended("error") == 1
        assert r.ended("worker_lost") is None


async def test_a_wedged_teardown_stops_the_keepalive_after_the_bound() -> None:
    stream = json_log_stream()
    ender = RecordingEnder()
    async with rig(ender=ender) as r:
        body = GatedTeardown()
        running = asyncio.ensure_future(r.session.run(body))
        await r.clock.wait_for_sleep(HEARTBEAT_INTERVAL_S)
        r.session.request_end("completed", "user")
        await body.tearing.wait()
        heartbeat = heartbeat_key(r.session.call_id)
        owner = owner_key(r.session.call_id)
        await r.clock.run_for(TEARDOWN_KEEPALIVE_S - 1.0)  # last beat at 24 s, last renewal at 20 s
        beat = await r.redis.get(heartbeat)
        assert beat is not None
        last_beat = stamp(beat)
        assert last_beat == r.clock.now(), "kept alive within the bound"
        assert not [e for e in log_events(stream) if e["event"] == "media.body_teardown_overran"]
        await r.clock.run_for(1.0)
        overran = [e for e in log_events(stream) if e["event"] == "media.body_teardown_overran"]
        assert [e["keepalive_s"] for e in overran] == [TEARDOWN_KEEPALIVE_S]
        await r.redis.pexpire(owner, 60_000)
        await r.clock.run_for(15.0)  # beats would be due at 27, 30, ... s; renewals at 25, 30, 35 s
        beat = await r.redis.get(heartbeat)
        assert beat is not None
        assert stamp(beat) == last_beat, "the heartbeat stopped at the bound"
        assert await r.redis.pttl(owner) > OWNER_TTL_MS, "no renewal (it resets 15 s)"
        assert r.clock.sleepers == 0, "no keeper is left sleeping"
        assert not running.done(), "the session keeps waiting for the body"
        assert ender.seen == []
        body.gate.set()
        assert await running == OWNERSHIP_LOST, "both keepers stopped: the workflow ends the call"
        assert [seen[0] for seen in ender.seen] == [OWNERSHIP_LOST]
        assert r.ended("worker_lost") == 1
        assert r.ended("completed") is None
        assert await r.redis.exists(heartbeat) == 0
        assert await r.redis.exists(owner) == 0, "released (the lock was never lost)"
        assert not r.session.ownership_lost
        assert r.socket.closed_with == 1000
