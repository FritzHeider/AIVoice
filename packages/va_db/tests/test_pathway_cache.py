"""The resolved pathway graph cache keys and their invalidation (specs/03 §11; specs/12 §4 Decision
P07-T09b)."""

from __future__ import annotations

import uuid

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from va_db.pathway_cache import PATHWAY_CACHE_ENVIRONMENTS, invalidate_pathway_cache, pathway_cache_key


def test_keys_follow_the_spec_pattern() -> None:
    pathway_id = uuid.UUID("0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70")
    assert pathway_cache_key(pathway_id, "production") == f"pathway:{pathway_id}:v:production"
    assert pathway_cache_key(pathway_id, "staging") == f"pathway:{pathway_id}:v:staging"
    assert PATHWAY_CACHE_ENVIRONMENTS == ("production", "staging")


async def test_invalidate_deletes_only_the_given_environments() -> None:
    redis = fakeredis.FakeAsyncRedis()
    pathway_id, other = uuid.uuid4(), uuid.uuid4()
    keys = [pathway_cache_key(pathway_id, env) for env in PATHWAY_CACHE_ENVIRONMENTS]
    await redis.mset({keys[0]: "{}", keys[1]: "{}", pathway_cache_key(other, "staging"): "{}"})
    assert await invalidate_pathway_cache(redis, pathway_id, ["staging"]) == []
    assert (await redis.exists(keys[0]), await redis.exists(keys[1])) == (1, 0)
    assert await redis.exists(pathway_cache_key(other, "staging")) == 1
    await redis.aclose()


async def test_redis_failures_are_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = fakeredis.FakeAsyncRedis()

    async def broken(*_keys: str) -> int:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "delete", broken)
    pathway_id = uuid.uuid4()
    assert await invalidate_pathway_cache(redis, pathway_id, PATHWAY_CACHE_ENVIRONMENTS) == [
        "production",
        "staging",
    ]
    await redis.aclose()
