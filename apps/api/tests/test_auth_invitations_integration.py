"""``/internal/auth/invitations`` details and accept against a real database (specs/04 §4.1, §6)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fake_oidc import configure_sso
from identity_db import IdentityDb, unique_email
from session_helpers import (
    ACCEPT_INVITATION,
    API_BASE_URL,
    DASHBOARD_URL,
    PASSWORD,
    Member,
    SessionContainer,
    add_member,
    enrol_mfa,
    error_of,
    invite,
    login,
    make_member,
    signed_in,
    write_headers,
)
from sqlalchemy import func, select, update

from va_api.auth.api_key_auth import ORG_SUSPENDED_MESSAGE
from va_api.auth.cookies import SESSION_COOKIE
from va_api.auth.invitations import ALREADY_MEMBER_MESSAGE, INVITATION_INVALID_MESSAGE
from va_api.auth.session_auth import MFA_REQUIRED_MESSAGE, ORG_CLOSED_MESSAGE
from va_api.routers.auth import SSO_REQUIRED_MESSAGE
from va_api.routers.auth_invitations import (
    NAME_REQUIRED_MESSAGE,
    OTHER_EMAIL_MESSAGE,
    PASSWORD_REQUIRED_MESSAGE,
    SIGN_IN_MESSAGE,
)
from va_common.auth import sha256_hex
from va_db.models import Invitation, MemberRole, Membership, Org, OrgStatus, User

SESSION = "/internal/auth/session"
NEW_PASSWORD = "copper-lantern-meadow-58"  # noqa: S105 - test users' password
SEVEN_DAYS_S = 7 * 24 * 3600
NEW_ACCOUNT = {"name": "Priya Nair", "password": NEW_PASSWORD}


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


async def invited(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    *,
    email: str | None = None,
    role: MemberRole = "developer",
) -> tuple[Member, str, str]:
    """An owner invites ``email`` (a fresh address by default) and signs out; the owner, email, and token."""
    owner = await signed_in(client, identity, container)
    address = email or unique_email("priya")
    token = await invite(client, container, address, role)
    client.cookies.clear()
    return owner, address, token


async def invited_existing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> tuple[Member, Member, str]:
    """A user with an account (in an org of their own) invited into another org; the user, the inviting
    owner, and the token."""
    existing = await make_member(identity, container.password_hasher)
    owner, _, token = await invited(client, identity, container, email=existing.user.email)
    return existing, owner, token


async def details(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.get(f"/internal/auth/invitations/{token}")


async def accept(
    client: httpx.AsyncClient, body: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    sent = headers if headers is not None else {"Origin": DASHBOARD_URL}
    return await client.post(ACCEPT_INVITATION, json=body, headers=sent)


async def accept_signed_in(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(ACCEPT_INVITATION, json={"token": token}, headers=write_headers(client))


async def membership_count(identity: IdentityDb, org_id: uuid.UUID, email: str) -> int:
    async with identity.database.system_session() as session:
        statement = (
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id, User.email == email)
        )
        return (await session.execute(statement)).scalar_one()


async def set_org_status(identity: IdentityDb, org_id: uuid.UUID, status: OrgStatus) -> None:
    async with identity.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == org_id).values(status=status))


async def test_details_of_an_open_invitation(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner, email, token = await invited(client, identity, container, role="analyst")
    response = await details(client, token)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    expires_at = container.clock.now().timestamp() + SEVEN_DAYS_S
    assert data == {
        "org_name": owner.org.name,
        "role": "analyst",
        "email": email,
        "inviter_name": "Jane Park",
        "expires_at": data["expires_at"],
        "account_exists": False,
    }
    async with identity.database.system_session() as session:
        row = await session.scalar(select(Invitation).where(Invitation.token_hash == sha256_hex(token)))
    assert row is not None
    assert row.expires_at.timestamp() == expires_at
    existing = await make_member(identity, container.password_hasher)
    _, _, existing_token = await invited(client, identity, container, email=existing.user.email)
    assert (await details(client, existing_token)).json()["data"]["account_exists"] is True


@pytest.mark.parametrize("token", ["short", "A" * 43, "!" * 43])
async def test_details_of_unknown_tokens(client: httpx.AsyncClient, token: str) -> None:
    response = await details(client, token)
    assert error_of(response) == (400, "TOKEN_EXPIRED", "token")
    assert message_of(response) == INVITATION_INVALID_MESSAGE


async def test_expired_invitations_are_unusable(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, _, token = await invited(client, identity, container)
    container.clock.advance(SEVEN_DAYS_S - 1)
    assert (await details(client, token)).status_code == 200
    container.clock.advance(1)
    assert error_of(await details(client, token)) == (400, "TOKEN_EXPIRED", "token")
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert error_of(response) == (400, "TOKEN_EXPIRED", "token")


async def test_new_user_accepts_and_is_signed_in(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner, email, token = await invited(client, identity, container)
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["org_id"], data["org_name"], data["role"]) == (
        str(owner.org.id),
        owner.org.name,
        "developer",
    )
    assert (data["user"]["email"], data["user"]["name"], data["user"]["email_verified"]) == (
        email,
        "Priya Nair",
        True,
    )
    assert SESSION_COOKIE in client.cookies
    session = (await client.get(SESSION)).json()["data"]
    assert session["active_org_id"] == str(owner.org.id)
    assert [(item["org_id"], item["role"]) for item in session["memberships"]] == [
        (str(owner.org.id), "developer")
    ]
    client.cookies.clear()
    await login(client, email, NEW_PASSWORD)
    assert error_of(await details(client, token)) == (400, "TOKEN_EXPIRED", "token")
    client.cookies.clear()
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert error_of(response) == (400, "TOKEN_EXPIRED", "token")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"password": NEW_PASSWORD}, ("name", NAME_REQUIRED_MESSAGE)),
        ({"name": "Priya Nair"}, ("password", PASSWORD_REQUIRED_MESSAGE)),
    ],
)
async def test_new_users_need_a_name_and_password(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, str],
    expected: tuple[str, str],
) -> None:
    _, _, token = await invited(client, identity, container)
    response = await accept(client, {"token": token, **body})
    field, message = expected
    assert error_of(response) == (400, "INVALID_INPUT", field)
    assert message_of(response) == message
    assert (await details(client, token)).status_code == 200


async def test_weak_password_keeps_the_invitation(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, email, token = await invited(client, identity, container)
    response = await accept(client, {"token": token, "name": "Priya Nair", "password": "short-pw"})
    assert error_of(response) == (400, "PASSWORD_TOO_WEAK", "password")
    assert SESSION_COOKIE not in client.cookies
    assert (await details(client, token)).json()["data"]["account_exists"] is False
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user"]["email"] == email


async def test_invalid_accept_bodies_and_foreign_origin(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, _, token = await invited(client, identity, container)
    response = await accept(client, {"token": token, **NEW_ACCOUNT, "role": "owner"})
    assert error_of(response) == (400, "INVALID_INPUT", "role")
    response = await accept(client, {**NEW_ACCOUNT})
    assert error_of(response) == (400, "INVALID_INPUT", "token")
    response = await accept(client, {"token": token, **NEW_ACCOUNT}, {"Origin": "https://evil.test"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert (await details(client, token)).status_code == 200


async def test_existing_users_must_sign_in(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    existing, owner, token = await invited_existing(client, identity, container)
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert error_of(response) == (401, "AUTH_FAILURE", None)
    assert message_of(response) == SIGN_IN_MESSAGE
    assert (await details(client, token)).status_code == 200
    await login(client, existing.user.email)
    response = await accept_signed_in(client, token)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["org_id"], data["role"], data["user"]["id"]) == (
        str(owner.org.id),
        "developer",
        str(existing.user.id),
    )
    session = (await client.get(SESSION)).json()["data"]
    assert session["active_org_id"] == str(owner.org.id)
    assert {item["org_id"]: item["role"] for item in session["memberships"]} == {
        str(existing.org.id): "owner",
        str(owner.org.id): "developer",
    }
    members = (await client.get("/v1/org/members")).json()["data"]
    assert str(existing.user.id) in {item["user_id"] for item in members}


async def test_signed_in_acceptance_verifies_the_email(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    existing = await make_member(identity, container.password_hasher)
    async with identity.database.global_session() as session:
        await session.execute(update(User).where(User.id == existing.user.id).values(email_verified_at=None))
    _, _, token = await invited(client, identity, container, email=existing.user.email.upper())
    await login(client, existing.user.email)
    response = await accept_signed_in(client, token)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user"]["email_verified"] is True


async def test_signed_in_user_with_another_email_is_refused(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    other = await make_member(identity, container.password_hasher)
    _, _, token = await invited(client, identity, container)
    await login(client, other.user.email)
    response = await accept_signed_in(client, token)
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == OTHER_EMAIL_MESSAGE
    response = await client.post(ACCEPT_INVITATION, json={"token": token}, headers={"Origin": DASHBOARD_URL})
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    assert (await details(client, token)).status_code == 200


async def test_members_cannot_accept_again(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner, email, token = await invited(client, identity, container)
    joined = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert joined.status_code == 200, joined.text
    async with identity.database.system_session() as session:
        await session.execute(
            update(Invitation).where(Invitation.token_hash == sha256_hex(token)).values(accepted_at=None)
        )
    response = await accept_signed_in(client, token)
    assert error_of(response) == (409, "CONFLICT", None)
    assert message_of(response) == ALREADY_MEMBER_MESSAGE
    assert (await details(client, token)).status_code == 200
    assert await membership_count(identity, owner.org.id, email) == 1


async def test_concurrent_acceptance_succeeds_once(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    existing, owner, token = await invited_existing(client, identity, container)
    await login(client, existing.user.email)
    responses = await asyncio.gather(*(accept_signed_in(client, token) for _ in range(3)))
    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(200) == 1, [response.text for response in responses]
    assert all(status in {400, 409} for status in statuses if status != 200)
    assert await membership_count(identity, owner.org.id, existing.user.email) == 1


@pytest.mark.parametrize(
    ("status", "message"), [("suspended", ORG_SUSPENDED_MESSAGE), ("closed", ORG_CLOSED_MESSAGE)]
)
async def test_suspended_and_closed_orgs_refuse(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    status: OrgStatus,
    message: str,
) -> None:
    owner, _, token = await invited(client, identity, container)
    await set_org_status(identity, owner.org.id, status)
    response = await accept(client, {"token": token, **NEW_ACCOUNT})
    assert error_of(response)[:2] == (403, "ORG_SUSPENDED")
    assert message_of(response) == message


async def test_mfa_users_must_pass_mfa_first(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    existing = await signed_in(client, identity, container)
    await enrol_mfa(client, container)
    client.cookies.clear()
    _, _, token = await invited(client, identity, container, email=existing.user.email)
    await login(client, existing.user.email)
    response = await accept_signed_in(client, token)
    assert error_of(response) == (401, "MFA_REQUIRED", None)
    assert message_of(response) == MFA_REQUIRED_MESSAGE
    assert (await details(client, token)).status_code == 200


async def test_inviter_email_names_an_inviter_without_a_name(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    nameless = await add_member(identity, container.password_hasher, owner.org.id, "admin", name="")
    client.cookies.clear()
    await login(client, nameless.email)
    email = unique_email("priya")
    token = await invite(client, container, email)
    assert f"{nameless.email} invited you to join" in container.outbox.to(email)[-1].text
    assert (await details(client, token)).json()["data"]["inviter_name"] == nameless.email


async def test_sso_enforced_domains_must_use_sso(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org = await configure_sso(sso, identity, container, enforce=True)
    await login(sso, org.owner.user.email, PASSWORD)
    email = org.email()
    token = await invite(sso, container, email)
    sso.cookies.clear()
    response = await accept(sso, {"token": token, "name": "Sam Lee", "password": "short-pw"})
    assert error_of(response) == (403, "SSO_REQUIRED", None)
    assert message_of(response) == SSO_REQUIRED_MESSAGE
    sso_url = response.json()["errors"][0]["details"]["sso_url"]
    assert sso_url.startswith(f"{API_BASE_URL}/internal/auth/sso/start?email=")
    assert (await details(sso, token)).status_code == 200
