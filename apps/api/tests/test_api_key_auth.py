"""API key authentication (specs/04-auth-tenancy-rbac.md §2.2, §14) with fake Redis and an in-memory store."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import fakeredis
import pytest
from auth_helpers import KeyStore
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError

from va_api.auth.api_key_auth import (
    CACHE_TTL_S,
    INVALID_API_KEY,
    ApiKeyAuthenticator,
    cache_key,
    extract_api_key,
    invalidate_api_key_cache,
    touched_key,
)
from va_api.auth.context import RequestContext
from va_common.auth import ApiKeyHasher, Pepper, api_key_prefix
from va_common.errors import AppError
from va_common.time import FrozenClock
from va_db.repositories import ApiKeyRecord


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def store() -> KeyStore:
    return KeyStore()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def authenticator(store: KeyStore, redis: Redis, clock: FrozenClock) -> ApiKeyAuthenticator:
    return store.authenticator(redis, clock)


async def _auth(
    authenticator: ApiKeyAuthenticator, header: str | None, method: str = "GET"
) -> tuple[RequestContext, ApiKeyRecord]:
    return await authenticator.authenticate(
        header, method=method, request_id="req_1", ip="10.0.0.1", user_agent="ua"
    )


async def _assert_rejected(
    authenticator: ApiKeyAuthenticator, header: str | None, method: str = "GET"
) -> None:
    with pytest.raises(AppError) as caught:
        await _auth(authenticator, header, method)
    assert (caught.value.code, caught.value.message, caught.value.http_status) == (
        "AUTH_FAILURE",
        INVALID_API_KEY,
        401,
    )


class _HashCounter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = hashlib.sha256

        def counting(data: bytes) -> Any:
            self.calls += 1
            return real(data)

        monkeypatch.setattr(hashlib, "sha256", counting)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer k", "k"),
        ("bearer k", "k"),
        ("BEARER   k", "k"),
        ("k", "k"),
        ("  k  ", "k"),
        ("", None),
        ("   ", None),
        (None, None),
        ("Bearer", "Bearer"),
        ("Basic xyz", "Basic xyz"),
    ],
)
def test_extract_api_key(header: str | None, expected: str | None) -> None:
    assert extract_api_key(header) == expected


def test_cache_keys_use_hash_tags() -> None:
    key_id = uuid.UUID(int=7)
    assert cache_key("va_live_3kQ9") == "apikey:{va_live_3kQ9}"
    assert touched_key(key_id) == f"apikey:{{{key_id}}}:touched"


async def test_bearer_and_bare_headers_authenticate(
    store: KeyStore, authenticator: ApiKeyAuthenticator
) -> None:
    plaintext, record = store.issue(environment="test", scopes=("calls:read",))
    bearer, _ = await _auth(authenticator, f"Bearer {plaintext}")
    bare, found = await _auth(authenticator, plaintext)
    assert found == record
    assert bearer == bare
    assert bare == RequestContext(
        request_id="req_1",
        org_id=record.org_id,
        principal_type="api_key",
        principal_id=record.id,
        role=None,
        scopes=frozenset({"calls:read"}),
        environment="test",
        plan_id="build",
        ip="10.0.0.1",
        user_agent="ua",
        acting_parent_org_id=None,
    )


@pytest.mark.parametrize("header", [None, "", "va_prod_" + "a" * 32, "va_live_short", "sk_live_" + "a" * 32])
async def test_wrong_prefix_or_length_fails_with_one_dummy_hash(
    store: KeyStore, authenticator: ApiKeyAuthenticator, monkeypatch: pytest.MonkeyPatch, header: str | None
) -> None:
    counter = _HashCounter(monkeypatch)
    await _assert_rejected(authenticator, header)
    assert counter.calls == 1
    assert store.lookups == []


async def test_unknown_prefix_hashes_once(
    store: KeyStore, authenticator: ApiKeyAuthenticator, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = KeyStore(hasher=store.hasher).issue()[0]
    counter = _HashCounter(monkeypatch)
    await _assert_rejected(authenticator, unknown)
    assert counter.calls == 1
    assert store.lookups == [api_key_prefix(unknown)]


async def test_wrong_secret_same_prefix_fails(
    store: KeyStore, authenticator: ApiKeyAuthenticator, monkeypatch: pytest.MonkeyPatch
) -> None:
    plaintext, _ = store.issue()
    forged = plaintext[:-1] + ("A" if plaintext[-1] != "A" else "B")
    counter = _HashCounter(monkeypatch)
    await _assert_rejected(authenticator, forged)
    assert counter.calls == 1


async def test_revoked_key_fails(store: KeyStore, authenticator: ApiKeyAuthenticator) -> None:
    plaintext, _ = store.issue(revoked=True)
    await _assert_rejected(authenticator, plaintext)


async def test_expired_key_fails(
    store: KeyStore, authenticator: ApiKeyAuthenticator, clock: FrozenClock
) -> None:
    plaintext, _ = store.issue(expires_at=clock.now() + timedelta(seconds=1))
    ctx, _ = await _auth(authenticator, plaintext)
    assert ctx.principal_type == "api_key"
    clock.advance(1)
    await _assert_rejected(authenticator, plaintext)
    clock.advance(3600)
    await _assert_rejected(authenticator, plaintext)


async def test_closed_org_fails(store: KeyStore, authenticator: ApiKeyAuthenticator) -> None:
    plaintext, _ = store.issue(org_status="closed")
    await _assert_rejected(authenticator, plaintext)


async def test_suspended_org_allows_reads_blocks_writes(
    store: KeyStore, authenticator: ApiKeyAuthenticator
) -> None:
    plaintext, record = store.issue(org_status="suspended")
    for method in ("GET", "HEAD", "OPTIONS", "get"):
        ctx, _ = await _auth(authenticator, plaintext, method)
        assert ctx.org_id == record.org_id
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(AppError) as caught:
            await _auth(authenticator, plaintext, method)
        assert (caught.value.code, caught.value.http_status) == ("ORG_SUSPENDED", 403)


async def test_previous_pepper_keys_still_verify(redis: Redis, clock: FrozenClock) -> None:
    old_pepper, new_pepper = Pepper(secrets.token_bytes(32), 1), Pepper(secrets.token_bytes(32), 2)
    old = KeyStore(hasher=ApiKeyHasher(old_pepper))
    plaintext, record = old.issue()
    rotated = KeyStore(hasher=ApiKeyHasher(new_pepper, old_pepper), records=old.records)
    ctx, _ = await _auth(rotated.authenticator(redis, clock), plaintext)
    assert ctx.principal_id == record.id
    assert rotated.issue()[1].pepper_version == 2


async def test_cache_hit_skips_lookup(
    store: KeyStore, authenticator: ApiKeyAuthenticator, redis: Redis
) -> None:
    plaintext, record = store.issue()
    await _auth(authenticator, plaintext)
    await _auth(authenticator, plaintext)
    assert store.lookups == [api_key_prefix(plaintext)]
    key = cache_key(api_key_prefix(plaintext))
    assert 1 <= await redis.ttl(key) <= CACHE_TTL_S
    raw = await redis.get(key)
    assert raw is not None
    assert plaintext.encode() not in raw
    assert record.secret_hash.encode() in raw


async def test_cached_record_is_still_checked(store: KeyStore, authenticator: ApiKeyAuthenticator) -> None:
    plaintext, _ = store.issue()
    await _auth(authenticator, plaintext)
    forged = plaintext[:-1] + ("A" if plaintext[-1] != "A" else "B")
    await _assert_rejected(authenticator, forged)
    assert len(store.lookups) == 1


async def test_unknown_prefix_not_cached(
    store: KeyStore, authenticator: ApiKeyAuthenticator, redis: Redis
) -> None:
    unknown = KeyStore(hasher=store.hasher).issue()[0]
    await _assert_rejected(authenticator, unknown)
    await _assert_rejected(authenticator, unknown)
    assert len(store.lookups) == 2
    assert await redis.exists(cache_key(api_key_prefix(unknown))) == 0


async def test_invalidate_api_key_cache_forces_reload(
    store: KeyStore, authenticator: ApiKeyAuthenticator, redis: Redis
) -> None:
    plaintext, record = store.issue()
    prefix = api_key_prefix(plaintext)
    await _auth(authenticator, plaintext)
    store.records[prefix] = replace(record, revoked=True)
    await _auth(authenticator, plaintext)  # still served from the cache
    await invalidate_api_key_cache(redis, prefix)
    assert await redis.exists(cache_key(prefix)) == 0
    await _assert_rejected(authenticator, plaintext)
    assert store.lookups == [prefix, prefix]


async def test_malformed_cache_entry_is_reloaded(
    store: KeyStore, authenticator: ApiKeyAuthenticator, redis: Redis
) -> None:
    plaintext, record = store.issue()
    await redis.set(cache_key(api_key_prefix(plaintext)), b'{"id": "nope"}')
    _, found = await _auth(authenticator, plaintext)
    assert found == record
    assert len(store.lookups) == 1


class _DownRedis(fakeredis.FakeAsyncRedis):
    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    async def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("down")


async def test_redis_down_falls_back_to_lookup(store: KeyStore, clock: FrozenClock) -> None:
    redis = _DownRedis()
    stream = json_log_stream()
    authenticator = store.authenticator(redis, clock)
    plaintext, record = store.issue()
    for _ in range(2):
        ctx, _ = await _auth(authenticator, plaintext)
        assert ctx.principal_id == record.id
    assert len(store.lookups) == 2
    await authenticator.touch_if_due(record.id)
    assert store.touches == []
    events = [event["event"] for event in log_events(stream)]
    assert events.count("api_key_cache_unavailable") == 4
    assert "api_key_touch_failed" in events
    assert plaintext not in stream.getvalue()
    await redis.aclose()


async def test_touch_at_most_once_per_minute(
    store: KeyStore, authenticator: ApiKeyAuthenticator, redis: Redis, clock: FrozenClock
) -> None:
    _, record = store.issue()
    for _ in range(3):
        await authenticator.touch_if_due(record.id)
    assert store.touches == [(record.id, clock.now())]
    assert 1 <= await redis.ttl(touched_key(record.id)) <= 60
    clock.advance(61)
    await redis.delete(touched_key(record.id))  # the 60 s gate expiring
    await authenticator.touch_if_due(record.id)
    assert [touched_at for _, touched_at in store.touches] == [
        clock.now() - timedelta(seconds=61),
        clock.now(),
    ]


async def test_touch_failure_is_swallowed_and_logged(
    store: KeyStore, redis: Redis, clock: FrozenClock
) -> None:
    async def failing_touch(*_args: Any) -> None:
        msg = "UPDATE api_keys"
        raise OperationalError(msg, {}, Exception("connection lost"))

    stream = json_log_stream()
    plaintext, record = store.issue()
    authenticator = ApiKeyAuthenticator(
        redis=redis, hasher=store.hasher, lookup=store.lookup, toucher=failing_touch, clock=clock
    )
    await authenticator.touch_if_due(record.id)
    (event,) = [event for event in log_events(stream) if event["event"] == "api_key_touch_failed"]
    assert event["api_key_id"] == str(record.id)
    assert event["error"] == "OperationalError"
    assert plaintext not in stream.getvalue()
