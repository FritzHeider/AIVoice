"""``/v1/org/members/invitations`` against a real database (specs/04 §3.2, §6)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from identity_db import IdentityDb, unique_email
from session_helpers import (
    DASHBOARD_URL,
    INVITATIONS,
    SessionContainer,
    add_member,
    error_of,
    invite,
    link_token,
    login,
    make_member,
    signed_in,
    store_api_key,
    write_headers,
)
from sqlalchemy import select, update

from va_api.auth.invitations import INVITATION_INVALID_MESSAGE
from va_api.routers.org_invitations import (
    ALREADY_MEMBER_EMAIL_MESSAGE,
    INVITATION_NOT_FOUND_MESSAGE,
    NO_KEY_CREATOR_MESSAGE,
)
from va_api.routers.org_invitations_schemas import INVALID_EMAIL_MESSAGE
from va_common.auth import sha256_hex
from va_db.models import ApiKey, Invitation, MemberRole

OWNER_TRANSFER_MESSAGE = "Missing required permission 'org.owner_transfer'."
MANAGE_MESSAGE = "Missing required permission 'members.manage'."
VIEW_KEYS = {"id", "email", "role", "invited_by", "status", "expires_at", "created_at"}
SEVEN_DAYS_S = 7 * 24 * 3600


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


def revoke_path(invitation_id: uuid.UUID | str) -> str:
    return f"{INVITATIONS}/{invitation_id}"


async def create(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post(INVITATIONS, json=body, headers=write_headers(client))


async def revoke(client: httpx.AsyncClient, invitation_id: uuid.UUID | str) -> httpx.Response:
    return await client.delete(revoke_path(invitation_id), headers=write_headers(client))


async def details(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.get(f"/internal/auth/invitations/{token}")


async def listed(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(INVITATIONS)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["data"]
    return items


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owners_and_admins_invite(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    email = unique_email("priya")
    response = await create(client, {"email": f"  {email} ", "role": "developer"})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert set(data) == VIEW_KEYS
    assert (data["email"], data["role"], data["status"]) == (email, "developer", "pending")
    assert data["invited_by"] == str(member.user.id)
    token = link_token(container.outbox, email, "invitation")
    message = container.outbox.to(email)[-1]
    assert message.subject == "You have been invited to VoiceAgents"
    assert f"Jane Park invited you to join {member.org.name}" in message.text
    assert f"{DASHBOARD_URL}/invite/{token}" in message.text
    async with identity.database.system_session() as session:
        row = await session.scalar(select(Invitation).where(Invitation.id == uuid.UUID(data["id"])))
    assert row is not None
    assert row.token_hash == sha256_hex(token)
    assert (row.expires_at - row.created_at).total_seconds() == SEVEN_DAYS_S
    assert token not in response.text


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_other_roles_cannot_invite_list_or_revoke(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    await signed_in(client, identity, container, role=role)
    response = await create(client, {"email": unique_email(), "role": "viewer"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == MANAGE_MESSAGE
    assert error_of(await client.get(INVITATIONS))[:2] == (403, "FORBIDDEN")
    assert error_of(await revoke(client, uuid.uuid4()))[:2] == (403, "FORBIDDEN")


async def test_only_owners_invite_owners(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    admin = await signed_in(client, identity, container, role="admin")
    response = await create(client, {"email": unique_email(), "role": "owner"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == OWNER_TRANSFER_MESSAGE
    owner = await add_member(identity, container.password_hasher, admin.org.id, "owner")
    client.cookies.clear()
    await login(client, owner.email)
    response = await create(client, {"email": unique_email(), "role": "owner"})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["role"] == "owner"


async def test_members_cannot_be_invited_again(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    viewer = await add_member(identity, container.password_hasher, owner.org.id, "viewer")
    stranger = await make_member(identity, container.password_hasher)
    response = await create(client, {"email": viewer.email.upper(), "role": "admin"})
    assert error_of(response) == (409, "CONFLICT", "email")
    assert message_of(response) == ALREADY_MEMBER_EMAIL_MESSAGE
    response = await create(client, {"email": stranger.user.email, "role": "admin"})
    assert response.status_code == 201, response.text


async def test_reinviting_replaces_the_open_invitation(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    email = unique_email("priya")
    first = await invite(client, container, email, "viewer")
    container.clock.advance(5)
    second = await invite(client, container, email.upper(), "admin")
    assert first != second
    response = await details(client, first)
    assert error_of(response) == (400, "TOKEN_EXPIRED", "token")
    assert message_of(response) == INVITATION_INVALID_MESSAGE
    assert (await details(client, second)).status_code == 200
    items = await listed(client)
    assert [(item["email"].casefold(), item["role"]) for item in items] == [(email.casefold(), "admin")]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"email": "no-at-sign.test", "role": "viewer"}, "email"),
        ({"email": "two@at@signs.test", "role": "viewer"}, "email"),
        ({"email": "@acme.test", "role": "viewer"}, "email"),
        ({"email": "jane@", "role": "viewer"}, "email"),
        ({"email": "a@", "role": "viewer"}, "email"),
        ({"email": "jane@acme.test", "role": "superuser"}, "role"),
        ({"email": "jane@acme.test"}, "role"),
        ({"email": "jane@acme.test", "role": "viewer", "name": "Jane"}, "name"),
    ],
)
async def test_invalid_bodies(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    field: str,
) -> None:
    await signed_in(client, identity, container)
    response = await create(client, body)
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_invalid_email_message(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    response = await create(client, {"email": "two@at@signs.test", "role": "viewer"})
    assert INVALID_EMAIL_MESSAGE in message_of(response)


async def test_wildcard_api_keys_invite_as_their_creator(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await make_member(identity, container.password_hasher)
    key, row = await store_api_key(container, owner.org.id)
    orphan, _ = await store_api_key(container, owner.org.id)
    reader, _ = await store_api_key(container, owner.org.id, ("org:read",))
    writer, _ = await store_api_key(container, owner.org.id, ("org:write",))
    async with identity.database.system_session() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(created_by=owner.user.id))
    body = {"email": unique_email("priya"), "role": "developer"}
    response = await client.post(INVITATIONS, json=body, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["invited_by"] == str(owner.user.id)
    response = await client.post(INVITATIONS, json=body, headers={"Authorization": f"Bearer {orphan}"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == NO_KEY_CREATOR_MESSAGE
    response = await client.post(INVITATIONS, json=body, headers={"Authorization": f"Bearer {reader}"})
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    response = await client.post(INVITATIONS, json=body, headers={"Authorization": f"Bearer {writer}"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == MANAGE_MESSAGE
    owner_body = {"email": unique_email("boss"), "role": "owner"}
    response = await client.post(INVITATIONS, json=owner_body, headers={"Authorization": f"Bearer {key}"})
    assert message_of(response) == OWNER_TRANSFER_MESSAGE
    response = await client.get(INVITATIONS, headers={"Authorization": f"Bearer {key}"})
    assert [item["email"] for item in response.json()["data"]] == [body["email"]]


async def test_list_marks_expired_and_hides_accepted(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    old_email, new_email, taken_email = unique_email("old"), unique_email("new"), unique_email("taken")
    await invite(client, container, old_email)
    container.clock.advance(SEVEN_DAYS_S - 60)
    await invite(client, container, new_email, "analyst")
    taken = await invite(client, container, taken_email)
    async with identity.database.system_session() as session:
        await session.execute(
            update(Invitation)
            .where(Invitation.token_hash == sha256_hex(taken))
            .values(accepted_at=container.clock.now())
        )
    container.clock.advance(60)
    items = await listed(client)
    assert [(item["email"], item["status"]) for item in items] == [
        (new_email, "pending"),
        (old_email, "expired"),
    ]
    other = await make_member(identity, container.password_hasher)
    client.cookies.clear()
    await login(client, other.user.email)
    assert await listed(client) == []
    assert owner.org.id != other.org.id


async def test_revoke(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> None:
    owner = await signed_in(client, identity, container)
    email = unique_email("priya")
    token = await invite(client, container, email)
    (item,) = await listed(client)
    response = await revoke(client, item["id"])
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"id": item["id"], "revoked": True}
    assert error_of(await details(client, token)) == (400, "TOKEN_EXPIRED", "token")
    assert await listed(client) == []
    response = await revoke(client, item["id"])
    assert error_of(response) == (404, "NOT_FOUND", None)
    assert message_of(response) == INVITATION_NOT_FOUND_MESSAGE
    assert error_of(await revoke(client, "not-a-uuid"))[:2] == (400, "INVALID_INPUT")
    other = await make_member(identity, container.password_hasher)
    client.cookies.clear()
    await login(client, other.user.email)
    foreign_token = await invite(client, container, unique_email("foreign"))
    (foreign,) = await listed(client)
    client.cookies.clear()
    await login(client, owner.user.email)
    assert error_of(await revoke(client, foreign["id"])) == (404, "NOT_FOUND", None)
    assert (await details(client, foreign_token)).status_code == 200


async def test_admins_cannot_revoke_owner_invitations(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    await invite(client, container, unique_email("boss"), "owner")
    await invite(client, container, unique_email("dev"), "developer")
    admin = await add_member(identity, container.password_hasher, owner.org.id, "admin")
    client.cookies.clear()
    await login(client, admin.email)
    items = {item["role"]: item["id"] for item in await listed(client)}
    response = await revoke(client, items["owner"])
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == OWNER_TRANSFER_MESSAGE
    assert (await revoke(client, items["developer"])).status_code == 200


async def test_accepted_invitations_cannot_be_revoked(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    token = await invite(client, container, unique_email("priya"))
    (item,) = await listed(client)
    async with identity.database.system_session() as session:
        await session.execute(
            update(Invitation)
            .where(Invitation.token_hash == sha256_hex(token))
            .values(accepted_at=container.clock.now())
        )
    assert error_of(await revoke(client, item["id"])) == (404, "NOT_FOUND", None)


async def test_writes_need_csrf(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    body = {"email": unique_email(), "role": "viewer"}
    response = await client.post(INVITATIONS, json=body, headers={"Origin": DASHBOARD_URL})
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    response = await client.delete(revoke_path(uuid.uuid4()), headers={"Origin": DASHBOARD_URL})
    assert error_of(response)[:2] == (403, "FORBIDDEN")
