"""``POST /internal/auth/switch-org`` and child active orgs of sessions (specs/04 §4.1, §4.3, §8.3)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    DASHBOARD_URL,
    SessionContainer,
    enrol_mfa,
    error_of,
    login,
    make_member,
    session_app,
    session_client,
    set_org,
    signed_in,
    stored_org,
    sub_account,
    sub_account_member,
    write_headers,
)
from sqlalchemy import update

from va_api.auth.api_key_auth import ORG_SUSPENDED_MESSAGE
from va_api.auth.session_auth import ORG_CLOSED_MESSAGE
from va_api.auth.sub_accounts import NOT_A_CHILD_MESSAGE
from va_api.routers.auth_switch_org import SWITCH_FORBIDDEN_MESSAGE
from va_db.models import MemberRole, Membership, OrgStatus
from va_db.repositories import MembershipRepo

SWITCH = "/internal/auth/switch-org"
SESSION = "/internal/auth/session"


async def switch(client: httpx.AsyncClient, org_id: uuid.UUID | str) -> httpx.Response:
    return await client.post(SWITCH, json={"org_id": str(org_id)}, headers=write_headers(client))


async def switched(client: httpx.AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await switch(client, org_id)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def session_data(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(SESSION)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_switching_to_another_membership(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    other = await make_member(identity, container.password_hasher)
    async with identity.database.tenant_session(other.org.id) as session:
        await MembershipRepo(session, other.org.id).add(member.user.id, "developer")
    data = await switched(client, other.org.id)
    assert (data["active_org_id"], data["acting_parent_org_id"]) == (str(other.org.id), None)
    assert {item["org_id"] for item in data["memberships"]} == {str(member.org.id), str(other.org.id)}
    assert data["csrf_token"]
    me = (await client.get("/v1/me")).json()["data"]
    assert (me["org"]["id"], me["role"], me["acting_parent_org_id"]) == (str(other.org.id), "developer", None)
    back = await switched(client, member.org.id)
    assert back["active_org_id"] == str(member.org.id)


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_or_admin_switches_into_a_child(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    async with sub_account_member(client, identity, container, role) as (other, owner, child_id):
        data = await switched(other, child_id)
        assert (data["active_org_id"], data["acting_parent_org_id"]) == (str(child_id), str(owner.org.id))
        assert [item["org_id"] for item in data["memberships"]] == [str(owner.org.id)]
        me = (await other.get("/v1/me")).json()["data"]
        assert (me["org"]["id"], me["role"]) == (str(child_id), role)
        assert me["acting_parent_org_id"] == str(owner.org.id)
        renamed = await other.patch("/v1/org", json={"name": "Austin Clinic"}, headers=write_headers(other))
        assert renamed.status_code == 200, renamed.text
        assert (await session_data(other))["active_org_id"] == str(child_id)
    assert (await stored_org(identity, child_id)).name == "Austin Clinic"
    assert (await stored_org(identity, owner.org.id)).name == "Acme Dental"


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_other_parent_roles_cannot_switch_into_a_child(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    async with sub_account_member(client, identity, container, role) as (other, owner, child_id):
        response = await switch(other, child_id)
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == SWITCH_FORBIDDEN_MESSAGE
        assert (await session_data(other))["active_org_id"] == str(owner.org.id)


async def test_foreign_and_unknown_orgs_are_refused(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    stranger = await make_member(identity, container.password_hasher)
    async with session_client(session_app(container)) as other:
        await login(other, stranger.user.email)
        stranger_child, _ = await sub_account(other)
    for org_id in (stranger.org.id, stranger_child, uuid.uuid4()):
        response = await switch(client, org_id)
        assert error_of(response) == (403, "FORBIDDEN", None)
    assert (await session_data(client))["active_org_id"] == str(member.org.id)


async def test_switch_org_validates_the_request(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    no_csrf = await client.post(
        SWITCH, json={"org_id": str(member.org.id)}, headers={"Origin": DASHBOARD_URL}
    )
    assert error_of(no_csrf)[:2] == (403, "FORBIDDEN")
    extra = await client.post(
        SWITCH, json={"org_id": str(member.org.id), "role": "owner"}, headers=write_headers(client)
    )
    assert error_of(extra)[:2] == (400, "INVALID_INPUT")
    malformed = await client.post(SWITCH, json={"org_id": "acme"}, headers=write_headers(client))
    assert error_of(malformed) == (400, "INVALID_INPUT", "org_id")
    async with session_client(session_app(container)) as anonymous:
        unauthenticated = await anonymous.post(SWITCH, json={"org_id": str(member.org.id)})
    assert error_of(unauthenticated)[:2] == (401, "AUTH_FAILURE")


async def test_a_switched_session_cannot_hop_with_the_header(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    first_id, _ = await sub_account(client, "Austin")
    second_id, _ = await sub_account(client, "Dallas")
    await switched(client, first_id)
    for org_id in (second_id, member.org.id):
        response = await client.get("/v1/org", headers={"X-VA-Org-Id": str(org_id)})
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == NOT_A_CHILD_MESSAGE
    same = await client.get("/v1/org", headers={"X-VA-Org-Id": str(first_id)})
    assert same.json()["data"]["id"] == str(first_id)
    sibling = await switched(client, second_id)
    assert sibling["active_org_id"] == str(second_id)


async def test_a_lowered_parent_role_falls_back_to_the_oldest_membership(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    await switched(client, child_id)
    async with identity.database.system_session() as session:
        await session.execute(
            update(Membership)
            .where(Membership.org_id == member.org.id, Membership.user_id == member.user.id)
            .values(role="developer")
        )
    data = await session_data(client)
    assert (data["active_org_id"], data["acting_parent_org_id"]) == (str(member.org.id), None)
    me = (await client.get("/v1/me")).json()["data"]
    assert (me["org"]["id"], me["role"]) == (str(member.org.id), "developer")


async def test_a_child_requiring_mfa_gates_the_switched_session(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    await set_org(identity, child_id, settings={"mfa_required": True})
    data = await switched(client, child_id)
    assert (data["mfa_required"], data["mfa_passed"]) == (True, False)
    gated = await client.get("/v1/org")
    assert error_of(gated)[:2] == (401, "MFA_REQUIRED")
    blocked = await switch(client, child_id)
    assert error_of(blocked)[:2] == (401, "MFA_REQUIRED")
    await enrol_mfa(client, container)
    assert (await client.get("/v1/org")).json()["data"]["id"] == str(child_id)


def refusal(response: httpx.Response) -> tuple[int, str, str]:
    status, code, _ = error_of(response)
    message: str = response.json()["errors"][0]["message"]
    return status, code, message


async def switched_into_child_of(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, parent_status: OrgStatus
) -> tuple[uuid.UUID, uuid.UUID]:
    """Sign in an owner, switch into a new child, then give the parent ``parent_status``."""
    owner = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    await switched(client, child_id)
    await set_org(identity, owner.org.id, status=parent_status)
    return owner.org.id, child_id


async def test_a_suspended_parent_keeps_a_switched_session_read_only(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    parent_id, child_id = await switched_into_child_of(client, identity, container, "suspended")
    assert (await client.get("/v1/org")).json()["data"]["id"] == str(child_id)
    renamed = await client.patch("/v1/org", json={"name": "Austin Clinic"}, headers=write_headers(client))
    assert refusal(renamed) == (403, "ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)
    assert (await stored_org(identity, child_id)).name == "Acme Dental - Austin"
    assert (await switched(client, child_id))["acting_parent_org_id"] == str(parent_id)


async def test_a_closed_parent_closes_its_children_to_a_switched_session(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, child_id = await switched_into_child_of(client, identity, container, "closed")
    assert refusal(await client.get("/v1/org")) == (403, "ORG_SUSPENDED", ORG_CLOSED_MESSAGE)
    assert refusal(await switch(client, child_id)) == (403, "ORG_SUSPENDED", ORG_CLOSED_MESSAGE)
