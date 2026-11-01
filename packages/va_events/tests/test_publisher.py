"""``EventPublisher`` (specs/19-events-and-webhooks.md §1 steps 1-5) on fakeredis."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from event_helpers import ORG, PublisherRig, event, publisher_rig, stream_events, stream_payloads

from va_events import STREAMS, EventPublisher, EventPublishError, EventType, decode, event_uuid, stream_for
from va_events import publisher as publisher_module
from va_events.keys import CALL_SEQ_TTL_S, call_live_channel, call_seq_key, stream_key


@pytest.fixture
def rig() -> PublisherRig:
    return publisher_rig()


async def _stream_types(rig: PublisherRig) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for stream in STREAMS:
        found[stream] = [e.type.value for e in await stream_events(rig.redis, stream_key(stream))]
    return found


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.CALL_QUEUED,
        EventType.BATCH_COMPLETED,
        EventType.CITATIONS_READY,
        EventType.EVALS_COMPLETED,
        EventType.SMS_DELIVERED,
        EventType.CONVERSATION_MESSAGE,
        EventType.WIDGET_MESSAGE,
        EventType.BILLING_PAYMENT_FAILED,
        EventType.NUMBER_RELEASED,
        EventType.KB_READY,
    ],
)
async def test_each_event_goes_to_its_stream_only(rig: PublisherRig, event_type: EventType) -> None:
    await rig.publisher.publish(event(event_type))
    found = await _stream_types(rig)
    expected = stream_for(event_type)
    assert found[expected] == [event_type.value]
    assert all(not types for stream, types in found.items() if stream != expected)


async def test_seq_increases_per_call_and_refreshes_the_ttl(rig: PublisherRig) -> None:
    call_a, call_b = uuid.uuid4(), uuid.uuid4()
    seqs_a = [(await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_a))).seq for _ in range(3)]
    seq_b = (await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_b))).seq
    assert seqs_a == [1, 2, 3]
    assert seq_b == 1
    key = call_seq_key(call_a)
    assert key == f"call:{{{call_a}}}:seq"
    await rig.redis.expire(key, 5)
    await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_a))
    assert await rig.redis.ttl(key) == CALL_SEQ_TTL_S
    assert await rig.redis.get(key) == b"4"


async def test_seq_is_gap_free_under_concurrent_publishers(rig: PublisherRig) -> None:
    call_id = uuid.uuid4()
    published = await asyncio.gather(
        *(rig.publisher.publish(event(EventType.CALL_LATENCY, call_id=call_id)) for _ in range(200))
    )
    assert sorted(e.seq for e in published if e.seq is not None) == list(range(1, 201))
    entries = await stream_events(rig.redis, stream_key("calls"))
    assert sorted(e.seq or 0 for e in entries) == list(range(1, 201))
    assert sorted(r.seq for r in rig.writer.rows) == list(range(1, 201))


async def test_non_call_events_have_no_seq_live_message_or_row(rig: PublisherRig) -> None:
    published = await rig.publisher.publish(event(EventType.BILLING_RECHARGED))
    assert published.seq is None
    assert [key async for key in rig.redis.scan_iter("call:*")] == []
    assert rig.writer.rows == []
    assert len(rig.router.routed) == 1


async def test_call_events_are_published_live_with_the_stream_payload(rig: PublisherRig) -> None:
    call_id = uuid.uuid4()
    pubsub = rig.redis.pubsub()
    try:
        await pubsub.subscribe(call_live_channel(call_id))
        assert (await pubsub.get_message(timeout=1.0))["type"] == "subscribe"
        published = await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_id, message="hi"))
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert message is not None
    assert message["channel"] == f"call:{{{call_id}}}:live".encode()
    assert decode(message["data"]) == published
    assert await stream_payloads(rig.redis, stream_key("calls")) == [message["data"]]


async def test_streams_are_trimmed_approximately(rig: PublisherRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher_module, "STREAM_MAXLEN", 10)
    for _ in range(60):
        await rig.publisher.publish(event(EventType.KB_READY))
    assert 10 <= await rig.redis.xlen(stream_key("system")) < 60


async def test_the_call_events_row_mirrors_the_event(rig: PublisherRig) -> None:
    call_id = uuid.uuid4()
    published = await rig.publisher.publish(
        event(EventType.CALL_TOOL, call_id=call_id, level="warning", message="tool", data={"tool_name": "x"})
    )
    (row,) = rig.writer.rows
    assert row.id == event_uuid(published)
    assert (row.org_id, row.call_id, row.seq) == (ORG, call_id, 1)
    assert (row.category, row.level, row.message, row.data) == ("tool", "warning", "tool", {"tool_name": "x"})
    assert row.created_at == published.occurred_at


async def test_an_explicit_category_wins(rig: PublisherRig) -> None:
    await rig.publisher.publish(event(EventType.CALL_ERROR, call_id=uuid.uuid4(), category="memory"))
    assert [r.category for r in rig.writer.rows] == ["memory"]


async def test_call_events_without_any_category_are_not_persisted(rig: PublisherRig) -> None:
    published = await rig.publisher.publish(event(EventType.BATCH_IN_PROGRESS, call_id=uuid.uuid4()))
    assert published.seq == 1
    assert rig.writer.rows == []


async def test_persist_flag_off_writes_no_row(rig: PublisherRig) -> None:
    await rig.publisher.publish(event(EventType.CALL_TURN, call_id=uuid.uuid4()), persist_call_event=False)
    assert rig.writer.rows == []
    assert await rig.redis.xlen(stream_key("calls")) == 1


async def test_the_router_gets_the_sequenced_event_and_the_session(rig: PublisherRig) -> None:
    session = object()
    published = await rig.publisher.publish(
        event(EventType.CALL_ENDED, call_id=uuid.uuid4()),
        session=session,  # type: ignore[arg-type]
    )
    assert rig.router.routed == [(published, session)]
    await rig.publisher.publish(event(EventType.CALL_ENDED, call_id=uuid.uuid4()), deliver_webhooks=False)
    assert len(rig.router.routed) == 1


async def test_without_a_router_or_sink_publishing_still_works() -> None:
    rig = publisher_rig(router=False)
    published = await EventPublisher(rig.redis, call_events=None).publish(
        event(EventType.CALL_TURN, call_id=uuid.uuid4())
    )
    assert published.seq == 1
    assert await rig.redis.xlen(stream_key("calls")) == 1


@pytest.mark.parametrize("call_id", [uuid.uuid4(), None])
async def test_a_redis_outage_raises_and_persists_nothing(
    rig: PublisherRig, call_id: uuid.UUID | None
) -> None:
    rig.server.connected = False
    with pytest.raises(EventPublishError) as caught:
        await rig.publisher.publish(event(EventType.CALL_TURN, call_id=call_id))
    assert caught.value.step == ("seq" if call_id else "stream")
    assert caught.value.__cause__ is not None
    assert rig.writer.rows == []
    assert rig.router.routed == []


async def test_sequence_assigns_the_seq_without_broadcasting_or_persisting(rig: PublisherRig) -> None:
    call_id = uuid.uuid4()
    first = await rig.publisher.sequence(event(EventType.CALL_QUEUED, call_id=call_id))
    second = await rig.publisher.sequence(event(EventType.CALL_TURN, call_id=call_id))
    assert (first.seq, second.seq) == (1, 2)
    assert await rig.redis.xlen(stream_key("calls")) == 0
    assert rig.writer.rows == []
    assert rig.router.routed == []


async def test_sequence_leaves_non_call_events_unchanged(rig: PublisherRig) -> None:
    original = event(EventType.KB_READY)
    assert await rig.publisher.sequence(original) is original
    assert [key async for key in rig.redis.scan_iter("call:*")] == []


async def test_broadcast_sends_the_exact_payload_to_the_stream_and_live_channel(rig: PublisherRig) -> None:
    call_id = uuid.uuid4()
    sequenced = await rig.publisher.sequence(event(EventType.CALL_QUEUED, call_id=call_id))
    pubsub = rig.redis.pubsub()
    try:
        await pubsub.subscribe(call_live_channel(call_id))
        assert (await pubsub.get_message(timeout=1.0))["type"] == "subscribe"
        await rig.publisher.broadcast(sequenced)
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert message is not None
    assert decode(message["data"]) == sequenced
    assert await stream_payloads(rig.redis, stream_key("calls")) == [message["data"]]
    assert rig.writer.rows == []
    assert rig.router.routed == []


async def test_broadcast_failure_raises_at_the_stream_step(rig: PublisherRig) -> None:
    sequenced = await rig.publisher.sequence(event(EventType.CALL_QUEUED, call_id=uuid.uuid4()))
    rig.server.connected = False
    with pytest.raises(EventPublishError) as caught:
        await rig.publisher.broadcast(sequenced)
    assert caught.value.step == "stream"
