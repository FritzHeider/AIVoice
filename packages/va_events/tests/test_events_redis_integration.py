"""The publisher and consumer on a real Redis 7: seq under concurrency, XAUTOCLAIM idle time, trimming."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from event_helpers import RecordingWriter, event, stream_events
from redis.asyncio import Redis
from redis_container import running_redis_url
from service_helpers import settings_of

from va_common.redis import redis_client
from va_common.settings import RedisSettings
from va_events import DirectCallEventWriter, EventPublisher, EventType, StreamConsumer
from va_events import publisher as publisher_module
from va_events.keys import stream_key

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with running_redis_url() as url:
        yield url


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = redis_client(settings_of(RedisSettings, redis_url=redis_url), max_connections=256)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


async def test_real_redis_bus(redis: Redis, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = RecordingWriter()
    publisher = EventPublisher(redis, call_events=DirectCallEventWriter(writer))
    call_id = uuid.uuid4()

    published = await asyncio.gather(
        *(publisher.publish(event(EventType.CALL_TURN, call_id=call_id)) for _ in range(200))
    )
    assert sorted(e.seq or 0 for e in published) == list(range(1, 201))
    assert sorted(r.seq for r in writer.rows) == list(range(1, 201))

    pod_a = StreamConsumer(redis, "calls", "worker", "pod-a")
    pod_b = StreamConsumer(redis, "calls", "worker", "pod-b")
    await pod_a.ensure_group()
    first = await publisher.publish(event(EventType.CALL_ENDED, call_id=call_id))
    (taken,) = await pod_a.read(block_ms=10)
    assert taken.event == first
    assert await pod_b.claim_stale(min_idle_ms=60_000) == []
    await asyncio.sleep(0.1)  # real Redis idle time: the entry must sit pending longer than min_idle_ms
    claimed = await pod_b.claim_stale(min_idle_ms=50)
    assert [entry.entry_id for entry in claimed] == [taken.entry_id]
    assert await pod_b.ack([taken.entry_id]) == 1

    monkeypatch.setattr(publisher_module, "STREAM_MAXLEN", 100)
    for _ in range(2_000):
        await publisher.publish(event(EventType.KB_READY))
    length = await redis.xlen(stream_key("system"))
    assert 100 <= length < 2_000
    assert {e.type for e in await stream_events(redis, stream_key("system"))} == {EventType.KB_READY}
