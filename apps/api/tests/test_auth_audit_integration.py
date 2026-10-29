"""Audit rows of the dashboard auth routes on a migrated Postgres (specs/04 §13 decision, P02-T11b).

User-level events land once in every org where the user has a membership, with the user as actor and the
request's address and user agent; ``invitation.accept`` lands in the invitation's org. Each row commits with
the change it describes, so refused requests write none, and no row holds a password, token, or cookie.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb, unique_email
from session_helpers import (
    ACCEPT_INVITATION,
    DASHBOARD_URL,
    MFA_DISABLE,
    MFA_ENABLE,
    MFA_SETUP,
    PASSWORD,
    Member,
    SessionContainer,
    audit_rows,
    enrol_mfa,
    error_of,
    invite,
    link_token,
    login,
    make_member,
    mfa_code,
    session_app,
    session_client,
    signed_in,
    signup,
    write_headers,
)
from sqlalchemy import func, select, update

from va_api.auth.cookies import SESSION_COOKIE
from va_api.auth.login_throttle import EMAIL_FAILURE_LIMIT, email_failure_key
from va_db.models import AuditLog, Session, User
from va_db.repositories import MembershipRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

LOGIN: Final = "/internal/auth/login"
ORIGIN: Final = {"Origin": DASHBOARD_URL}
NEW_PASSWORD: Final = "quiet-harbor-lantern-77"  # noqa: S105 - test users' new password
WRONG_PASSWORD: Final = "not-my-password-at-all"  # noqa: S105 - a wrong guess


async def join(identity: IdentityDb, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Give an existing user a developer membership in ``org_id`` too."""
    async with identity.database.tenant_session(org_id) as session:
        await MembershipRepo(session, org_id).add(user_id, "developer")


async def two_org_member(identity: IdentityDb, container: SessionContainer) -> tuple[Member, uuid.UUID]:
    """A member of their own org who also belongs to a second org; the member and the second org's id."""
    member = await make_member(identity, container.password_hasher)
    other = await make_member(identity, container.password_hasher)
    await join(identity, other.org.id, member.user.id)
    return member, other.org.id


async def attempt(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post(LOGIN, json={"email": email, "password": password}, headers=ORIGIN)


async def all_rows(identity: IdentityDb) -> int:
    async with identity.database.system_session() as session:
        return int(await session.scalar(select(func.count()).select_from(AuditLog)) or 0)


async def session_ids(identity: IdentityDb, user_id: uuid.UUID) -> list[str]:
    async with identity.database.global_session() as session:
        rows = await session.scalars(select(Session.id).where(Session.user_id == user_id))
        return [str(row) for row in rows]


def assert_user_row(row: AuditLog, user_id: uuid.UUID, after: dict[str, Any]) -> None:
    assert (row.actor_type, row.actor_id) == ("user", user_id)
    assert (row.resource_type, row.resource_id) == ("user", str(user_id))
    assert row.changes["after"] == after
    assert str(row.ip) == "127.0.0.1"
    assert (row.user_agent or "").startswith("python-httpx/")


def assert_no_secrets(rows: list[AuditLog], *secrets: str) -> None:
    dumped = json.dumps([row.changes for row in rows])
    for secret in secrets:
        assert secret not in dumped


async def test_login_is_logged_in_every_org_of_the_user(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, other_org = await two_org_member(identity, container)
    await login(client, member.user.email)
    (session_id,) = await session_ids(identity, member.user.id)
    for org_id in (member.org.id, other_org):
        (row,) = await audit_rows(identity, org_id, "auth.login")
        assert_user_row(row, member.user.id, {"method": "password", "session_id": session_id})
        assert row.changes["before"] is None
    rows = await audit_rows(identity, member.org.id)
    assert_no_secrets(rows, PASSWORD, client.cookies.get(SESSION_COOKIE) or "missing-cookie")


async def test_wrong_password_is_logged_as_a_failed_login(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, other_org = await two_org_member(identity, container)
    response = await attempt(client, member.user.email, WRONG_PASSWORD)
    assert error_of(response)[:2] == (401, "INVALID_CREDENTIALS")
    for org_id in (member.org.id, other_org):
        (row,) = await audit_rows(identity, org_id, "auth.login_failed")
        assert_user_row(row, member.user.id, {"reason": "invalid_password"})
        assert await audit_rows(identity, org_id, "auth.login") == []
    assert_no_secrets(await audit_rows(identity, member.org.id), WRONG_PASSWORD)


async def test_disabled_account_is_logged_with_its_reason(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    async with identity.database.global_session() as session:
        await session.execute(update(User).where(User.id == member.user.id).values(status="disabled"))
    assert error_of(await attempt(client, member.user.email, PASSWORD))[:2] == (401, "INVALID_CREDENTIALS")
    (row,) = await audit_rows(identity, member.org.id, "auth.login_failed")
    assert_user_row(row, member.user.id, {"reason": "account_disabled"})


async def test_unknown_email_and_locked_logins_write_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    before = await all_rows(identity)
    assert error_of(await attempt(client, unique_email("nobody"), PASSWORD))[0] == 401
    assert await all_rows(identity) == before
    await container.redis.set(email_failure_key(member.user.email), EMAIL_FAILURE_LIMIT)
    assert (await attempt(client, member.user.email, WRONG_PASSWORD)).status_code == 429
    assert await all_rows(identity) == before


async def test_logout_and_logout_all_are_logged(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, other_org = await two_org_member(identity, container)
    await login(client, member.user.email)
    response = await client.post("/internal/auth/logout", headers=write_headers(client))
    assert response.status_code == 200, response.text
    for org_id in (member.org.id, other_org):
        (row,) = await audit_rows(identity, org_id, "auth.logout")
        assert_user_row(row, member.user.id, {"revoked_sessions": 1, "all": False})

    await login(client, member.user.email)
    client.cookies.delete(SESSION_COOKIE)
    await login(client, member.user.email)
    response = await client.post("/internal/auth/logout-all", headers=write_headers(client))
    assert response.json()["data"]["revoked_sessions"] == 2
    rows = await audit_rows(identity, member.org.id, "auth.logout")
    assert [row.changes["after"] for row in rows] == [
        {"revoked_sessions": 1, "all": False},
        {"revoked_sessions": 2, "all": True},
    ]


async def test_mfa_enable_and_disable_are_logged(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    setup = await client.post(MFA_SETUP, headers=write_headers(client))
    assert setup.status_code == 200, setup.text
    refused = await client.post(MFA_ENABLE, json={"code": "000000"}, headers=write_headers(client))
    assert error_of(refused)[1] == "INVALID_MFA_CODE"
    assert await audit_rows(identity, member.org.id, "auth.mfa_enabled") == []

    container.clock.advance(30)
    secret, _ = await enrol_mfa(client, container)
    (enabled,) = await audit_rows(identity, member.org.id, "auth.mfa_enabled")
    assert_user_row(enabled, member.user.id, {"mfa_enabled": True})
    assert enabled.changes["before"] == {"mfa_enabled": False}

    disable = await client.post(
        MFA_DISABLE, json={"code": mfa_code(container, secret)}, headers=write_headers(client)
    )
    assert disable.status_code == 200, disable.text
    (disabled,) = await audit_rows(identity, member.org.id, "auth.mfa_disabled")
    assert_user_row(disabled, member.user.id, {"mfa_enabled": False})
    assert disabled.changes["before"] == {"mfa_enabled": True}
    assert_no_secrets([enabled, disabled], secret)


async def change(client: httpx.AsyncClient, current: str) -> httpx.Response:
    body = {"current_password": current, "new_password": NEW_PASSWORD}
    return await client.post("/internal/auth/password/change", json=body, headers=write_headers(client))


async def test_password_change_is_logged_and_a_wrong_current_password_fails_a_login(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    wrong = await change(client, WRONG_PASSWORD)
    assert error_of(wrong)[:2] == (401, "INVALID_CREDENTIALS")
    (failed,) = await audit_rows(identity, member.org.id, "auth.login_failed")
    assert_user_row(failed, member.user.id, {"reason": "invalid_current_password"})
    assert await audit_rows(identity, member.org.id, "auth.password_changed") == []

    async with session_client(session_app(container)) as other:
        await login(other, member.user.email)
    assert (await change(client, PASSWORD)).status_code == 200
    (changed,) = await audit_rows(identity, member.org.id, "auth.password_changed")
    assert_user_row(changed, member.user.id, {"revoked_sessions": 1})
    assert_no_secrets(await audit_rows(identity, member.org.id), PASSWORD, NEW_PASSWORD, WRONG_PASSWORD)


async def test_password_reset_is_logged_and_a_bad_token_writes_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    forgot = await client.post(
        "/internal/auth/password/forgot", json={"email": member.user.email}, headers=ORIGIN
    )
    assert forgot.status_code == 200, forgot.text
    token = link_token(container.outbox, member.user.email, "password_reset")
    before = await all_rows(identity)
    bad = await client.post(
        "/internal/auth/password/reset", json={"token": "x" * 43, "password": NEW_PASSWORD}, headers=ORIGIN
    )
    assert error_of(bad)[1] == "TOKEN_EXPIRED"
    assert await all_rows(identity) == before

    reset = await client.post(
        "/internal/auth/password/reset", json={"token": token, "password": NEW_PASSWORD}, headers=ORIGIN
    )
    assert reset.status_code == 200, reset.text
    (row,) = await audit_rows(identity, member.org.id, "auth.password_reset")
    assert_user_row(row, member.user.id, {"revoked_sessions": 1})
    assert_no_secrets([row], token, NEW_PASSWORD)


async def test_accepting_an_invitation_is_logged_in_its_org(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    new_email = unique_email("newbie")
    new_token = await invite(client, container, new_email)
    existing = await make_member(identity, container.password_hasher)
    existing_token = await invite(client, container, existing.user.email, "analyst")
    client.cookies.clear()

    body = {"token": new_token, "name": "Lena Ortiz", "password": NEW_PASSWORD}
    created = await client.post(ACCEPT_INVITATION, json=body, headers=ORIGIN)
    assert created.status_code == 200, created.text
    new_user_id = uuid.UUID(created.json()["data"]["user"]["id"])
    client.cookies.clear()
    await login(client, existing.user.email)
    joined = await client.post(
        ACCEPT_INVITATION, json={"token": existing_token}, headers=write_headers(client)
    )
    assert joined.status_code == 200, joined.text
    used = await client.post(ACCEPT_INVITATION, json={"token": existing_token}, headers=write_headers(client))
    assert error_of(used)[1] == "TOKEN_EXPIRED"

    rows = await audit_rows(identity, owner.org.id, "invitation.accept")
    assert [(row.actor_type, row.actor_id, row.resource_type) for row in rows] == [
        ("user", new_user_id, "invitation"),
        ("user", existing.user.id, "invitation"),
    ]
    assert [row.changes["after"] for row in rows] == [
        {"user_id": str(new_user_id), "email": new_email, "role": "developer", "user_created": True},
        {
            "user_id": str(existing.user.id),
            "email": existing.user.email,
            "role": "analyst",
            "user_created": False,
        },
    ]
    assert_no_secrets(rows, new_token, existing_token, NEW_PASSWORD)


async def test_exempt_routes_write_no_rows(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    email = unique_email("founder")
    response = await signup(client, email=email)
    assert response.status_code == 201, response.text
    org_id = uuid.UUID(response.json()["data"]["org"]["id"])
    token = link_token(container.outbox, email)
    verified = await client.post("/internal/auth/verify-email", json={"token": token}, headers=ORIGIN)
    assert verified.status_code == 200, verified.text
    assert (await client.post(MFA_SETUP, headers=write_headers(client))).status_code == 200
    switched = await client.post(
        "/internal/auth/switch-org", json={"org_id": str(org_id)}, headers=write_headers(client)
    )
    assert switched.status_code == 200, switched.text
    assert await audit_rows(identity, org_id) == []
