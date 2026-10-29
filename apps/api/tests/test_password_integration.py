"""Password forgot, reset, and change on a migrated Postgres (specs/04 §4.1, §4.2).

Through the real app with fake Redis, a frozen clock, and the in-memory email sender; the reset email is a
background task, which httpx's ASGI transport runs before the call returns.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from identity_db import IdentityDb, unique_email
from session_helpers import (
    DASHBOARD_URL,
    PASSWORD,
    Member,
    SessionContainer,
    error_of,
    link_token,
    login,
    make_member,
    session_app,
    session_client,
    write_headers,
)
from sqlalchemy import update
from structlog.testing import capture_logs

from va_api.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from va_api.auth.csrf import CSRF_INVALID, ORIGIN_NOT_ALLOWED
from va_api.auth.login_throttle import EMAIL_FAILURE_LIMIT, email_failure_key
from va_api.auth.password_reset import (
    CURRENT_PASSWORD_MESSAGE,
    REAUTH_MESSAGE,
    REAUTH_WINDOW,
    RESET_HOURLY_LIMIT,
    RESET_LIMIT_WINDOW,
    RESET_TOKEN_INVALID_MESSAGE,
)
from va_api.auth.sessions import SESSION_INVALID, session_cache_key
from va_common.auth import sha256_hex
from va_db.models import Org, User
from va_db.repositories import EMAIL_VERIFY_TTL, PASSWORD_RESET_TTL, IdentityRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_FORGOT = "/internal/auth/password/forgot"
_RESET = "/internal/auth/password/reset"
_CHANGE = "/internal/auth/password/change"
_SESSION = "/internal/auth/session"
_LOGIN = "/internal/auth/login"
NEW_PASSWORD = "quiet-harbor-lantern-77"  # noqa: S105 - test users' new password
_ORIGIN = {"Origin": DASHBOARD_URL}


async def _member(identity: IdentityDb, container: SessionContainer) -> Member:
    return await make_member(identity, container.password_hasher)


async def _forgot(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post(_FORGOT, json={"email": email}, headers=_ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"data": {"accepted": True}, "errors": None}


async def _reset_token(client: httpx.AsyncClient, container: SessionContainer, email: str) -> str:
    await _forgot(client, email)
    return link_token(container.outbox, email, "password_reset")


async def _reset(client: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD) -> httpx.Response:
    return await client.post(_RESET, json={"token": token, "password": password}, headers=_ORIGIN)


async def _try_login(client: httpx.AsyncClient, email: str, password: str) -> int:
    response = await client.post(_LOGIN, json={"email": email, "password": password}, headers=_ORIGIN)
    return response.status_code


async def _user(identity: IdentityDb, email: str) -> User:
    async with identity.database.global_session() as session:
        user = await IdentityRepo(session).get_user_by_email(email)
    assert user is not None
    return user


async def _change(
    client: httpx.AsyncClient, current: str = PASSWORD, new: str = NEW_PASSWORD
) -> httpx.Response:
    return await client.post(
        _CHANGE, json={"current_password": current, "new_password": new}, headers=write_headers(client)
    )


async def test_forgot_then_reset_flow(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await _member(identity, container)
    email = member.user.email
    async with identity.database.system_session() as session:
        await session.execute(update(User).where(User.id == member.user.id).values(email_verified_at=None))
    await login(client, email)
    old_cookie = client.cookies[SESSION_COOKIE]

    await _forgot(client, email)
    (message,) = container.outbox.to(email)
    assert message.template == "password_reset"
    assert f"{DASHBOARD_URL}/reset-password/" in message.text
    reset = await _reset(client, link_token(container.outbox, email, "password_reset"))
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"] == {"revoked_sessions": 1}
    cleared = reset.headers.get_list("set-cookie")
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        assert any(header.startswith(f"{name}=") and "max-age=0" in header.lower() for header in cleared)

    async with session_client(session_app(container)) as stale:
        stale.cookies.set(SESSION_COOKIE, old_cookie)
        response = await stale.get(_SESSION)
        assert response.status_code == 401
        assert response.json()["errors"][0]["message"] == SESSION_INVALID
    assert await _try_login(client, email, PASSWORD) == 401
    assert await _try_login(client, email, NEW_PASSWORD) == 200
    assert (await _user(identity, email)).email_verified_at == container.clock.now()


async def test_forgot_unknown_and_disabled_users(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await _member(identity, container)
    async with identity.database.system_session() as session:
        await session.execute(update(User).where(User.id == member.user.id).values(status="disabled"))
    unknown = unique_email("nobody.private")
    with capture_logs() as logs:
        await _forgot(client, unknown)
        await _forgot(client, member.user.email)
    assert container.outbox.sent == []
    reasons = [entry["reason"] for entry in logs if entry["event"] == "password_reset_suppressed"]
    assert reasons == ["unknown_user", "disabled"]
    assert "nobody.private" not in repr(logs)
    assert member.user.email.split("@")[0] not in repr(logs)


async def test_forgot_hourly_limit(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    for _ in range(RESET_HOURLY_LIMIT):
        await _forgot(client, email)
        container.clock.advance(60)
    assert len(container.outbox.to(email)) == RESET_HOURLY_LIMIT
    with capture_logs() as logs:
        await _forgot(client, email)
    assert len(container.outbox.to(email)) == RESET_HOURLY_LIMIT
    assert [entry["reason"] for entry in logs if entry["event"] == "password_reset_suppressed"] == ["limit"]
    container.clock.advance(RESET_LIMIT_WINDOW - timedelta(seconds=60 * (RESET_HOURLY_LIMIT - 1)))
    await _forgot(client, email)
    assert len(container.outbox.to(email)) == RESET_HOURLY_LIMIT + 1


async def test_reset_token_reuse_expiry_and_purpose(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    token = await _reset_token(client, container, email)
    assert (await _reset(client, token)).status_code == 200
    reused = await _reset(client, token, "another-fresh-passphrase-9")
    assert error_of(reused) == (400, "TOKEN_EXPIRED", "token")
    assert reused.json()["errors"][0]["message"] == RESET_TOKEN_INVALID_MESSAGE
    assert error_of(await _reset(client, "not a token")) == (400, "TOKEN_EXPIRED", "token")
    assert error_of(await _reset(client, "A" * 43)) == (400, "TOKEN_EXPIRED", "token")

    user = await _user(identity, email)
    async with identity.database.global_session() as session:
        verify = await IdentityRepo(session).issue_email_token(
            user_id=user.id, purpose="verify_email", now=container.clock.now(), ttl=EMAIL_VERIFY_TTL
        )
    assert error_of(await _reset(client, verify.plaintext)) == (400, "TOKEN_EXPIRED", "token")
    container.clock.advance(RESET_LIMIT_WINDOW)
    late = await _reset_token(client, container, email)
    container.clock.advance(PASSWORD_RESET_TTL + timedelta(seconds=1))
    assert error_of(await _reset(client, late)) == (400, "TOKEN_EXPIRED", "token")
    assert await _try_login(client, email, NEW_PASSWORD) == 200


async def test_reset_weak_password_keeps_token(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    token = await _reset_token(client, container, email)
    for weak in ("short-pw", f"{email.split('@')[0]}-orchard-42"):
        assert error_of(await _reset(client, token, weak)) == (400, "PASSWORD_TOO_WEAK", "password")
    assert await _try_login(client, email, PASSWORD) == 200
    assert (await _reset(client, token)).status_code == 200


async def test_reset_retires_other_links(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    first = await _reset_token(client, container, email)
    second = await _reset_token(client, container, email)
    assert first != second
    assert (await _reset(client, second)).status_code == 200
    assert error_of(await _reset(client, first, "another-fresh-passphrase-9"))[1] == "TOKEN_EXPIRED"


async def test_reset_disabled_user_rejected(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await _member(identity, container)
    token = await _reset_token(client, container, member.user.email)
    async with identity.database.system_session() as session:
        await session.execute(update(User).where(User.id == member.user.id).values(status="disabled"))
    assert error_of(await _reset(client, token)) == (400, "TOKEN_EXPIRED", "token")


async def test_reset_revokes_all_sessions(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    async with session_client(session_app(container)) as other:
        await login(client, email)
        await login(other, email)
        hashes = [sha256_hex(http.cookies[SESSION_COOKIE]) for http in (client, other)]
        for token_hash in hashes:
            assert await container.redis.exists(session_cache_key(token_hash))
        async with session_client(session_app(container)) as anonymous:
            token = await _reset_token(anonymous, container, email)
            response = await _reset(anonymous, token)
        assert response.json()["data"] == {"revoked_sessions": 2}
        for token_hash in hashes:
            assert not await container.redis.exists(session_cache_key(token_hash))
        assert (await other.get(_SESSION)).status_code == 401
        assert (await client.get(_SESSION)).status_code == 401


async def test_reset_clears_login_lock(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    for _ in range(EMAIL_FAILURE_LIMIT):
        assert await _try_login(client, email, "wrong-password-guess") == 401
    assert await _try_login(client, email, PASSWORD) == 429
    token = await _reset_token(client, container, email)
    assert (await _reset(client, token)).status_code == 200
    assert not await container.redis.exists(email_failure_key(email))
    assert await _try_login(client, email, NEW_PASSWORD) == 200


async def test_change_password(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    async with session_client(session_app(container)) as other:
        await login(other, email)
        await login(client, email)
        stale_reset = await _reset_token(other, container, email)
        response = await _change(client)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"revoked_sessions": 1}
        assert (await client.get(_SESSION)).status_code == 200
        assert (await other.get(_SESSION)).status_code == 401
    assert error_of(await _reset(client, stale_reset, "another-fresh-passphrase-9"))[1] == "TOKEN_EXPIRED"
    async with session_client(session_app(container)) as fresh:
        assert await _try_login(fresh, email, PASSWORD) == 401
        assert await _try_login(fresh, email, NEW_PASSWORD) == 200


async def test_change_requires_recent_auth(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    await login(client, email)
    container.clock.advance(REAUTH_WINDOW + timedelta(seconds=1))
    response = await _change(client)
    assert error_of(response) == (401, "REAUTH_REQUIRED", None)
    assert response.json()["errors"][0]["message"] == REAUTH_MESSAGE
    assert await _try_login(client, email, PASSWORD) == 200
    assert (await _change(client)).status_code == 200


async def test_change_wrong_current_password(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    await login(client, email)
    wrong = await _change(client, current="not-my-password-at-all")
    assert error_of(wrong) == (401, "INVALID_CREDENTIALS", "current_password")
    assert wrong.json()["errors"][0]["message"] == CURRENT_PASSWORD_MESSAGE
    for _ in range(EMAIL_FAILURE_LIMIT - 1):
        assert error_of(await _change(client, current="not-my-password-at-all"))[1] == "INVALID_CREDENTIALS"
    locked = await _change(client)
    assert error_of(locked)[:2] == (429, "AUTH_LOCKED")
    assert "retry-after" in locked.headers
    response = await client.post(_LOGIN, json={"email": email, "password": PASSWORD}, headers=_ORIGIN)
    assert error_of(response)[:2] == (429, "AUTH_LOCKED")


async def test_change_weak_new_password(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    await login(client, email)
    weak = await _change(client, new=f"{email.split('@')[0]}-orchard-42")
    assert error_of(weak) == (400, "PASSWORD_TOO_WEAK", "new_password")
    assert weak.json()["errors"][0]["message"] == "Password must not contain your email address."
    assert error_of(await _change(client, new="short-pw")) == (400, "PASSWORD_TOO_WEAK", "new_password")
    assert await _try_login(client, email, PASSWORD) == 200


async def test_change_requires_session_and_csrf(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    anonymous = await client.post(_CHANGE, json=body, headers=_ORIGIN)
    assert error_of(anonymous)[:2] == (401, "AUTH_FAILURE")
    await login(client, (await _member(identity, container)).user.email)
    forged = await client.post(_CHANGE, json=body, headers=_ORIGIN)
    assert error_of(forged)[:2] == (403, "FORBIDDEN")
    assert forged.json()["errors"][0]["message"] == CSRF_INVALID
    extra = await client.post(_CHANGE, json={**body, "email": "x@y.test"}, headers=write_headers(client))
    assert error_of(extra)[:2] == (400, "INVALID_INPUT")


async def test_change_mfa_gated(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await _member(identity, container)
    await login(client, member.user.email)
    async with identity.database.system_session() as session:
        await session.execute(
            update(Org).where(Org.id == member.org.id).values(settings={"mfa_required": True})
        )
    assert error_of(await _change(client))[:2] == (401, "MFA_REQUIRED")


async def test_password_routes_foreign_origin(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = (await _member(identity, container)).user.email
    evil = {"Origin": "https://evil.test"}
    forgot = await client.post(_FORGOT, json={"email": email}, headers=evil)
    assert error_of(forgot)[:2] == (403, "FORBIDDEN")
    assert forgot.json()["errors"][0]["message"] == ORIGIN_NOT_ALLOWED
    assert container.outbox.sent == []
    cli = await client.post(_FORGOT, json={"email": email})
    assert cli.status_code == 200
    token = link_token(container.outbox, email, "password_reset")
    body = {"token": token, "password": NEW_PASSWORD}
    assert error_of(await client.post(_RESET, json=body, headers=evil))[:2] == (403, "FORBIDDEN")
    assert (await client.post(_RESET, json=body)).status_code == 200


async def test_forgot_validation(client: httpx.AsyncClient) -> None:
    for body in ({}, {"email": "a"}, {"email": unique_email("x"), "name": "Jane"}):
        response = await client.post(_FORGOT, json=body, headers=_ORIGIN)
        assert error_of(response)[:2] == (400, "INVALID_INPUT"), body
