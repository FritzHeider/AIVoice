"""``/v1/org/members`` and ``/v1/org/transfer-ownership`` against a real database (specs/04 §3.2, §6)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, get_args

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    Member,
    SessionContainer,
    add_member,
    error_of,
    login,
    make_member,
    session_app,
    session_client,
    signed_in,
    store_api_key,
    write_headers,
)
from sqlalchemy import select

from va_api.auth.password_reset import REAUTH_WINDOW
from va_api.routers.org_members import (
    MEMBER_NOT_FOUND_MESSAGE,
    SELF_TRANSFER_MESSAGE,
    TRANSFER_REAUTH_MESSAGE,
)
from va_db.models import MemberRole, Membership, User
from va_db.repositories import LAST_OWNER_MESSAGE, MembershipRepo

MEMBERS = "/v1/org/members"
TRANSFER = "/v1/org/transfer-ownership"
ME = "/v1/me"
ROLES = get_args(MemberRole)
OWNER_TRANSFER_MESSAGE = "Missing required permission 'org.owner_transfer'."
MEMBER_KEYS = {"user_id", "email", "name", "role", "mfa_enabled", "last_login_at", "joined_at"}


def member_path(user_id: uuid.UUID) -> str:
    return f"{MEMBERS}/{user_id}"


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


async def patch_role(client: httpx.AsyncClient, user_id: uuid.UUID, body: dict[str, Any]) -> httpx.Response:
    return await client.patch(member_path(user_id), json=body, headers=write_headers(client))


async def remove(client: httpx.AsyncClient, user_id: uuid.UUID) -> httpx.Response:
    return await client.delete(member_path(user_id), headers=write_headers(client))


async def transfer(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post(TRANSFER, json=body, headers=write_headers(client))


async def roles_of(identity: IdentityDb, org_id: uuid.UUID) -> dict[uuid.UUID, MemberRole]:
    async with identity.database.tenant_session(org_id) as session:
        return {
            record.user_id: record.role for record in await MembershipRepo(session, org_id).list_members()
        }


async def with_viewer(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> tuple[Member, User]:
    """A signed-in member with ``role`` and a second member, a viewer, of the same org."""
    member = await signed_in(client, identity, container, role=role)
    return member, await add_member(identity, container.password_hasher, member.org.id, "viewer")


async def with_stranger(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> tuple[Member, list[uuid.UUID]]:
    """A signed-in owner, and the ids of a member of another org and of nobody."""
    owner = await signed_in(client, identity, container)
    stranger = await make_member(identity, container.password_hasher, role="developer")
    return owner, [stranger.user.id, uuid.uuid4()]


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_lists_the_members(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member, other = await with_viewer(client, identity, container, role)
    response = await client.get(MEMBERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["user_id"] for item in data] == [str(member.user.id), str(other.id)]
    assert all(set(item) == MEMBER_KEYS for item in data)
    caller, listed = data
    assert (caller["email"], caller["name"], caller["role"]) == (member.user.email, "Jane Park", role)
    assert caller["last_login_at"] is not None
    assert (listed["email"], listed["name"], listed["role"]) == (other.email, "Omar Haddad", "viewer")
    assert (listed["mfa_enabled"], listed["last_login_at"]) == (False, None)


async def test_api_keys_list_members_with_org_read_only(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    wildcard, _ = await store_api_key(container, member.org.id)
    narrow, _ = await store_api_key(container, member.org.id, ("calls:read",))
    listed = await client.get(MEMBERS, headers={"Authorization": f"Bearer {wildcard}"})
    assert listed.status_code == 200, listed.text
    assert [item["user_id"] for item in listed.json()["data"]] == [str(member.user.id)]
    assert error_of(await client.get(MEMBERS, headers={"Authorization": narrow}))[:2] == (403, "FORBIDDEN")


async def test_owner_promotes_and_admin_changes_non_owner_roles(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    developer = await add_member(identity, container.password_hasher, owner.org.id, "developer")
    promoted = await patch_role(client, developer.id, {"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["data"]["role"] == "admin"
    assert set(promoted.json()["data"]) == MEMBER_KEYS
    analyst = await add_member(identity, container.password_hasher, owner.org.id, "analyst")
    async with session_client(session_app(container)) as admin_client:
        await login(admin_client, developer.email)
        changed = await patch_role(admin_client, analyst.id, {"role": "viewer"})
        assert changed.status_code == 200, changed.text
        same = await patch_role(admin_client, analyst.id, {"role": "viewer"})
        assert same.json()["data"] == changed.json()["data"]
    assert await roles_of(identity, owner.org.id) == {
        owner.user.id: "owner",
        developer.id: "admin",
        analyst.id: "viewer",
    }


async def test_admins_cannot_touch_owners(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    admin = await signed_in(client, identity, container, role="admin")
    owner = await add_member(identity, container.password_hasher, admin.org.id, "owner")
    developer = await add_member(identity, container.password_hasher, admin.org.id, "developer")
    for response in (
        await patch_role(client, developer.id, {"role": "owner"}),
        await patch_role(client, owner.id, {"role": "viewer"}),
        await remove(client, owner.id),
    ):
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert message_of(response) == OWNER_TRANSFER_MESSAGE
    assert await roles_of(identity, admin.org.id) == {
        admin.user.id: "admin",
        owner.id: "owner",
        developer.id: "developer",
    }


async def test_owner_promotes_an_owner_and_the_last_owner_stays(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    sole = await patch_role(client, owner.user.id, {"role": "admin"})
    assert error_of(sole) == (409, "CONFLICT", None)
    assert message_of(sole) == LAST_OWNER_MESSAGE
    assert error_of(await remove(client, owner.user.id))[:2] == (409, "CONFLICT")
    admin = await add_member(identity, container.password_hasher, owner.org.id, "admin")
    promoted = await patch_role(client, admin.id, {"role": "owner"})
    assert promoted.status_code == 200, promoted.text
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner", admin.id: "owner"}
    removed = await remove(client, admin.id)
    assert removed.status_code == 200, removed.text
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner"}


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_roles_without_members_manage_cannot_change_members(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member, other = await with_viewer(client, identity, container, role)
    assert error_of(await patch_role(client, other.id, {"role": "analyst"}))[:2] == (403, "FORBIDDEN")
    assert error_of(await remove(client, other.id))[:2] == (403, "FORBIDDEN")
    assert await roles_of(identity, member.org.id) == {member.user.id: role, other.id: "viewer"}


async def test_wildcard_api_keys_manage_members_but_not_owners(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    developer = await add_member(identity, container.password_hasher, member.org.id, "developer")
    key, _ = await store_api_key(container, member.org.id)
    auth = {"Authorization": f"Bearer {key}"}
    changed = await client.patch(member_path(developer.id), json={"role": "analyst"}, headers=auth)
    assert changed.status_code == 200, changed.text
    to_owner = await client.patch(member_path(developer.id), json={"role": "owner"}, headers=auth)
    assert error_of(to_owner) == (403, "FORBIDDEN", None)
    assert error_of(await client.delete(member_path(member.user.id), headers=auth))[:2] == (403, "FORBIDDEN")
    narrow, _ = await store_api_key(container, member.org.id, ("org:write",))
    refused = await client.patch(
        member_path(developer.id), json={"role": "viewer"}, headers={"Authorization": narrow}
    )
    assert error_of(refused)[:2] == (403, "FORBIDDEN")
    assert await roles_of(identity, member.org.id) == {member.user.id: "owner", developer.id: "analyst"}


async def test_members_of_other_orgs_and_unknown_ids_are_not_found(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner, missing_ids = await with_stranger(client, identity, container)
    for user_id in missing_ids:
        for response in (
            await patch_role(client, user_id, {"role": "viewer"}),
            await remove(client, user_id),
        ):
            assert error_of(response) == (404, "NOT_FOUND", None)
            assert message_of(response) == MEMBER_NOT_FOUND_MESSAGE
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner"}
    async with identity.database.system_session() as session:
        stranger_roles = (
            await session.scalars(select(Membership.role).where(Membership.user_id == missing_ids[0]))
        ).all()
    assert stranger_roles == ["developer"]


async def test_invalid_bodies_ids_and_missing_csrf(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    developer = await add_member(identity, container.password_hasher, owner.org.id, "developer")
    assert error_of(await patch_role(client, developer.id, {"role": "superuser"})) == (
        400,
        "INVALID_INPUT",
        "role",
    )
    extra = await patch_role(client, developer.id, {"role": "viewer", "note": "x"})
    assert error_of(extra)[:2] == (400, "INVALID_INPUT")
    not_uuid = await client.patch(
        f"{MEMBERS}/not-a-uuid", json={"role": "viewer"}, headers=write_headers(client)
    )
    assert error_of(not_uuid)[:2] == (400, "INVALID_INPUT")
    no_csrf = await client.patch(member_path(developer.id), json={"role": "viewer"})
    assert error_of(no_csrf)[:2] == (403, "FORBIDDEN")
    assert error_of(await client.delete(member_path(developer.id)))[:2] == (403, "FORBIDDEN")
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner", developer.id: "developer"}


async def test_removed_member_loses_the_org_on_the_next_request(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    admin = await signed_in(client, identity, container, role="admin")
    developer = await add_member(identity, container.password_hasher, admin.org.id, "developer")
    elsewhere = await add_member(identity, container.password_hasher, admin.org.id, "analyst")
    second_org = await make_member(identity, container.password_hasher)
    async with identity.database.tenant_session(second_org.org.id) as session:
        await MembershipRepo(session, second_org.org.id).add(elsewhere.id, "viewer")
    async with (
        session_client(session_app(container)) as developer_client,
        session_client(session_app(container)) as elsewhere_client,
    ):
        await login(developer_client, developer.email)
        await login(elsewhere_client, elsewhere.email)
        assert (await developer_client.get(ME)).json()["data"]["org"]["id"] == str(admin.org.id)
        assert (await elsewhere_client.get(ME)).json()["data"]["org"]["id"] == str(admin.org.id)
        removed = await remove(client, developer.id)
        assert removed.status_code == 200, removed.text
        assert removed.json()["data"] == {"user_id": str(developer.id), "removed": True}
        assert (await remove(client, elsewhere.id)).status_code == 200
        gone = await developer_client.get(ME)
        assert error_of(gone) == (403, "FORBIDDEN", None)
        assert message_of(gone) == "You are not a member of any organization."
        moved = await elsewhere_client.get(ME)
        assert moved.status_code == 200, moved.text
        assert (moved.json()["data"]["org"]["id"], moved.json()["data"]["role"]) == (
            str(second_org.org.id),
            "viewer",
        )
    assert await roles_of(identity, admin.org.id) == {admin.user.id: "admin"}


async def test_owner_transfers_ownership_after_a_recent_sign_in(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    developer = await add_member(identity, container.password_hasher, owner.org.id, "developer")
    response = await transfer(client, {"user_id": str(developer.id)})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["previous_owner"]["user_id"], data["previous_owner"]["role"]) == (
        str(owner.user.id),
        "admin",
    )
    assert (data["owner"]["user_id"], data["owner"]["role"]) == (str(developer.id), "owner")
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "admin", developer.id: "owner"}
    me = (await client.get(ME)).json()["data"]
    assert me["role"] == "admin"
    assert error_of(await transfer(client, {"user_id": str(developer.id)}))[:2] == (403, "FORBIDDEN")


async def test_transfer_needs_a_sign_in_in_the_last_ten_minutes(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    developer = await add_member(identity, container.password_hasher, owner.org.id, "developer")
    container.clock.advance(REAUTH_WINDOW + timedelta(seconds=1))
    stale = await transfer(client, {"user_id": str(developer.id)})
    assert error_of(stale) == (401, "REAUTH_REQUIRED", None)
    assert message_of(stale) == TRANSFER_REAUTH_MESSAGE
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner", developer.id: "developer"}


async def test_transfer_refuses_admins_api_keys_and_bad_targets(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner, missing_ids = await with_stranger(client, identity, container)
    for user_id in missing_ids:
        missing = await transfer(client, {"user_id": str(user_id)})
        assert error_of(missing) == (404, "NOT_FOUND", None)
        assert message_of(missing) == MEMBER_NOT_FOUND_MESSAGE
    itself = await transfer(client, {"user_id": str(owner.user.id)})
    assert error_of(itself) == (400, "INVALID_INPUT", "user_id")
    assert message_of(itself) == SELF_TRANSFER_MESSAGE
    extra = await transfer(client, {"user_id": str(owner.user.id), "keep_owner": True})
    assert error_of(extra)[:2] == (400, "INVALID_INPUT")
    admin = await add_member(identity, container.password_hasher, owner.org.id, "admin")
    key, _ = await store_api_key(container, owner.org.id)
    by_key = await client.post(
        TRANSFER, json={"user_id": str(admin.id)}, headers={"Authorization": f"Bearer {key}"}
    )
    assert error_of(by_key) == (403, "FORBIDDEN", None)
    assert message_of(by_key) == OWNER_TRANSFER_MESSAGE
    async with session_client(session_app(container)) as admin_client:
        await login(admin_client, admin.email)
        refused = await transfer(admin_client, {"user_id": str(admin.id)})
    assert error_of(refused)[:2] == (403, "FORBIDDEN")
    assert await roles_of(identity, owner.org.id) == {owner.user.id: "owner", admin.id: "admin"}
