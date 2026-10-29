"""Signup, email verification, and verification resend on a migrated Postgres (specs/04 §4.1).

Through the real app with fake Redis, a frozen clock, and the in-memory email sender; the verification email
is a background task, which httpx's ASGI transport runs before the call returns.
"""

from __future__ import annotations

import re
from datetime import timedelta

import fakeredis
import httpx
import pytest
from identity_db import IdentityDb, row_counts, unique_email
from session_helpers import (
    DASHBOARD_URL,
    PASSWORD,
    SESSION_PROBE_PATH,
    Member,
    SessionContainer,
    error_of,
    link_token,
    login,
    make_member,
    session_app,
    session_client,
    signup,
    write_headers,
)
from sqlalchemy import select, update
from structlog.testing import capture_logs

from va_api.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from va_api.auth.csrf import CSRF_INVALID, ORIGIN_NOT_ALLOWED
from va_api.auth.sessions import SESSION_INVALID, session_cache_key
from va_api.auth.verification import RESEND_LIMIT, TOKEN_INVALID_MESSAGE, resend_key
from va_common.auth import sha256_hex
from va_common.auth.passwords import common_passwords
from va_common.email import EmailDeliveryError, EmailMessage
from va_db.models import EmailToken, Membership, Org, Session, User
from va_db.repositories import EMAIL_VERIFY_TTL, IdentityRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_SIGNUP = "/internal/auth/signup"
_VERIFY = "/internal/auth/verify-email"
_RESEND = "/internal/auth/resend-verification"
_SLUG = re.compile(r"^acme-dental-[a-z0-9]{4}$")


class FailingSender:
    """An email sender whose relay rejects every message."""

    async def send(self, message: EmailMessage) -> None:
        msg = f"delivery of {message.template!r} failed: SMTPRecipientsRefused"
        raise EmailDeliveryError(msg)


async def _user(identity: IdentityDb, email: str) -> User:
    async with identity.database.global_session() as session:
        user = await IdentityRepo(session).get_user_by_email(email)
    assert user is not None
    return user


async def _signed_up(client: httpx.AsyncClient, email: str) -> httpx.Response:
    response = await signup(client, email=email)
    assert response.status_code == 201, response.text
    return response


async def test_signup_verify_flow(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("jane")
    response = await _signed_up(client, email)
    body = response.json()
    assert body["errors"] is None
    user, org = body["data"]["user"], body["data"]["org"]
    assert (user["email"], user["name"], user["email_verified"], user["mfa_enabled"]) == (
        email,
        "Jane Park",
        False,
        False,
    )
    assert (org["name"], org["plan_id"], org["status"]) == ("Acme Dental", "start", "active")
    assert _SLUG.fullmatch(org["slug"])

    for header in response.headers.get_list("set-cookie"):
        assert "samesite=lax" in header.lower()
        assert "secure" in header.lower()
    token = client.cookies[SESSION_COOKIE]
    assert client.cookies[CSRF_COOKIE]
    now = container.clock.now()
    async with identity.database.global_session() as session:
        row = await session.scalar(select(Session).where(Session.token_hash == sha256_hex(token)))
        stored = await IdentityRepo(session).get_user_by_email(email)
    assert row is not None
    assert stored is not None
    assert (str(row.user_id), str(row.active_org_id)) == (user["id"], org["id"])
    assert (row.created_at, row.expires_at) == (now, now + timedelta(days=14))
    assert stored.last_login_at == now
    assert await container.redis.exists(session_cache_key(sha256_hex(token)))

    probe = await client.get(SESSION_PROBE_PATH)
    assert probe.status_code == 200, probe.text
    assert (probe.json()["role"], probe.json()["org_id"]) == ("owner", org["id"])

    async with identity.database.system_session() as session:
        org_row = await session.get(Org, org["id"])
    assert org_row is not None
    secret = await container.provisioner.webhook_signing_secret(org_row)
    assert secret.startswith("whsec_")

    (message,) = container.outbox.to(email)
    assert message.template == "verify_email"
    assert f"{DASHBOARD_URL}/verify-email/" in message.text
    verified = await client.post(_VERIFY, json={"token": link_token(container.outbox, email)})
    assert verified.status_code == 200, verified.text
    assert verified.json()["data"]["user"]["email_verified"] is True
    assert (await _user(identity, email)).email_verified_at == now


async def test_signup_rejects_weak_password(identity: IdentityDb, client: httpx.AsyncClient) -> None:
    email = unique_email("weak")
    breached = next(entry for entry in sorted(common_passwords()) if len(entry) >= 12)
    before = await row_counts(identity.database)
    for password in ("short-pw", breached, f"{email.split('@')[0]}-orchard-42"):
        response = await signup(client, email=email, password=password)
        assert error_of(response) == (400, "PASSWORD_TOO_WEAK", "password"), password
    assert await row_counts(identity.database) == before
    assert SESSION_COOKIE not in client.cookies


async def test_signup_duplicate_email(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("dup")
    await _signed_up(client, email)
    before = await row_counts(identity.database)
    async with session_client(session_app(container)) as other:
        response = await signup(other, email=email.upper())
        assert SESSION_COOKIE not in other.cookies
    status, code, field = error_of(response)
    assert (status, code, field) == (409, "CONFLICT", "email")
    assert response.json()["errors"][0]["message"] == "An account with this email already exists."
    assert await row_counts(identity.database) == before
    assert len(container.outbox.to(email)) == 1


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"org_name": None}, "org_name"),
        ({"org_name": "   "}, "org_name"),
        ({"name": ""}, "name"),
        ({"email": "a"}, "email"),
        ({"plan_id": "scale"}, "plan_id"),
    ],
)
async def test_signup_validation(
    client: httpx.AsyncClient, override: dict[str, str | None], field: str
) -> None:
    body: dict[str, str | None] = {
        "email": unique_email("val"),
        "password": PASSWORD,
        "name": "Jane Park",
        "org_name": "Acme Dental",
    }
    body.update(override)
    payload = {key: value for key, value in body.items() if value is not None}
    response = await client.post(_SIGNUP, json=payload, headers={"Origin": DASHBOARD_URL})
    assert response.status_code == 400
    errors = response.json()["errors"]
    assert {item["error"] for item in errors} == {"INVALID_INPUT"}
    assert field in {item["field"] for item in errors}


async def test_signup_origin_rules(client: httpx.AsyncClient) -> None:
    foreign = await signup(client, email=unique_email("evil"), headers={"Origin": "https://evil.test"})
    assert foreign.status_code == 403
    assert foreign.json()["errors"][0]["message"] == ORIGIN_NOT_ALLOWED
    cli = await signup(client, email=unique_email("cli"), headers={})
    assert cli.status_code == 201, cli.text


async def test_signup_revokes_presented_session(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member: Member = await make_member(identity, container.password_hasher)
    await login(client, member.user.email)
    previous = client.cookies[SESSION_COOKIE]
    await _signed_up(client, unique_email("fresh"))
    assert client.cookies[SESSION_COOKIE] != previous
    async with identity.database.global_session() as session:
        row = await session.scalar(select(Session).where(Session.token_hash == sha256_hex(previous)))
    assert row is not None
    assert row.revoked_at == container.clock.now()


async def test_signup_email_failure_does_not_fail(identity: IdentityDb) -> None:
    container = SessionContainer(identity=identity, email_sender=FailingSender())
    email = unique_email("jane.private")
    with capture_logs() as logs:
        async with session_client(session_app(container)) as http:
            response = await signup(http, email=email)
    assert response.status_code == 201, response.text
    (entry,) = [item for item in logs if item["event"] == "verification_email_failed"]
    assert (entry["template"], entry["error_type"]) == ("verify_email", "EmailDeliveryError")
    assert "jane.private" not in repr(logs)


async def test_verify_token_reuse_expiry_and_purpose(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("reuse")
    await _signed_up(client, email)
    token = link_token(container.outbox, email)
    assert (await client.post(_VERIFY, json={"token": token})).status_code == 200
    reused = await client.post(_VERIFY, json={"token": token})
    assert error_of(reused) == (400, "TOKEN_EXPIRED", "token")
    assert reused.json()["errors"][0]["message"] == TOKEN_INVALID_MESSAGE
    assert error_of(await client.post(_VERIFY, json={"token": "not a token"})) == (
        400,
        "TOKEN_EXPIRED",
        "token",
    )
    assert error_of(await client.post(_VERIFY, json={"token": "A" * 43})) == (400, "TOKEN_EXPIRED", "token")

    user = await _user(identity, email)
    async with identity.database.global_session() as session:
        repo = IdentityRepo(session)
        reset = await repo.issue_email_token(
            user_id=user.id, purpose="reset_password", now=container.clock.now(), ttl=timedelta(hours=1)
        )
        late = await repo.issue_email_token(
            user_id=user.id, purpose="verify_email", now=container.clock.now(), ttl=EMAIL_VERIFY_TTL
        )
    assert error_of(await client.post(_VERIFY, json={"token": reset.plaintext}))[1] == "TOKEN_EXPIRED"
    container.clock.advance(EMAIL_VERIFY_TTL + timedelta(seconds=1))
    assert error_of(await client.post(_VERIFY, json={"token": late.plaintext}))[1] == "TOKEN_EXPIRED"


async def test_verify_keeps_first_verification_time(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("twice")
    await _signed_up(client, email)
    first = link_token(container.outbox, email)
    resent = await client.post(_RESEND, json={}, headers=write_headers(client))
    assert resent.json()["data"] == {"sent": True, "email_verified": False}
    second = link_token(container.outbox, email)
    verified_at = container.clock.now()
    assert (await client.post(_VERIFY, json={"token": first})).status_code == 200
    container.clock.advance(60)
    again = await client.post(_VERIFY, json={"token": second})
    assert again.status_code == 200
    assert again.json()["data"]["user"]["email_verified"] is True
    assert (await _user(identity, email)).email_verified_at == verified_at


async def test_resend_verification_limit(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("resend")
    await _signed_up(client, email)
    user = await _user(identity, email)
    for _ in range(RESEND_LIMIT):
        response = await client.post(_RESEND, headers=write_headers(client))
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"sent": True, "email_verified": False}
    assert len(container.outbox.to(email)) == RESEND_LIMIT + 1
    limited = await client.post(_RESEND, json={}, headers=write_headers(client))
    status, code, _ = error_of(limited)
    assert (status, code) == (429, "RATE_LIMITED")
    assert 0 < int(limited.headers["retry-after"]) <= 3600
    assert limited.json()["errors"][0]["message"].startswith("Too many verification emails; try again in ")
    assert len(container.outbox.to(email)) == RESEND_LIMIT + 1
    async with identity.database.global_session() as session:
        issued = await session.scalars(select(EmailToken).where(EmailToken.user_id == user.id))
        assert len(list(issued)) == RESEND_LIMIT + 1

    await container.redis.delete(resend_key(user.id))
    assert (await client.post(_RESEND, headers=write_headers(client))).status_code == 200


async def test_resend_requires_session_and_csrf(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    anonymous = await client.post(_RESEND, json={}, headers={"Origin": DASHBOARD_URL})
    assert anonymous.status_code == 401
    assert anonymous.json()["errors"][0]["message"] == SESSION_INVALID
    await _signed_up(client, unique_email("csrf"))
    forged = await client.post(_RESEND, json={}, headers={"Origin": DASHBOARD_URL})
    assert forged.status_code == 403
    assert forged.json()["errors"][0]["message"] == CSRF_INVALID
    unknown_field = await client.post(_RESEND, json={"email": "x@y.test"}, headers=write_headers(client))
    assert unknown_field.status_code == 400


async def test_resend_when_verified(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("done")
    await _signed_up(client, email)
    await client.post(_VERIFY, json={"token": link_token(container.outbox, email)})
    container.outbox.clear()
    response = await client.post(_RESEND, json={}, headers=write_headers(client))
    assert response.status_code == 200
    assert response.json()["data"] == {"sent": False, "email_verified": True}
    assert container.outbox.sent == []
    user = await _user(identity, email)
    assert not await container.redis.exists(resend_key(user.id))


async def test_resend_is_mfa_gated(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("mfa")
    org_id = (await _signed_up(client, email)).json()["data"]["org"]["id"]
    async with identity.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == org_id).values(settings={"mfa_required": True}))
    response = await client.post(_RESEND, json={}, headers=write_headers(client))
    assert error_of(response)[:2] == (401, "MFA_REQUIRED")


async def test_resend_redis_down_allows(identity: IdentityDb) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    container = SessionContainer(identity=identity, redis=fakeredis.FakeAsyncRedis(server=server))
    email = unique_email("down")
    async with session_client(session_app(container)) as http:
        await _signed_up(http, email)
        for _ in range(RESEND_LIMIT + 1):
            response = await http.post(_RESEND, json={}, headers=write_headers(http))
            assert response.status_code == 200, response.text
    assert len(container.outbox.to(email)) == RESEND_LIMIT + 2


async def test_signup_membership_is_owner(identity: IdentityDb, client: httpx.AsyncClient) -> None:
    email = unique_email("owner")
    data = (await _signed_up(client, email)).json()["data"]
    async with identity.database.system_session() as session:
        roles = await session.scalars(select(Membership.role).where(Membership.org_id == data["org"]["id"]))
        assert list(roles) == ["owner"]
