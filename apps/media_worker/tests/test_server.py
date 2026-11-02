"""``/ws/telnyx`` attach over real uvicorn: auth, admission, ownership, start timeout, session end
(specs/07 §1-§3, §19; specs/07 §3 Decision P05-T07a)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import fakeredis
from media_helpers import (
    FakeMediaContainer,
    close_code,
    connected_msg,
    served,
    start_msg,
    stop_msg,
)
from service_helpers import json_log_stream, log_events

from va_domain.carrier import heartbeat_key, owner_key
from va_media.session import STREAM_CLOSED, CallSession, SessionEnd


async def test_bad_signature_closes_4401_without_reading(models_dir: Path) -> None:
    stream = json_log_stream()
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id, signed_host="evil.test") as ws:
            assert await close_code(ws) == 4401
        async with s.connect(call_id, token=s.keys.token(uuid.uuid4(), s.clock.now())) as ws:
            assert await close_code(ws) == 4401
        assert not s.body.started.is_set()
        assert s.rejected("auth") == 2
        assert await s.container.redis.exists(owner_key(call_id)) == 0
    details = [e["detail"] for e in log_events(stream) if e["event"] == "media.ws_rejected"]
    assert details == ["invalid_signature", "token_call_mismatch"]


async def test_attach_runs_the_session_and_cleans_up_on_stop(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            session = await s.attach(ws)
            assert session.call_id == call_id
            assert (session.start.stream_id, session.start.call_control_id) == ("stream-1", "cc-1")
            assert (session.start.media_format.encoding, session.start.media_format.sample_rate) == (
                "PCMU",
                8000,
            )
            redis = s.container.redis
            assert await redis.get(owner_key(call_id)) == b"pod-test"
            assert await redis.exists(heartbeat_key(call_id)) == 1
            assert s.media.registry.contains(str(call_id))
            assert s.metric("va_media_active_calls", transport="telnyx") == 1
            await ws.send(stop_msg())
            assert await close_code(ws) == 1000
        assert await s.media.registry.wait_idle(5)
        assert s.body.ends == [STREAM_CLOSED]
        assert await redis.exists(owner_key(call_id), heartbeat_key(call_id)) == 0
        assert s.metric("va_media_calls_started_total") == 1
        assert s.metric("va_media_calls_ended_total", end_reason="stream_closed") == 1
        assert s.metric("va_media_active_calls", transport="telnyx") == 0


async def test_second_stream_for_an_attached_call_is_refused(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as first:
            await s.attach(first)
            async with s.connect(call_id) as second:
                assert await close_code(second) == 4409
            assert s.rejected("duplicate") == 1
            assert s.media.registry.active == 1, "the first stream keeps the call"
            await first.send(stop_msg())
            assert await close_code(first) == 1000


async def test_stream_owned_by_another_pod_is_refused(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        await s.container.redis.set(owner_key(call_id), "pod-other")
        async with s.connect(call_id) as ws:
            assert await close_code(ws) == 4409
        assert await s.media.registry.wait_idle(5)
        assert await s.container.redis.get(owner_key(call_id)) == b"pod-other"
        assert s.rejected("duplicate") == 1


async def test_capacity_and_draining_refuse_with_4503(models_dir: Path) -> None:
    async with served(models_dir, media_max_calls_per_pod=1) as s:
        async with s.connect(uuid.uuid4()) as first:
            await s.attach(first)
            async with s.connect(uuid.uuid4()) as second:
                assert await close_code(second) == 4503
            assert s.rejected("capacity") == 1
            s.media.drain.draining = True
            await first.send(stop_msg())
            assert await close_code(first) == 1000, "draining finishes the existing call"
        assert await s.media.registry.wait_idle(5)
        async with s.connect(uuid.uuid4()) as third:
            assert await close_code(third) == 4503
        assert s.rejected("draining") == 1


async def test_secondary_leg_is_misrouted(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id, leg="warm_proxy") as ws:
            assert await close_code(ws) == 4421
        assert s.rejected("misrouted_leg") == 1
        assert await s.container.redis.exists(owner_key(call_id)) == 0


async def test_no_start_within_5_s_closes_1008_and_releases(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await ws.send(connected_msg())
            await s.clock.wait_for_sleep(5.0)
            assert await s.container.redis.get(owner_key(call_id)) == b"pod-test"
            await s.clock.run_for(5.0)
            assert await close_code(ws) == 1008
        assert await s.media.registry.wait_idle(5)
        assert await s.container.redis.exists(owner_key(call_id)) == 0
        assert s.rejected("start_timeout") == 1
        assert not s.body.started.is_set()


async def test_stop_or_garbage_before_start_closes_1008(models_dir: Path) -> None:
    async with served(models_dir) as s:
        for first in (stop_msg(), "not json"):
            call_id = uuid.uuid4()
            async with s.connect(call_id) as ws:
                await ws.send(first)
                assert await close_code(ws) == 1008
            assert await s.media.registry.wait_idle(5)
            assert await s.container.redis.exists(owner_key(call_id)) == 0
        assert not s.body.started.is_set()


async def test_redis_unavailable_closes_1011(models_dir: Path) -> None:
    server = fakeredis.FakeServer()
    container = FakeMediaContainer(redis=fakeredis.FakeAsyncRedis(server=server))
    async with served(models_dir, container=container) as s:
        server.connected = False
        async with s.connect(uuid.uuid4()) as ws:
            assert await close_code(ws) == 1011
        assert await s.media.registry.wait_idle(5)
        assert s.rejected("redis") == 1


async def test_failed_session_is_logged_and_closed_1011(models_dir: Path) -> None:
    stream = json_log_stream()
    started = asyncio.Event()

    async def broken(session: CallSession) -> SessionEnd:
        started.set()
        msg = "pipeline failed"
        raise RuntimeError(msg)

    async with served(models_dir, body=broken) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await ws.send(start_msg())
            assert await close_code(ws) == 1011
        assert await s.media.registry.wait_idle(5)
        assert started.is_set()
        assert await s.container.redis.exists(owner_key(call_id)) == 0
        assert s.metric("va_media_calls_ended_total", end_reason="error") == 1
    assert any(e["event"] == "media.session_failed" for e in log_events(stream))


async def test_peer_disconnects_end_the_session_or_the_attach(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await s.attach(ws)
            await ws.send(b"\x00binary garbage")
        async with asyncio.timeout(5):
            await s.body.ended.wait()
        assert await s.media.registry.wait_idle(5)
        assert s.body.ends == [STREAM_CLOSED]
        before_start = uuid.uuid4()
        async with s.connect(before_start) as ws:
            await ws.send(connected_msg())
        assert await s.media.registry.wait_idle(5)
        assert await s.container.redis.exists(owner_key(call_id), owner_key(before_start)) == 0


async def test_release_failure_after_a_failed_start_is_logged(models_dir: Path) -> None:
    stream = json_log_stream()
    server = fakeredis.FakeServer()
    container = FakeMediaContainer(redis=fakeredis.FakeAsyncRedis(server=server))
    async with served(models_dir, container=container) as s:
        async with s.connect(uuid.uuid4()) as ws:
            await ws.send(connected_msg())
            await s.clock.wait_for_sleep(5.0)
            server.connected = False
            await ws.send(stop_msg())
            assert await close_code(ws) == 1008
        assert await s.media.registry.wait_idle(5)
    assert any(e["event"] == "media.ownership_release_failed" for e in log_events(stream))
