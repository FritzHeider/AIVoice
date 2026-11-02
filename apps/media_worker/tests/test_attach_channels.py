"""``/ws/telnyx`` after ``start``: context refusals, control and carrier channels, the carrier snapshot
(specs/07 §3 steps 3 and 6, §17; specs/07 §3 Decision P05-T07b)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from media_helpers import close_code, served, start_msg, stop_msg
from redis.exceptions import ConnectionError as RedisConnectionError

from va_domain.carrier import CarrierEvent, carrier_channel, carrier_state_key, owner_key
from va_domain.control import SayControl, StopControl, control_ack_key, control_channel
from va_media import server
from va_media.carrier_state import CarrierSnapshot
from va_media.channels import CallFeed, ChannelError
from va_media.context import ContextRefusedError


@pytest.mark.parametrize(
    ("reason", "code"), [("call_status", 4401), ("call_unknown", 4401), ("database", 1011), ("redis", 1011)]
)
async def test_context_refusals_close_and_release(models_dir: Path, reason: str, code: int) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        s.loader.refusals[call_id] = ContextRefusedError(reason)  # type: ignore[arg-type]
        async with s.connect(call_id) as ws:
            await ws.send(start_msg())
            assert await close_code(ws) == code
        assert s.loader.loads == [(call_id, "cc-1")]
        assert await s.container.redis.exists(owner_key(call_id)) == 0
        assert s.rejected(reason) == 1
        assert not s.body.started.is_set()
        assert s.media.registry.active == 0


async def test_stop_control_ends_the_call_and_is_acknowledged(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            session = await s.attach(ws)
            assert session.context == s.loader.contexts[call_id]
            say = SayControl(id=uuid.uuid4(), text="hold on")
            stop = StopControl(id=uuid.uuid4())
            redis = s.container.redis
            assert await redis.publish(control_channel(call_id), say.model_dump_json()) == 1
            assert await redis.publish(control_channel(call_id), stop.model_dump_json()) == 1
            assert await close_code(ws) == 1000
        await s.media.registry.wait_idle(5)
        assert await redis.exists(control_ack_key(call_id, stop.id)) == 1
        assert await redis.exists(control_ack_key(call_id, say.id)) == 0
        assert s.metric("va_media_calls_ended_total", end_reason="api_stop") == 1
        assert await redis.publish(control_channel(call_id), stop.model_dump_json()) == 0  # unsubscribed
        assert len(s.media.sessions) == 0


def carrier_event(call_id: uuid.UUID, event_id: str, kind: str) -> CarrierEvent:
    return CarrierEvent(
        type=kind,
        leg="primary",
        call_id=call_id,
        org_id=uuid.uuid4(),
        telnyx_event_id=event_id,
        occurred_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )


async def test_carrier_state_before_and_events_after_subscription(models_dir: Path) -> None:
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        redis = s.container.redis
        await redis.hset(carrier_state_key(call_id), mapping={"answered": "1", "amd_result": "human"})
        async with s.connect(call_id) as ws:
            session = await s.attach(ws)
            assert session.carrier_state.answered
            assert session.carrier_state.amd_result == "human"
            event = carrier_event(call_id, "ev-9", "call.machine.premium.detection.ended")
            assert await redis.publish(carrier_channel(call_id), event.model_dump_json()) == 1
            async with asyncio.timeout(5):
                assert await session.feed.next_carrier() == event
            await ws.send(stop_msg())
            assert await close_code(ws) == 1000


async def test_subscription_failure_closes_1011(models_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(models_dir) as s:

        async def refuse(call_id: uuid.UUID) -> CallFeed:
            raise ChannelError("timeout")

        monkeypatch.setattr(s.app.state.media_attach.hub, "subscribe", refuse)
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await ws.send(start_msg())
            assert await close_code(ws) == 1011
        assert s.rejected("subscribe") == 1
        assert await s.container.redis.exists(owner_key(call_id)) == 0


async def test_snapshot_failure_closes_1011_and_unsubscribes(
    models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(redis: object, call_id: uuid.UUID) -> CarrierSnapshot:
        msg = "redis down"
        raise RedisConnectionError(msg)

    monkeypatch.setattr(server, "read_carrier_state", broken)
    async with served(models_dir) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await ws.send(start_msg())
            assert await close_code(ws) == 1011
        await s.media.registry.wait_idle(5)
        assert s.rejected("redis") == 1
        assert s.app.state.media_attach.hub.subscribed_channels == set()
        assert await s.container.redis.exists(owner_key(call_id)) == 0
