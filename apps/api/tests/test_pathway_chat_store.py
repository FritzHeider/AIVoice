"""Pathway test chat pieces without the app: snapshot pruning and the Redis store's answers (specs/12 §7
Decision P07-T10)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from va_api.services.pathway_chat import (
    BUSY_MESSAGE,
    CHAT_TTL_S,
    MAX_SNAPSHOTS,
    ChatSession,
    ChatStore,
    chat_key,
    prune_snapshots,
)
from va_common.errors import AppError

ORG = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b01")


def test_snapshots_keep_the_newest_by_turn_number() -> None:
    snapshots = {str(turn): {"turn_index": turn} for turn in range(MAX_SNAPSHOTS + 7)}
    kept = prune_snapshots(snapshots)
    assert list(kept) == [str(turn) for turn in range(7, MAX_SNAPSHOTS + 7)]
    assert list(prune_snapshots({"10": {}, "9": {}, "2": {}}, limit=2)) == ["9", "10"]


def _session(org_id: uuid.UUID = ORG) -> ChatSession:
    return ChatSession(
        org_id=org_id,
        pathway_id=uuid.uuid4(),
        version=1,
        graph={"nodes": [], "edges": [], "global_config": {}},
        request_data={},
        simulate_from="+14155550123",
        simulate_to="+14155550100",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        state={},
        snapshots={},
    )


async def test_store_round_trip_ttl_org_check_and_lock() -> None:
    redis = FakeAsyncRedis()
    store = ChatStore(redis)
    chat_id = uuid.uuid4()
    await store.save(chat_id, _session())
    assert await redis.ttl(chat_key(chat_id)) == CHAT_TTL_S
    assert (await store.load(ORG, chat_id)).simulate_to == "+14155550100"
    with pytest.raises(AppError) as foreign:
        await store.load(uuid.uuid4(), chat_id)
    assert foreign.value.code == "NOT_FOUND"
    async with store.lock(chat_id):
        with pytest.raises(AppError) as busy:
            async with store.lock(chat_id):
                pytest.fail("a second holder must be refused")
        assert (busy.value.code, busy.value.message) == ("CONFLICT", BUSY_MESSAGE)
    async with store.lock(chat_id):
        assert await redis.exists(f"pathway_chat:{chat_id}:lock") == 1
    assert await redis.exists(f"pathway_chat:{chat_id}:lock") == 0
    await redis.aclose()


async def test_store_answers_service_unavailable_when_redis_is_down() -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    store = ChatStore(redis)
    chat_id = uuid.uuid4()
    server.connected = False
    for attempt in (store.load(ORG, chat_id), store.save(chat_id, _session())):
        with pytest.raises(AppError) as down:
            await attempt
        assert down.value.code == "SERVICE_UNAVAILABLE"
    with pytest.raises(AppError) as locked:
        async with store.lock(chat_id):
            pytest.fail("no lock without Redis")
    assert locked.value.code == "SERVICE_UNAVAILABLE"
    await redis.aclose()
