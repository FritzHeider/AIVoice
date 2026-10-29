"""Request rate limiting per principal over HTTP (specs/04 §11; specs/31 ``RATE_LIMITED``)."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from auth_helpers import AUTH_ENVELOPE_PREFIX, AUTH_LEGACY_PREFIX, AuthContainer, auth_app
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events, make_client

from va_api.auth import RequestContext
from va_api.auth.dependencies import RATE_LIMIT_BUCKET_ATTR, rate_limit
from va_api.main import CORS_EXPOSE_HEADERS
from va_api.rate_limit import (
    PLAN_RATE_LIMITS,
    PlanRateLimits,
    plan_rate_limits,
    rate_limit_headers,
    rate_limit_key,
    rate_limited_message,
)
from va_common.token_bucket import BucketDecision, BucketLimit

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"
PREFIXES = [AUTH_LEGACY_PREFIX, AUTH_ENVELOPE_PREFIX]
_ROW = re.compile(r"^\| (\w+) \| (\d+) req/s[^|]*\| (\d+) req/s \| (\d+) \|$")
_LIMIT, _REMAINING, _RESET = "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"


def _spec_rate_table() -> dict[str, tuple[int, int, int]]:
    section = SPEC_04.read_text(encoding="utf-8").split("## 11.", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, tuple[int, int, int]] = {}
    for line in section.splitlines():
        match = _ROW.match(line.strip())
        if match:
            plan, default_rate, calls_rate, burst = match.groups()
            rows[plan] = (int(default_rate), int(calls_rate), int(burst))
    return rows


def _context(principal_type: Any) -> RequestContext:
    return RequestContext(
        request_id="req_1",
        org_id=uuid.UUID(int=1),
        principal_type=principal_type,
        principal_id=uuid.UUID(int=2),
        role=None,
        scopes=frozenset({"*"}),
        environment="live",
        plan_id="start",
        ip=None,
        user_agent=None,
        acting_parent_org_id=None,
    )


def _headers(response: httpx.Response) -> tuple[str, str, str]:
    return response.headers[_LIMIT], response.headers[_REMAINING], response.headers[_RESET]


def _start_key(container: AuthContainer, **overrides: Any) -> dict[str, str]:
    plaintext, _ = container.store.issue(plan_id="start", **overrides)
    return {"Authorization": f"Bearer {plaintext}"}


async def _requests(
    container: AuthContainer, calls: list[tuple[str, str, dict[str, str]]]
) -> list[httpx.Response]:
    async with make_client(auth_app(container)) as client:
        return [await client.request(method, path, headers=headers) for method, path, headers in calls]


def test_plan_rate_limits_match_spec_table() -> None:
    table = _spec_rate_table()
    assert set(table) == set(PLAN_RATE_LIMITS) == {"start", "build", "scale", "enterprise"}
    for plan, (default_rate, calls_rate, burst) in table.items():
        limits = PLAN_RATE_LIMITS[plan]
        assert limits.default == BucketLimit(rate_per_s=float(default_rate), burst=burst)
        assert limits.calls_send == BucketLimit(rate_per_s=float(calls_rate), burst=2 * calls_rate)


def test_unknown_plan_falls_back_to_start() -> None:
    stream = json_log_stream()
    assert plan_rate_limits("legacy-gold") == PLAN_RATE_LIMITS["start"]
    (event,) = [e for e in log_events(stream) if e["event"] == "rate_limit_unknown_plan"]
    assert event["plan_id"] == "legacy-gold"


def test_rate_per_sec_override_replaces_default_bucket_only() -> None:
    limits = plan_rate_limits("enterprise", 1000)
    assert limits == PlanRateLimits(
        default=BucketLimit(rate_per_s=1000.0, burst=2000),
        calls_send=PLAN_RATE_LIMITS["enterprise"].calls_send,
    )
    assert plan_rate_limits("start", 1).default == BucketLimit(rate_per_s=1.0, burst=2)
    assert limits.for_bucket("calls_send") is limits.calls_send


def test_rate_limit_key_for_api_key_and_user() -> None:
    principal = "00000000-0000-0000-0000-000000000002"
    assert rate_limit_key(_context("api_key"), "default") == f"rl:key:{{{principal}}}:default"
    assert rate_limit_key(_context("user"), "calls_send") == f"rl:user:{{{principal}}}:calls_send"
    assert rate_limit_key(_context("platform_admin"), "default") == f"rl:user:{{{principal}}}:default"


def test_rate_limit_key_rejects_widget() -> None:
    with pytest.raises(ValueError, match="no request rate bucket for principal type 'widget'"):
        rate_limit_key(_context("widget"), "default")


def test_headers_and_message_format() -> None:
    allowed = BucketDecision(allowed=True, limit=20, remaining=3, reset_s=2, retry_after_s=0)
    denied = BucketDecision(allowed=False, limit=4, remaining=0, reset_s=2, retry_after_s=3)
    assert rate_limit_headers(allowed) == ((_LIMIT, "20"), (_REMAINING, "3"), (_RESET, "2"))
    assert rate_limit_headers(denied)[-1] == ("retry-after", "3")
    assert rate_limited_message(1) == "Rate limit exceeded; retry after 1 second."
    assert rate_limited_message(3) == "Rate limit exceeded; retry after 3 seconds."


def test_rate_limit_dependency_carries_bucket() -> None:
    assert getattr(rate_limit("calls_send"), RATE_LIMIT_BUCKET_ATTR) == "calls_send"


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_headers_on_success(prefix: str) -> None:
    container = AuthContainer()
    (response,) = await _requests(container, [("GET", f"{prefix}/read", _start_key(container))])
    assert response.status_code == 200
    assert _headers(response) == ("20", "19", "1")
    assert "retry-after" not in response.headers


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_429_after_burst_with_retry_after(prefix: str) -> None:
    container = AuthContainer()
    headers = _start_key(container)
    responses = await _requests(container, [("GET", f"{prefix}/read", headers)] * 21)
    assert [r.status_code for r in responses[:20]] == [200] * 20
    denied = responses[20]
    assert denied.status_code == 429
    assert _headers(denied) == ("20", "0", "2")
    assert denied.headers["retry-after"] == "1"
    assert denied.headers["x-request-id"] == denied.json()["request_id"]
    item = denied.json()["errors"][0]
    assert item == {"error": "RATE_LIMITED", "message": "Rate limit exceeded; retry after 1 second."}
    if prefix == AUTH_LEGACY_PREFIX:
        assert denied.json()["status"] == "error"
    else:
        assert denied.json()["data"] is None


async def test_refill_after_clock_advance() -> None:
    container = AuthContainer()
    headers = _start_key(container)
    path = f"{AUTH_LEGACY_PREFIX}/read"
    async with make_client(auth_app(container)) as client:
        for _ in range(20):
            assert (await client.get(path, headers=headers)).status_code == 200
        assert (await client.get(path, headers=headers)).status_code == 429
        container.clock.advance(0.1)
        refilled = await client.get(path, headers=headers)
        assert refilled.status_code == 200
        assert refilled.headers[_REMAINING] == "0"
        assert (await client.get(path, headers=headers)).status_code == 429


async def test_headers_on_error_responses() -> None:
    container = AuthContainer()
    plaintext, _ = container.store.issue(plan_id="start", scopes=("calls:read",))
    (response,) = await _requests(
        container, [("POST", f"{AUTH_ENVELOPE_PREFIX}/write", {"Authorization": plaintext})]
    )
    assert response.status_code == 403
    assert response.json()["errors"][0]["error"] == "FORBIDDEN"
    assert _headers(response) == ("20", "19", "1")


async def test_no_headers_when_auth_fails() -> None:
    container = AuthContainer()
    (response,) = await _requests(
        container, [("GET", f"{AUTH_LEGACY_PREFIX}/read", {"Authorization": "nope"})]
    )
    assert response.status_code == 401
    assert _LIMIT not in response.headers
    assert await container.redis.keys("rl:*") == []


async def test_public_routes_are_not_rate_limited() -> None:
    (response,) = await _requests(AuthContainer(), [("GET", "/v1/health", {})])
    assert response.status_code == 200
    assert _LIMIT not in response.headers


async def test_calls_send_bucket() -> None:
    container = AuthContainer()
    headers = _start_key(container)
    send = ("POST", f"{AUTH_LEGACY_PREFIX}/send", headers)
    responses = await _requests(container, [send] * 5 + [("GET", f"{AUTH_LEGACY_PREFIX}/read", headers)])
    assert [r.status_code for r in responses[:5]] == [200, 200, 200, 200, 429]
    assert _headers(responses[0]) == ("4", "3", "1")
    assert _headers(responses[4]) == ("4", "0", "2")
    assert responses[4].headers["retry-after"] == "1"
    # Every request took a `default` token too, the denied one included: 20 - 5 - 1.
    assert _headers(responses[5]) == ("20", "14", "1")


async def test_default_bucket_denial_skips_calls_send() -> None:
    container = AuthContainer()
    headers = _start_key(container)
    reads = [("GET", f"{AUTH_LEGACY_PREFIX}/read", headers)] * 20
    responses = await _requests(container, [*reads, ("POST", f"{AUTH_LEGACY_PREFIX}/send", headers)])
    denied = responses[-1]
    assert denied.status_code == 429
    assert _headers(denied) == ("20", "0", "2")
    assert await container.redis.keys("rl:*:calls_send") == []


async def test_per_key_isolation() -> None:
    container = AuthContainer()
    first, record = container.store.issue(plan_id="start")
    second, _ = container.store.issue(plan_id="start", org_id=record.org_id)
    path = f"{AUTH_LEGACY_PREFIX}/read"
    responses = await _requests(
        container, [("GET", path, {"Authorization": first})] * 21 + [("GET", path, {"Authorization": second})]
    )
    assert responses[20].status_code == 429
    assert responses[21].status_code == 200
    assert responses[21].headers[_REMAINING] == "19"


@pytest.mark.parametrize(("plan_id", "limit"), [("build", "100"), ("scale", "200"), ("enterprise", "600")])
async def test_limit_follows_plan(plan_id: str, limit: str) -> None:
    container = AuthContainer()
    plaintext, _ = container.store.issue(plan_id=plan_id)
    (response,) = await _requests(
        container, [("GET", f"{AUTH_LEGACY_PREFIX}/read", {"Authorization": plaintext})]
    )
    assert response.headers[_LIMIT] == limit


async def test_org_rate_per_sec_override() -> None:
    container = AuthContainer()
    headers = _start_key(container, rate_per_sec=1)
    path = f"{AUTH_LEGACY_PREFIX}/read"
    responses = await _requests(container, [("GET", path, headers)] * 3)
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert _headers(responses[0]) == ("2", "1", "1")
    assert _headers(responses[2]) == ("2", "0", "2")


async def test_redis_down_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = fakeredis.FakeAsyncRedis()

    async def unavailable(*args: object) -> object:
        raise RedisConnectionError

    monkeypatch.setattr(redis, "evalsha", unavailable)
    container = AuthContainer(redis=redis)
    stream = json_log_stream()
    (response,) = await _requests(container, [("POST", f"{AUTH_LEGACY_PREFIX}/send", _start_key(container))])
    assert response.status_code == 200
    assert _LIMIT not in response.headers
    events = [e for e in log_events(stream) if e["event"] == "rate_limit_unavailable"]
    assert [(e["bucket"], e["principal_type"], e["error"]) for e in events] == [
        ("default", "api_key", "ConnectionError"),
        ("calls_send", "api_key", "ConnectionError"),
    ]


async def test_concurrent_requests_never_exceed_bucket() -> None:
    container = AuthContainer()
    headers = _start_key(container)
    async with make_client(auth_app(container)) as client:
        responses = await asyncio.gather(
            *(client.get(f"{AUTH_LEGACY_PREFIX}/read", headers=headers) for _ in range(100))
        )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 20
    assert statuses.count(429) == 80
    assert sorted(int(r.headers[_REMAINING]) for r in responses if r.status_code == 200) == list(range(20))


async def test_cors_exposes_rate_limit_headers() -> None:
    assert {"X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"} <= set(
        CORS_EXPOSE_HEADERS
    )
    container = AuthContainer()
    headers = {**_start_key(container), "Origin": "http://localhost:3000"}
    (response,) = await _requests(container, [("GET", f"{AUTH_LEGACY_PREFIX}/read", headers)])
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {_LIMIT, _REMAINING, _RESET, "retry-after"} <= exposed
