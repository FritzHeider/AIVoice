"""Dashboard session flows on a migrated Postgres (specs/04-auth-tenancy-rbac.md §4.1-§4.4).

Login, the session principal with sliding and absolute expiry, the ``sess:`` cache, CSRF and Origin checks,
logout, the MFA gate, and org status, all through the real app with fake Redis and a frozen clock.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import fakeredis
import httpx
import pytest
from identity_db import IdentityDb, unique_email
from session_helpers import (
    DASHBOARD_URL,
    PASSWORD,
    SESSION_PROBE_PATH,
    Member,
    SessionContainer,
    login,
    make_member,
    session_app,
    session_client,
    write_headers,
)
from sqlalchemy import delete, select, update

from va_api.auth.api_key_auth import INVALID_API_KEY, ORG_SUSPENDED_MESSAGE
from va_api.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from va_api.auth.csrf import CSRF_INVALID, ORIGIN_NOT_ALLOWED
from va_api.auth.login_throttle import email_failure_key, ip_failure_key
from va_api.auth.rbac import ROLE_SCOPES
from va_api.auth.session_auth import MFA_REQUIRED_MESSAGE, NO_MEMBERSHIP_MESSAGE, ORG_CLOSED_MESSAGE
from va_api.auth.sessions import SESSION_INVALID, session_cache_key
from va_api.routers.auth import INVALID_CREDENTIALS_MESSAGE
from va_common import json
from va_common.auth import Argon2Params, PasswordHasher, sha256_hex
from va_db.models import Membership, Org, Session, User
from va_db.repositories import IdentityRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_LOGIN = "/internal/auth/login"
_LOGOUT = "/internal/auth/logout"
_LOGOUT_ALL = "/internal/auth/logout-all"
_SESSION = "/internal/auth/session"
_CLIENT_IP = "127.0.0.1"  # httpx.ASGITransport's peer address


@pytest.fixture
async def member(identity: IdentityDb, container: SessionContainer) -> Member:
    return await make_member(identity, container.password_hasher)


def _error(response: httpx.Response) -> tuple[int, str, str]:
    item = response.json()["errors"][0]
    return response.status_code, item["error"], item["message"]


def _set_cookies(response: httpx.Response) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0]
        cookies[name] = header
    return cookies


async def _sessions_of(identity: IdentityDb, user_id: uuid.UUID) -> list[Session]:
    async with identity.database.global_session() as session:
        rows = await session.scalars(
            select(Session).where(Session.user_id == user_id).order_by(Session.created_at)
        )
        return list(rows)


async def _only_session(identity: IdentityDb, user_id: uuid.UUID) -> Session:
    rows = await _sessions_of(identity, user_id)
    assert len(rows) == 1
    return rows[0]


async def _drop_cache(container: SessionContainer, client: httpx.AsyncClient) -> None:
    await container.redis.delete(session_cache_key(sha256_hex(client.cookies[SESSION_COOKIE])))


async def _set_user(identity: IdentityDb, user_id: uuid.UUID, **values: Any) -> None:
    async with identity.database.global_session() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))


async def test_login_sets_cookies_and_stores_hash(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    response = await login(client, member.user.email)
    body = response.json()
    assert body["errors"] is None
    assert body["data"]["mfa_required"] is False
    user = body["data"]["user"]
    assert (user["id"], user["email"], user["name"]) == (str(member.user.id), member.user.email, "Jane Park")
    assert (user["email_verified"], user["mfa_enabled"], user["is_platform_admin"]) == (False, False, False)

    cookies = _set_cookies(response)
    session_cookie, csrf_cookie = cookies[SESSION_COOKIE].lower(), cookies[CSRF_COOKIE].lower()
    for header in (session_cookie, csrf_cookie):
        for attribute in ("samesite=lax", "path=/", "secure", "max-age=7776000"):
            assert attribute in header
        assert "domain=" not in header
    assert "httponly" in session_cookie
    assert "httponly" not in csrf_cookie

    token = client.cookies[SESSION_COOKIE]
    row = await _only_session(identity, member.user.id)
    now = container.clock.now()
    assert row.token_hash == sha256_hex(token)
    assert (row.created_at, row.expires_at, row.active_org_id) == (
        now,
        now + timedelta(days=14),
        member.org.id,
    )
    async with identity.database.global_session() as session:
        stored = await IdentityRepo(session).get_user(member.user.id)
    assert stored is not None
    assert stored.last_login_at == now


async def test_cookie_domain_and_local_env(
    identity: IdentityDb, container: SessionContainer, member: Member
) -> None:
    app = session_app(container, va_env="local", session_cookie_domain="voiceagents.test")
    async with session_client(app) as http:
        cookies = _set_cookies(await login(http, member.user.email))
    for header in cookies.values():
        assert "domain=voiceagents.test" in header.lower()
        assert "secure" not in header.lower()


async def test_login_invalid_credentials_generic(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    unknown = await client.post(_LOGIN, json={"email": unique_email("ghost"), "password": PASSWORD})
    wrong = await client.post(_LOGIN, json={"email": member.user.email, "password": "not-the-password"})
    expected = (401, "INVALID_CREDENTIALS", INVALID_CREDENTIALS_MESSAGE)
    assert _error(unknown) == _error(wrong) == expected
    assert unknown.json()["errors"] == wrong.json()["errors"]
    assert SESSION_COOKIE not in client.cookies
    assert int(await container.redis.get(email_failure_key(member.user.email)) or 0) == 1
    assert int(await container.redis.get(ip_failure_key(_CLIENT_IP)) or 0) == 2


async def test_login_validates_body(client: httpx.AsyncClient) -> None:
    response = await client.post(_LOGIN, json={"email": "a@b.test", "password": "", "extra": 1})
    assert response.status_code == 400
    assert {item["field"] for item in response.json()["errors"]} == {"password", "extra"}


async def test_login_locks_after_ten_failures(member: Member, client: httpx.AsyncClient) -> None:
    for _ in range(10):
        wrong = await client.post(_LOGIN, json={"email": member.user.email, "password": "nope-nope-nope"})
        assert wrong.status_code == 401
    locked = await client.post(_LOGIN, json={"email": member.user.email, "password": PASSWORD})
    status, code, message = _error(locked)
    retry_after = int(locked.headers["retry-after"])
    assert (status, code) == (429, "AUTH_LOCKED")
    assert 0 < retry_after <= 900
    assert message.startswith("Too many failed login attempts; try again in ")


async def test_login_rehashes_old_params(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    old_hasher = PasswordHasher(
        Argon2Params(time_cost=2, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
    )
    member = await make_member(identity, old_hasher)
    async with identity.database.global_session() as session:
        before = await IdentityRepo(session).get_user(member.user.id)
    assert before is not None
    assert before.password_hash is not None
    assert container.password_hasher.needs_rehash(before.password_hash)
    await login(client, member.user.email)
    async with identity.database.global_session() as session:
        after = await IdentityRepo(session).get_user(member.user.id)
    assert after is not None
    assert after.password_hash is not None
    assert after.password_hash != before.password_hash
    assert not container.password_hasher.needs_rehash(after.password_hash)
    assert container.password_hasher.verify(after.password_hash, PASSWORD)


async def test_login_revokes_presented_session(
    identity: IdentityDb, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    first = client.cookies[SESSION_COOKIE]
    await login(client, member.user.email)
    second = client.cookies[SESSION_COOKIE]
    assert first != second
    rows = {row.token_hash: row for row in await _sessions_of(identity, member.user.id)}
    assert rows[sha256_hex(first)].revoked_at is not None
    assert rows[sha256_hex(second)].revoked_at is None
    replay = await client.get(SESSION_PROBE_PATH, headers={"Cookie": f"{SESSION_COOKIE}={first}"})
    assert _error(replay) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_disabled_user_cannot_login(
    identity: IdentityDb, member: Member, client: httpx.AsyncClient
) -> None:
    await _set_user(identity, member.user.id, status="disabled")
    response = await client.post(_LOGIN, json={"email": member.user.email, "password": PASSWORD})
    assert _error(response) == (401, "INVALID_CREDENTIALS", INVALID_CREDENTIALS_MESSAGE)


async def test_login_rejects_foreign_origin(member: Member, client: httpx.AsyncClient) -> None:
    body = {"email": member.user.email, "password": PASSWORD}
    foreign = await client.post(_LOGIN, json=body, headers={"Origin": "https://evil.test"})
    assert _error(foreign) == (403, "FORBIDDEN", ORIGIN_NOT_ALLOWED)
    referer = await client.post(_LOGIN, json=body, headers={"Referer": "https://evil.test/login"})
    assert _error(referer) == (403, "FORBIDDEN", ORIGIN_NOT_ALLOWED)
    cli = await client.post(_LOGIN, json=body)
    assert cli.status_code == 200


async def test_session_endpoint_returns_memberships_and_csrf(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    response = await client.get(_SESSION)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user"]["id"] == str(member.user.id)
    assert data["memberships"] == [
        {
            "org_id": str(member.org.id),
            "org_name": "Acme Dental",
            "org_slug": member.org.slug,
            "role": "owner",
            "plan_id": "start",
            "org_status": "active",
            "parent_org_id": None,
        }
    ]
    assert data["active_org_id"] == str(member.org.id)
    assert (data["mfa_passed"], data["mfa_required"]) == (False, False)
    assert data["expires_at"] == (container.clock.now() + timedelta(days=14)).isoformat().replace(
        "+00:00", "Z"
    )
    csrf = data["csrf_token"]
    assert csrf == client.cookies[CSRF_COOKIE]
    again = await client.get(_SESSION)
    assert again.json()["data"]["csrf_token"] == csrf

    client.cookies.delete(CSRF_COOKIE)
    fresh = await client.get(_SESSION)
    new_csrf = fresh.json()["data"]["csrf_token"]
    assert new_csrf != csrf
    assert new_csrf == client.cookies[CSRF_COOKIE]


async def test_session_principal_reaches_scoped_route(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    response = await client.get(SESSION_PROBE_PATH)
    assert response.status_code == 200
    assert response.json() == {
        "org_id": str(member.org.id),
        "principal_type": "user",
        "principal_id": str(member.user.id),
        "role": "owner",
        "scopes": sorted(ROLE_SCOPES["owner"]),
        "environment": "live",
        "plan_id": "start",
    }
    assert await container.redis.exists(f"rl:user:{{{member.user.id}}}:default")
    assert int(response.headers["x-ratelimit-limit"]) > 0

    analyst = await make_member(identity, container.password_hasher, role="analyst")
    await login(client, analyst.user.email)
    read = await client.get(SESSION_PROBE_PATH)
    assert read.json()["role"] == "analyst"
    write = await client.post(SESSION_PROBE_PATH, headers=write_headers(client))
    assert _error(write) == (403, "FORBIDDEN", "Missing required scope 'knowledge:write'.")


async def test_csrf_missing_or_wrong_is_403(member: Member, client: httpx.AsyncClient) -> None:
    await login(client, member.user.email)
    missing = await client.post(SESSION_PROBE_PATH, headers={"Origin": DASHBOARD_URL})
    assert _error(missing) == (403, "FORBIDDEN", CSRF_INVALID)
    wrong = await client.post(SESSION_PROBE_PATH, headers={"Origin": DASHBOARD_URL, "X-CSRF-Token": "x" * 43})
    assert _error(wrong) == (403, "FORBIDDEN", CSRF_INVALID)
    ok = await client.post(SESSION_PROBE_PATH, headers=write_headers(client))
    assert ok.status_code == 200


async def test_session_write_requires_dashboard_origin(member: Member, client: httpx.AsyncClient) -> None:
    await login(client, member.user.email)
    foreign = await client.post(SESSION_PROBE_PATH, headers=write_headers(client, origin="https://evil.test"))
    assert _error(foreign) == (403, "FORBIDDEN", ORIGIN_NOT_ALLOWED)
    neither = await client.post(SESSION_PROBE_PATH, headers=write_headers(client, origin=None))
    assert _error(neither) == (403, "FORBIDDEN", ORIGIN_NOT_ALLOWED)
    referer = {**write_headers(client, origin=None), "Referer": f"{DASHBOARD_URL}/agents"}
    assert (await client.post(SESSION_PROBE_PATH, headers=referer)).status_code == 200


async def test_api_key_requests_skip_csrf(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    plaintext, record = container.store.issue()
    keyed = await client.post(SESSION_PROBE_PATH, headers={"Authorization": f"Bearer {plaintext}"})
    assert keyed.status_code == 200
    assert (keyed.json()["principal_type"], keyed.json()["org_id"]) == ("api_key", str(record.org_id))
    bad_key = await client.get(SESSION_PROBE_PATH, headers={"Authorization": "Bearer nope"})
    assert _error(bad_key) == (401, "AUTH_FAILURE", INVALID_API_KEY)
    client.cookies.clear()
    anonymous = await client.get(SESSION_PROBE_PATH)
    assert _error(anonymous) == (401, "AUTH_FAILURE", INVALID_API_KEY)
    garbage = await client.get(SESSION_PROBE_PATH, headers={"Cookie": f"{SESSION_COOKIE}=garbage"})
    assert _error(garbage) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_expired_session_is_401(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    container.clock.advance(timedelta(days=14, seconds=1))
    assert _error(await client.get(SESSION_PROBE_PATH)) == (401, "AUTH_FAILURE", SESSION_INVALID)
    assert _error(await client.get(_SESSION)) == (401, "AUTH_FAILURE", SESSION_INVALID)
    await _drop_cache(container, client)
    assert _error(await client.get(SESSION_PROBE_PATH)) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_sliding_extension_hourly(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    clock = container.clock
    clock.advance(timedelta(hours=2))
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    extended = clock.now() + timedelta(days=14)
    assert (await _only_session(identity, member.user.id)).expires_at == extended
    clock.advance(timedelta(minutes=30))
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert (await _only_session(identity, member.user.id)).expires_at == extended


async def test_absolute_lifetime_90_days(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    created = container.clock.now()
    for _ in range(6):
        container.clock.advance(timedelta(days=13))
        assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert (await _only_session(identity, member.user.id)).expires_at == created + timedelta(days=90)
    container.clock.advance(timedelta(days=11))
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert (await _only_session(identity, member.user.id)).expires_at == created + timedelta(days=90)
    container.clock.advance(timedelta(days=1, seconds=1))
    assert _error(await client.get(SESSION_PROBE_PATH)) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_session_cache_hit_and_revocation(
    monkeypatch: pytest.MonkeyPatch, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    loads: list[str] = []
    original = IdentityRepo.get_active_session_with_user

    async def counting(self: IdentityRepo, token: str, now: Any) -> Any:
        loads.append(token)
        return await original(self, token, now)

    monkeypatch.setattr(IdentityRepo, "get_active_session_with_user", counting)
    await login(client, member.user.email)
    key = session_cache_key(sha256_hex(client.cookies[SESSION_COOKIE]))
    cached = json.loads(await container.redis.get(key) or b"{}")
    assert set(cached) == {"session_id", "user_id", "active_org_id", "mfa_passed", "created_at", "expires_at"}
    assert cached["user_id"] == str(member.user.id)
    assert 0 < await container.redis.ttl(key) <= 300

    for _ in range(2):
        assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert loads == []
    await container.redis.delete(key)
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert len(loads) == 1
    assert await container.redis.exists(key)

    token = client.cookies[SESSION_COOKIE]
    assert (await client.post(_LOGOUT, headers=write_headers(client))).status_code == 200
    assert not await container.redis.exists(key)
    replay = await client.get(SESSION_PROBE_PATH, headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert _error(replay) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_logout_revokes_and_clears_cookies(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    token = client.cookies[SESSION_COOKIE]
    no_csrf = await client.post(_LOGOUT, headers={"Origin": DASHBOARD_URL})
    assert _error(no_csrf) == (403, "FORBIDDEN", CSRF_INVALID)

    response = await client.post(_LOGOUT, json={}, headers=write_headers(client))
    assert response.status_code == 200
    assert response.json() == {"data": {"revoked_sessions": 1}, "errors": None}
    cleared = _set_cookies(response)
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        assert "max-age=0" in cleared[name].lower()
    assert SESSION_COOKIE not in client.cookies
    assert (await _only_session(identity, member.user.id)).revoked_at == container.clock.now()
    replay = await client.get(_SESSION, headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert _error(replay) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_logout_rejects_unknown_body_fields(member: Member, client: httpx.AsyncClient) -> None:
    await login(client, member.user.email)
    response = await client.post(_LOGOUT, json={"everywhere": True}, headers=write_headers(client))
    assert response.status_code == 400


async def test_logout_all_revokes_every_session(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    first = client.cookies[SESSION_COOKIE]
    client.cookies.clear()
    await login(client, member.user.email)
    second = client.cookies[SESSION_COOKIE]
    response = await client.post(_LOGOUT_ALL, headers=write_headers(client))
    assert response.json()["data"] == {"revoked_sessions": 2}
    for token in (first, second):
        assert not await container.redis.exists(session_cache_key(sha256_hex(token)))
        replay = await client.get(SESSION_PROBE_PATH, headers={"Cookie": f"{SESSION_COOKIE}={token}"})
        assert _error(replay) == (401, "AUTH_FAILURE", SESSION_INVALID)
    assert all(row.revoked_at is not None for row in await _sessions_of(identity, member.user.id))


async def test_mfa_required_gate(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await make_member(identity, container.password_hasher, org_settings={"mfa_required": True})
    login_body = (await login(client, member.user.email)).json()
    assert login_body["data"]["mfa_required"] is False
    gated = (401, "MFA_REQUIRED", MFA_REQUIRED_MESSAGE)
    assert _error(await client.get(SESSION_PROBE_PATH)) == gated
    session_info = await client.get(_SESSION)
    assert session_info.status_code == 200
    assert (session_info.json()["data"]["mfa_required"], session_info.json()["data"]["mfa_passed"]) == (
        True,
        False,
    )
    assert _error(await client.post(_LOGOUT_ALL, headers=write_headers(client))) == gated

    row = await _only_session(identity, member.user.id)
    async with identity.database.global_session() as session:
        await IdentityRepo(session).set_mfa_passed(row.id)
    await _drop_cache(container, client)
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert (await client.get(_SESSION)).json()["data"]["mfa_required"] is False

    await login(client, member.user.email)
    assert _error(await client.get(SESSION_PROBE_PATH)) == gated
    assert (await client.post(_LOGOUT, headers=write_headers(client))).status_code == 200


async def test_suspended_and_closed_org(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await make_member(identity, container.password_hasher, org_status="suspended")
    await login(client, member.user.email)
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    write = await client.post(SESSION_PROBE_PATH, headers=write_headers(client))
    assert _error(write) == (403, "ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)
    async with identity.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == member.org.id).values(status="closed"))
    assert _error(await client.get(SESSION_PROBE_PATH)) == (403, "ORG_SUSPENDED", ORG_CLOSED_MESSAGE)


async def test_active_org_fallback_and_no_membership(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    row = await _only_session(identity, member.user.id)
    async with identity.database.global_session() as session:
        await session.execute(update(Session).where(Session.id == row.id).values(active_org_id=None))
    await _drop_cache(container, client)
    assert (await client.get(SESSION_PROBE_PATH)).json()["org_id"] == str(member.org.id)
    assert (await _only_session(identity, member.user.id)).active_org_id == member.org.id

    async with identity.database.system_session() as session:
        await session.execute(delete(Membership).where(Membership.user_id == member.user.id))
    assert _error(await client.get(SESSION_PROBE_PATH)) == (403, "FORBIDDEN", NO_MEMBERSHIP_MESSAGE)
    info = await client.get(_SESSION)
    assert info.status_code == 200
    assert (info.json()["data"]["memberships"], info.json()["data"]["active_org_id"]) == ([], None)


async def test_disabled_user_session_rejected_on_cache_miss(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    await _set_user(identity, member.user.id, status="disabled")
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    await _drop_cache(container, client)
    assert _error(await client.get(SESSION_PROBE_PATH)) == (401, "AUTH_FAILURE", SESSION_INVALID)


async def test_sessions_work_without_redis(identity: IdentityDb) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    container = SessionContainer(identity=identity, redis=fakeredis.FakeAsyncRedis(server=server))
    member = await make_member(identity, container.password_hasher)
    async with session_client(session_app(container)) as http:
        await login(http, member.user.email)
        container.clock.advance(timedelta(hours=2))
        assert (await http.get(SESSION_PROBE_PATH)).status_code == 200
        response = await http.post(_LOGOUT_ALL, headers=write_headers(http))
        assert response.json()["data"] == {"revoked_sessions": 1}


async def test_corrupt_cache_entry_falls_back_to_database(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    key = session_cache_key(sha256_hex(client.cookies[SESSION_COOKIE]))
    await container.redis.set(key, b'{"user_id": "not-a-uuid"}')
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
    assert json.loads(await container.redis.get(key) or b"{}")["user_id"] == str(member.user.id)
