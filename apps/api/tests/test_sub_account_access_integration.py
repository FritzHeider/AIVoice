"""Parent principals acting on sub-accounts with ``X-VA-Org-Id`` against a real database (specs/04 §8.3)."""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

import httpx
import pytest
import structlog
from identity_db import IdentityDb
from session_helpers import (
    INVITATIONS,
    Member,
    SessionContainer,
    enrol_mfa,
    error_of,
    make_member,
    session_app,
    session_client,
    set_org,
    signed_in,
    store_api_key,
    stored_org,
    sub_account,
    sub_account_member,
    write_headers,
)
from sqlalchemy import select, update

from va_api.auth.session_auth import MFA_REQUIRED_MESSAGE, ORG_CLOSED_MESSAGE
from va_api.auth.sub_accounts import (
    ACT_FORBIDDEN_MESSAGE,
    INVALID_ORG_HEADER_MESSAGE,
    NOT_A_CHILD_MESSAGE,
)
from va_api.rate_limit import LIMIT_HEADER
from va_api.routers.org_members import PARENT_TRANSFER_MESSAGE
from va_api.routers.orgs import NESTED_SUB_ACCOUNT_MESSAGE
from va_db.models import ApiKey, Invitation, MemberRole

ME = "/v1/me"
ORG = "/v1/org"
HEADER = "X-VA-Org-Id"


def acting(key: str, org_id: uuid.UUID | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", HEADER: str(org_id)}


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


class Family(NamedTuple):
    member: Member
    child_id: uuid.UUID
    key: str
    key_row: ApiKey


async def family(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    scopes: tuple[str, ...] = ("*",),
) -> Family:
    """A signed-in owner, a sub-account created by them, and a parent-org API key with ``scopes``."""
    member = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    key, row = await store_api_key(container, member.org.id, scopes)
    return Family(member=member, child_id=child_id, key=key, key_row=row)


async def me(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(ME, headers=headers)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_a_star_key_acts_on_its_child(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, child_id, key, row = await family(client, identity, container)
    data = await me(client, acting(key, child_id))
    assert data["org"]["id"] == str(child_id)
    assert data["acting_parent_org_id"] == str(member.org.id)
    assert data["api_key"]["id"] == str(row.id)
    assert data["scopes"] == ["*"]
    org = await client.get(ORG, headers=acting(key, child_id))
    assert org.json()["data"]["id"] == str(child_id)
    assert org.json()["data"]["parent_org_id"] == str(member.org.id)
    renamed = await client.patch(ORG, json={"name": "Austin Clinic"}, headers=acting(key, child_id))
    assert renamed.status_code == 200, renamed.text
    assert (await stored_org(identity, child_id)).name == "Austin Clinic"
    assert (await stored_org(identity, member.org.id)).name == "Acme Dental"
    plain = await me(client, {"Authorization": f"Bearer {key}"})
    assert (plain["org"]["id"], plain["acting_parent_org_id"]) == (str(member.org.id), None)


async def test_acting_binds_both_orgs_to_the_logs(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, child_id, key, _ = await family(client, identity, container)
    bound: dict[str, object] = {}
    original = structlog.contextvars.bind_contextvars

    def record(**values: object) -> object:
        bound.update(values)
        return original(**values)

    monkeypatch.setattr(structlog.contextvars, "bind_contextvars", record)
    await me(client, acting(key, child_id))
    assert (bound["org_id"], bound["acting_parent_org_id"]) == (str(child_id), str(member.org.id))


@pytest.mark.parametrize(
    ("scopes", "allowed"),
    [
        (("org:write",), True),
        (("org:read", "org:write"), True),
        (("org:read",), False),
        (("agents:read",), False),
    ],
)
async def test_api_keys_need_org_write(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    scopes: tuple[str, ...],
    allowed: bool,
) -> None:
    _, child_id, key, _ = await family(client, identity, container, scopes)
    response = await client.patch(ORG, json={"name": "Austin Clinic"}, headers=acting(key, child_id))
    if allowed:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["id"] == str(child_id)
    else:
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert message_of(response) == ACT_FORBIDDEN_MESSAGE
        assert (await stored_org(identity, child_id)).name == "Acme Dental - Austin"


async def test_the_header_naming_the_own_org_or_blank_changes_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    key, _ = await store_api_key(container, member.org.id, ("org:read",))
    for value in (str(member.org.id), "", "   "):
        data = await me(client, acting(key, value))
        assert (data["org"]["id"], data["acting_parent_org_id"]) == (str(member.org.id), None)


@pytest.mark.parametrize("value", ["acme", "12345", "0192f3c4-5a6b-7c8d-9e0f"])
async def test_a_malformed_header_is_invalid_input(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, value: str
) -> None:
    member = await signed_in(client, identity, container)
    key, _ = await store_api_key(container, member.org.id)
    response = await client.get(ME, headers=acting(key, value))
    assert error_of(response) == (400, "INVALID_INPUT", HEADER)
    assert message_of(response) == INVALID_ORG_HEADER_MESSAGE


async def test_only_direct_children_of_the_caller_can_be_acted_on(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    first_id, first_key = await sub_account(client, "Austin")
    second_id, _ = await sub_account(client, "Dallas")
    stranger = await make_member(identity, container.password_hasher)
    async with session_client(session_app(container)) as other:
        stranger_child, _ = await sub_account(
            other, **{"Authorization": (await store_api_key(container, stranger.org.id))[0]}
        )
    parent_key, _ = await store_api_key(container, member.org.id)
    denied = [
        acting(first_key, second_id),
        acting(first_key, member.org.id),
        acting(parent_key, stranger.org.id),
        acting(parent_key, stranger_child),
        acting(parent_key, uuid.uuid4()),
    ]
    for headers in denied:
        response = await client.get(ORG, headers=headers)
        assert error_of(response) == (403, "FORBIDDEN", None), headers
        assert message_of(response) == NOT_A_CHILD_MESSAGE
    assert (await me(client, acting(parent_key, first_id)))["org"]["id"] == str(first_id)


async def test_a_suspended_child_reads_and_a_closed_child_refuses_everything(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, child_id, key, _ = await family(client, identity, container)
    suspended = await client.post(f"/v1/orgs/{child_id}/suspend", headers=write_headers(client))
    assert suspended.status_code == 200, suspended.text
    read = await client.get(ORG, headers=acting(key, child_id))
    assert read.json()["data"]["status"] == "suspended"
    write = await client.patch(ORG, json={"name": "Austin Clinic"}, headers=acting(key, child_id))
    assert error_of(write)[:2] == (403, "ORG_SUSPENDED")
    await set_org(identity, child_id, status="closed")
    closed = await client.get(ORG, headers=acting(key, child_id))
    assert error_of(closed)[:2] == (403, "ORG_SUSPENDED")
    assert message_of(closed) == ORG_CLOSED_MESSAGE


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("owner", True),
        ("admin", True),
        ("developer", False),
        ("analyst", False),
        ("billing", False),
        ("viewer", False),
    ],
)
async def test_session_principals_need_the_owner_or_admin_role(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    role: MemberRole,
    allowed: bool,
) -> None:
    async with sub_account_member(client, identity, container, role) as (other, owner, child_id):
        response = await other.get(ME, headers={HEADER: str(child_id)})
    if allowed:
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert (data["org"]["id"], data["role"]) == (str(child_id), role)
        assert data["acting_parent_org_id"] == str(owner.org.id)
    else:
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert message_of(response) == ACT_FORBIDDEN_MESSAGE


async def test_a_child_requiring_mfa_gates_parent_sessions_until_mfa_passes(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    child_id, child_key = await sub_account(client)
    await set_org(identity, child_id, settings={"mfa_required": True})
    gated = await client.get(ORG, headers={HEADER: str(child_id)})
    assert error_of(gated)[:2] == (401, "MFA_REQUIRED")
    assert message_of(gated) == MFA_REQUIRED_MESSAGE
    await enrol_mfa(client, container)
    passed = await client.get(ORG, headers={HEADER: str(child_id)})
    assert passed.json()["data"]["id"] == str(child_id)
    own_key = await client.get(ORG, headers={"Authorization": f"Bearer {child_key}"})
    assert own_key.status_code == 200, own_key.text


async def test_acting_cannot_nest_sub_accounts_or_list_grandchildren(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    _, child_id, key, _ = await family(client, identity, container)
    nested = await client.post(
        "/v1/orgs/create", json={"org_display_name": "Nested"}, headers=acting(key, child_id)
    )
    assert error_of(nested)[:2] == (403, "FORBIDDEN")
    assert message_of(nested) == NESTED_SUB_ACCOUNT_MESSAGE
    listed = await client.get("/v1/orgs", headers=acting(key, child_id))
    assert listed.status_code == 200, listed.text
    assert listed.json()["orgs"] == []


async def test_ownership_of_a_child_cannot_be_transferred_by_the_parent(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    response = await client.post(
        "/v1/org/transfer-ownership",
        json={"user_id": str(uuid.uuid4())},
        headers={**write_headers(client), HEADER: str(child_id)},
    )
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == PARENT_TRANSFER_MESSAGE


async def test_rate_limits_follow_the_child_plan(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child_id, child_key = await sub_account(client)
    await set_org(identity, child_id, plan_id="scale", limits={"rate_per_sec": 70})
    key, _ = await store_api_key(container, member.org.id)
    own = await client.get(ME, headers={"Authorization": f"Bearer {key}"})
    as_child = await client.get(ME, headers=acting(key, child_id))
    child_itself = await client.get(ME, headers={"Authorization": f"Bearer {child_key}"})
    assert as_child.headers[LIMIT_HEADER] == child_itself.headers[LIMIT_HEADER]
    assert as_child.headers[LIMIT_HEADER] != own.headers[LIMIT_HEADER]


async def test_a_key_invites_into_the_child_as_its_creator(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, child_id, key, row = await family(client, identity, container)
    async with container.database.system_session() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(created_by=member.user.id))
    response = await client.post(
        INVITATIONS, json={"email": "omar@acme-dental.test", "role": "viewer"}, headers=acting(key, child_id)
    )
    assert response.status_code == 201, response.text
    async with container.database.system_session() as session:
        invitation = await session.scalar(select(Invitation).where(Invitation.org_id == child_id))
    assert invitation is not None
    assert invitation.invited_by == member.user.id
