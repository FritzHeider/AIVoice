"""Agent onboarding state, scope rules, and limits without a database (specs/04-auth-tenancy-rbac.md §10)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from structlog.testing import capture_logs

from va_api.auth.agent_onboarding import (
    APPROVAL_WINDOW,
    KEY_TTL,
    REQUEST_TTL_S,
    STORE_UNAVAILABLE_MESSAGE,
    AgentRequest,
    AgentRequestStore,
    OnboardingLimiter,
    agent_email_key,
    agent_ip_key,
    agent_key_key,
    agent_request_key,
    agent_token_key,
    approval_link,
    check_requested_scopes,
    decode_key_blob,
    email_hash,
    encode_key_blob,
    onboarding_limited_message,
)
from va_api.auth.window_counter import COUNTER_UNAVAILABLE_MESSAGE, hit_window
from va_common.auth import issue_token, sha256_hex
from va_common.errors import AppError
from va_common.time import FrozenClock

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
EMAIL = "Jane@Acme-Dental.test"


def offline_redis() -> fakeredis.FakeAsyncRedis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeAsyncRedis(server=server)


def sample(**changes: object) -> AgentRequest:
    request = AgentRequest(
        request_id=uuid.UUID("0192f5c3-1b2d-7e4f-8a90-b1c2d3e4f5a6"),
        email_hash=email_hash(EMAIL),
        agent_name="Claude Code",
        purpose="Build the reminder pathway.",
        scopes=("calls:read", "calls:write"),
        poll_secret_hash=sha256_hex("poll"),
        token_hash=sha256_hex("link"),
        status="pending_verification",
        created_at=NOW,
        expires_at=NOW + APPROVAL_WINDOW,
    )
    return replace(request, **changes)  # type: ignore[arg-type]


async def created(store: AgentRequestStore, token_hash: str = "") -> AgentRequest:
    return await store.create(
        email=EMAIL,
        agent_name="Claude Code",
        purpose="",
        scopes=["calls:read"],
        poll_secret_hash=sha256_hex("poll"),
        token_hash=token_hash or sha256_hex("link"),
        request_id=uuid.uuid4(),
    )


def test_key_names_match_specs_03() -> None:
    request_id = uuid.UUID("0192f5c3-1b2d-7e4f-8a90-b1c2d3e4f5a6")
    assert agent_request_key(request_id) == f"agentreq:{request_id}"
    assert agent_token_key("abc") == "agentreq:token:abc"
    assert agent_key_key(request_id) == f"agentkey:{request_id}"
    assert agent_ip_key("203.0.113.9") == "agentonb:ip:203.0.113.9"
    assert agent_ip_key(None) == "agentonb:ip:unknown"
    assert (
        agent_email_key(" JANE@acme-dental.test ") == f"agentonb:email:{sha256_hex('jane@acme-dental.test')}"
    )


def test_approval_link() -> None:
    assert (
        approval_link("https://app.voiceagents.test/", "tok")
        == "https://app.voiceagents.test/agent-onboarding/tok"
    )


def test_key_blob_round_trip() -> None:
    assert decode_key_blob(encode_key_blob(b"\x00\xffsecret")) == b"\x00\xffsecret"


def test_requested_scopes_deduplicated_and_sorted() -> None:
    assert check_requested_scopes(["pathways:write", "calls:read", "calls:read"]) == [
        "calls:read",
        "pathways:write",
    ]


@pytest.mark.parametrize("scope", ["*", "billing:read", "billing:write", "api_keys:write", "calls:delete"])
def test_requested_scopes_reject_excluded_and_unknown(scope: str) -> None:
    with pytest.raises(AppError) as caught:
        check_requested_scopes(["calls:read", scope])
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "requested_scopes")
    assert f"'{scope}'" in caught.value.message


def test_request_json_round_trip() -> None:
    approved = sample(
        status="approved",
        approver_user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        key_id=uuid.uuid4(),
        key_expires_at=NOW + KEY_TTL,
    )
    assert AgentRequest.from_json(approved.to_json()) == approved
    assert AgentRequest.from_json(sample().to_json()) == sample()
    assert "poll_secret_hash" not in repr(approved)
    assert sha256_hex("poll") not in repr(approved)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        sample().to_json().replace("pending_verification", "lost"),
        sample().to_json().replace('"agent_name":"Claude Code"', '"agent_name":7'),
        sample().to_json().replace('"scopes":["calls:read","calls:write"]', '"scopes":"calls:read"'),
    ],
)
def test_request_from_json_rejects_malformed(raw: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        AgentRequest.from_json(raw)


@pytest.mark.parametrize(
    ("changes", "at", "status"),
    [
        ({}, NOW, "pending_verification"),
        ({}, NOW + APPROVAL_WINDOW, "expired"),
        ({"status": "approving"}, NOW + APPROVAL_WINDOW, "pending_verification"),
        ({"status": "approved", "key_expires_at": NOW + KEY_TTL}, NOW, "approved"),
        ({"status": "approved", "key_expires_at": NOW + KEY_TTL}, NOW + KEY_TTL, "expired"),
        ({"status": "approved"}, NOW, "expired"),
        ({"status": "delivered"}, NOW + timedelta(days=1), "delivered"),
        ({"status": "denied"}, NOW, "denied"),
    ],
)
def test_effective_status(changes: dict[str, object], at: datetime, status: str) -> None:
    assert sample(**changes).effective_status(at) == status


def test_approvable_only_while_pending_before_deadline() -> None:
    assert sample().approvable(NOW)
    assert not sample().approvable(NOW + APPROVAL_WINDOW)
    assert not sample(status="denied").approvable(NOW)


async def test_store_create_get_and_by_token() -> None:
    redis = fakeredis.FakeAsyncRedis()
    store = AgentRequestStore(redis, FrozenClock(NOW))
    token = issue_token()
    stored = await created(store, token.token_hash)
    assert stored.expires_at == NOW + APPROVAL_WINDOW
    assert stored.email_hash == sha256_hex("jane@acme-dental.test")
    assert await store.get(stored.request_id) == stored
    assert await store.by_token(token.plaintext) == stored
    assert await store.by_token(issue_token().plaintext) is None
    assert 0 < await redis.ttl(agent_request_key(stored.request_id)) <= REQUEST_TTL_S
    assert 0 < await redis.ttl(agent_token_key(token.token_hash)) <= APPROVAL_WINDOW.total_seconds()
    await store.drop_token(token.token_hash)
    assert await store.by_token(token.plaintext) is None


async def test_store_ignores_malformed_values() -> None:
    redis = fakeredis.FakeAsyncRedis()
    store = AgentRequestStore(redis, FrozenClock(NOW))
    request_id = uuid.uuid4()
    await redis.set(agent_request_key(request_id), "{not json")
    await redis.set(agent_token_key(sha256_hex("bad-id")), "not-a-uuid")
    assert await store.get(request_id) is None
    assert await store.by_token("bad-id") is None


async def test_transition_is_compare_and_set_and_keeps_ttl() -> None:
    redis = fakeredis.FakeAsyncRedis()
    store = AgentRequestStore(redis, FrozenClock(NOW))
    stored = await created(store)
    ttl = await redis.ttl(agent_request_key(stored.request_id))
    results = await asyncio.gather(
        store.transition(stored.request_id, "pending_verification", lambda r: replace(r, status="approving")),
        store.transition(stored.request_id, "pending_verification", lambda r: replace(r, status="denied")),
    )
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    current = await store.get(stored.request_id)
    assert current is not None
    assert current.status == winners[0].status
    assert 0 < await redis.ttl(agent_request_key(stored.request_id)) <= ttl
    assert await store.transition(uuid.uuid4(), "pending_verification", lambda r: r) is None


async def test_take_key_once() -> None:
    redis = fakeredis.FakeAsyncRedis()
    store = AgentRequestStore(redis, FrozenClock(NOW))
    stored = await created(store)
    assert await store.take_key(stored.request_id) is None
    await store.transition(stored.request_id, "pending_verification", lambda r: replace(r, status="approved"))
    assert await store.take_key(stored.request_id) is None
    await store.put_key(stored.request_id, "YmxvYg==")
    assert 0 < await redis.ttl(agent_key_key(stored.request_id)) <= KEY_TTL.total_seconds()
    results = await asyncio.gather(*(store.take_key(stored.request_id) for _ in range(3)))
    taken = [result for result in results if result is not None]
    assert len(taken) == 1
    delivered, blob = taken[0]
    assert (delivered.status, blob) == ("delivered", "YmxvYg==")
    assert await redis.exists(agent_key_key(stored.request_id)) == 0
    current = await store.get(stored.request_id)
    assert current is not None
    assert current.status == "delivered"


async def test_store_unavailable() -> None:
    store = AgentRequestStore(offline_redis(), FrozenClock(NOW))
    request_id = uuid.uuid4()
    calls = [
        created(store),
        store.get(request_id),
        store.by_token("tok"),
        store.transition(request_id, "pending_verification", lambda r: r),
        store.put_key(request_id, "blob"),
        store.take_key(request_id),
        store.drop_token("hash"),
    ]
    for call in calls:
        with pytest.raises(AppError) as caught:
            await call
        assert (caught.value.code, caught.value.message) == ("SERVICE_UNAVAILABLE", STORE_UNAVAILABLE_MESSAGE)


async def test_limiter_counts_ip_then_email() -> None:
    redis = fakeredis.FakeAsyncRedis()
    limiter = OnboardingLimiter(redis)
    for index in range(5):
        await limiter.hit("203.0.113.9", f"user{index}@acme-dental.test")
    with pytest.raises(AppError) as caught:
        await limiter.hit("203.0.113.9", "other@acme-dental.test")
    assert caught.value.code == "RATE_LIMITED"
    retry_after = int(caught.value.headers["Retry-After"])
    assert 0 < retry_after <= 3600
    assert caught.value.message == onboarding_limited_message(retry_after)
    assert await redis.get(agent_email_key("other@acme-dental.test")) is None
    for ip in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        await limiter.hit(ip, EMAIL)
    with pytest.raises(AppError) as again:
        await limiter.hit("198.51.100.4", EMAIL.lower())
    assert again.value.code == "RATE_LIMITED"
    assert 0 < await redis.ttl(agent_email_key(EMAIL)) <= 3600


async def test_limiter_fails_closed() -> None:
    with pytest.raises(AppError) as caught:
        await OnboardingLimiter(offline_redis()).hit("203.0.113.9", EMAIL)
    assert (caught.value.code, caught.value.message) == ("SERVICE_UNAVAILABLE", COUNTER_UNAVAILABLE_MESSAGE)


async def test_hit_window_fails_open_with_an_event() -> None:
    with capture_logs() as logs:
        await hit_window(
            offline_redis(), "k", limit=1, window_s=60, message=str, unavailable_event="probe_unavailable"
        )
    assert [entry["event"] for entry in logs] == ["probe_unavailable"]
