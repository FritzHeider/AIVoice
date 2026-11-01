"""``StreamConsumer``: consumer groups, acks, XAUTOCLAIM of stale entries, poison payloads (specs/19 §1)."""

from __future__ import annotations

import uuid

import pytest
from event_helpers import PublisherRig, event, publisher_rig
from fakeredis import FakeAsyncRedis
from service_helpers import json_log_stream, log_events

from va_events import EventType, StreamConsumer
from va_events.keys import stream_key


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


@pytest.fixture
def rig() -> PublisherRig:
    return publisher_rig()


def _consumer(rig: PublisherRig, name: str, stream: str = "calls") -> StreamConsumer:
    return StreamConsumer(rig.redis, stream, "worker", name)  # type: ignore[arg-type]


async def _pending(rig: PublisherRig, key: str = "events:calls") -> int:
    return int((await rig.redis.xpending(key, "worker"))["pending"])


async def test_ensure_group_creates_the_stream_and_is_idempotent(rig: PublisherRig) -> None:
    consumer = _consumer(rig, "pod-a")
    await consumer.ensure_group()
    await consumer.ensure_group()
    assert consumer.key == stream_key("calls")
    groups = await rig.redis.xinfo_groups(consumer.key)
    assert [g["name"] for g in groups] == [b"worker"]


async def test_the_group_starts_at_the_end_of_the_stream(rig: PublisherRig) -> None:
    await rig.publisher.publish(event(EventType.CALL_QUEUED))
    consumer = _consumer(rig, "pod-a")
    await consumer.ensure_group()
    assert await consumer.read(block_ms=1) == []


async def test_read_returns_events_and_ack_clears_the_pending_list(rig: PublisherRig) -> None:
    consumer = _consumer(rig, "pod-a")
    await consumer.ensure_group()
    call_id = uuid.uuid4()
    published = [await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_id)) for _ in range(3)]
    entries = await consumer.read(block_ms=1)
    assert [entry.event for entry in entries] == published
    assert await _pending(rig) == 3
    assert await consumer.ack([entry.entry_id for entry in entries]) == 3
    assert await _pending(rig) == 0
    assert await consumer.ack([]) == 0
    assert await consumer.read(block_ms=1) == []


async def test_read_respects_count(rig: PublisherRig) -> None:
    consumer = _consumer(rig, "pod-a")
    await consumer.ensure_group()
    for _ in range(5):
        await rig.publisher.publish(event(EventType.CALL_QUEUED))
    assert len(await consumer.read(count=2, block_ms=1)) == 2
    assert len(await consumer.read(count=10, block_ms=1)) == 3


async def test_stale_entries_of_another_consumer_are_claimed_first(rig: PublisherRig) -> None:
    pod_a, pod_b = _consumer(rig, "pod-a"), _consumer(rig, "pod-b")
    await pod_a.ensure_group()
    stale = await rig.publisher.publish(event(EventType.CALL_QUEUED))
    (taken,) = await pod_a.read(block_ms=1)
    fresh = await rig.publisher.publish(event(EventType.CALL_DIALING))
    claimed = await pod_b.read(block_ms=1, min_idle_ms=0)
    assert [(entry.entry_id, entry.event) for entry in claimed] == [(taken.entry_id, stale)]
    assert [entry.event for entry in await pod_b.read(block_ms=1)] == [fresh]
    pending = await rig.redis.xpending_range(pod_a.key, "worker", min="-", max="+", count=10)
    assert [p["consumer"] for p in pending] == [b"pod-b", b"pod-b"]


async def test_fresh_entries_are_not_claimed_before_the_idle_threshold(rig: PublisherRig) -> None:
    pod_a, pod_b = _consumer(rig, "pod-a"), _consumer(rig, "pod-b")
    await pod_a.ensure_group()
    await rig.publisher.publish(event(EventType.CALL_QUEUED))
    assert len(await pod_a.read(block_ms=1)) == 1
    assert await pod_b.claim_stale() == []
    assert await pod_b.read(block_ms=1) == []


async def test_claim_stale_stops_at_count(rig: PublisherRig) -> None:
    pod_a, pod_b = _consumer(rig, "pod-a"), _consumer(rig, "pod-b")
    await pod_a.ensure_group()
    for _ in range(5):
        await rig.publisher.publish(event(EventType.CALL_QUEUED))
    assert len(await pod_a.read(block_ms=1)) == 5
    assert len(await pod_b.claim_stale(min_idle_ms=0, count=2)) == 2
    assert len(await pod_b.claim_stale(min_idle_ms=0, count=10)) == 5


async def test_poison_payloads_are_acked_logged_and_skipped(rig: PublisherRig) -> None:
    stream = json_log_stream()
    consumer = _consumer(rig, "pod-a")
    await consumer.ensure_group()
    bad_json = await rig.redis.xadd(consumer.key, {"payload": b"not json"})
    no_payload = await rig.redis.xadd(consumer.key, {"other": b"x"})
    good = await rig.publisher.publish(event(EventType.CALL_QUEUED))
    entries = await consumer.read(block_ms=1)
    assert [entry.event for entry in entries] == [good]
    assert await _pending(rig) == 1
    poison = [e for e in log_events(stream) if e["event"] == "events.stream_poison"]
    assert {e["entry_id"] for e in poison} == {_text(bad_json), _text(no_payload)}
    assert all(e["stream"] == "events:calls" for e in poison)
    assert all("payload" not in e for e in poison)


async def test_a_decode_responses_client_reads_events(rig: PublisherRig) -> None:
    text_redis = FakeAsyncRedis(server=rig.server, decode_responses=True)
    consumer = StreamConsumer(text_redis, "calls", "worker", "pod-text")
    await consumer.ensure_group()
    published = await rig.publisher.publish(event(EventType.CALL_QUEUED))
    entries = await consumer.read(block_ms=1)
    assert [entry.event for entry in entries] == [published]
    assert await _pending(rig) == 1
    await text_redis.aclose()


async def test_entries_deleted_while_pending_are_acked_away(rig: PublisherRig) -> None:
    pod_a, pod_b = _consumer(rig, "pod-a"), _consumer(rig, "pod-b")
    await pod_a.ensure_group()
    await rig.publisher.publish(event(EventType.CALL_QUEUED))
    (entry,) = await pod_a.read(block_ms=1)
    await rig.redis.xdel(pod_a.key, entry.entry_id)
    assert await pod_b.claim_stale(min_idle_ms=0) == []
    assert await _pending(rig) == 0


async def test_an_unrelated_group_error_propagates(rig: PublisherRig) -> None:
    await rig.redis.set("events:sms", "not a stream")
    with pytest.raises(Exception, match="WRONGTYPE"):
        await _consumer(rig, "pod-a", "sms").ensure_group()
