"""``GET /v1/me``, ``GET /v1/org`` and ``PATCH /v1/org`` against a real database (specs/04 §3.2, §8.1)."""

from __future__ import annotations

import uuid
from typing import Any, get_args

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    DASHBOARD_URL,
    Member,
    SessionContainer,
    error_of,
    login,
    make_member,
    store_api_key,
    write_headers,
)
from sqlalchemy import update

from va_api.auth.rbac import ROLE_SCOPES
from va_db.models import MemberRole, Org, Plan

ME = "/v1/me"
ORG = "/v1/org"
ROLES = get_args(MemberRole)


async def signed_in(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole = "owner"
) -> Member:
    member = await make_member(identity, container.password_hasher, role=role)
    await login(client, member.user.email)
    return member


async def set_org(identity: IdentityDb, org_id: uuid.UUID, **values: Any) -> None:
    async with identity.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == org_id).values(**values))


async def stored_org(identity: IdentityDb, org_id: uuid.UUID) -> Org:
    async with identity.database.system_session() as session:
        org = await session.get(Org, org_id)
    assert org is not None
    return org


async def patch(client: httpx.AsyncClient, body: dict[str, Any], **headers: str) -> httpx.Response:
    return await client.patch(ORG, json=body, headers={**write_headers(client), **headers})


async def test_me_for_an_owner_session(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    response = await client.get(ME)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["principal_type"] == "user"
    assert data["user"] == {
        "id": str(member.user.id),
        "email": member.user.email,
        "name": "Jane Park",
        "mfa_enabled": False,
    }
    assert data["api_key"] is None
    assert data["org"] == {
        "id": str(member.org.id),
        "name": "Acme Dental",
        "slug": member.org.slug,
        "plan_id": member.org.plan_id,
        "status": "active",
    }
    assert data["role"] == "owner"
    assert data["scopes"] == sorted(ROLE_SCOPES["owner"])


async def test_me_for_a_viewer_session(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container, role="viewer")
    data = (await client.get(ME)).json()["data"]
    assert data["role"] == "viewer"
    assert data["scopes"] == sorted(ROLE_SCOPES["viewer"])


async def test_me_for_an_api_key(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, row = await store_api_key(container, member.org.id, ("org:read", "calls:read"))
    response = await client.get(ME, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["principal_type"] == "api_key"
    assert data["user"] is None
    assert data["role"] is None
    assert data["api_key"] == {
        "id": str(row.id),
        "prefix": row.prefix,
        "scopes": ["calls:read", "org:read"],
        "environment": "live",
    }
    assert data["org"]["id"] == str(member.org.id)
    assert data["scopes"] == ["calls:read", "org:read"]


async def test_me_refuses_keys_without_org_read_and_unknown_principals(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    narrow, _ = await store_api_key(container, member.org.id, ("calls:read",))
    assert error_of(await client.get(ME, headers={"Authorization": narrow}))[:2] == (403, "FORBIDDEN")
    vanished, _ = container.store.issue(org_id=member.org.id, scopes=("*",))
    assert error_of(await client.get(ME, headers={"Authorization": vanished}))[:2] == (401, "AUTH_FAILURE")
    assert error_of(await client.get(ME))[:2] == (401, "AUTH_FAILURE")


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_reads_the_org(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    await set_org(
        identity,
        member.org.id,
        plan_id="build",
        limits={"concurrency": 7, "unknown_key": 1},
        credits_balance_micros=12_500_000,
        settings={"timezone": "America/Chicago"},
    )
    async with identity.database.system_session() as session:
        plan = await session.get(Plan, "build")
    assert plan is not None
    response = await client.get(ORG)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["id"] == str(member.org.id)
    assert (data["name"], data["slug"], data["plan_id"]) == ("Acme Dental", member.org.slug, "build")
    assert (data["status"], data["data_region"], data["parent_org_id"]) == ("active", "us", None)
    assert (data["retention_days"], data["recording_retention_days"]) == (365, 90)
    assert data["credits_balance"] == 12.5
    assert data["settings"] == {"timezone": "America/Chicago"}
    limits = data["limits"]
    assert limits["concurrency"] == 7
    assert limits["daily_calls"] == plan.daily_call_cap
    assert limits["hourly_calls"] == plan.hourly_call_cap
    assert limits["voice_clones"] == plan.voice_clone_cap
    assert limits["connected_rate_micros_per_min"] == plan.connected_rate_micros_per_min
    assert "unknown_key" not in limits


async def test_owner_renames_and_admin_sets_retention(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    response = await patch(client, {"name": "  Acme Dental West  "})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Acme Dental West"
    assert (await stored_org(identity, owner.org.id)).name == "Acme Dental West"

    admin = await signed_in(client, identity, container, role="admin")
    response = await patch(client, {"retention_days": 180, "recording_retention_days": 0})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["retention_days"], data["recording_retention_days"], data["name"]) == (180, 0, "Acme Dental")
    stored = await stored_org(identity, admin.org.id)
    assert (stored.retention_days, stored.recording_retention_days) == (180, 0)


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_roles_without_org_write_cannot_patch(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    assert error_of(await patch(client, {"name": "Taken Over"}))[:2] == (403, "FORBIDDEN")
    assert (await stored_org(identity, member.org.id)).name == "Acme Dental"


async def test_api_keys_need_org_compliance_for_retention(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    full, _ = await store_api_key(container, member.org.id)
    response = await client.patch(
        ORG, json={"name": "Acme Dental East", "retention_days": 30}, headers={"Authorization": full}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["retention_days"] == 30

    writer, _ = await store_api_key(container, member.org.id, ("org:write",))
    headers = {"Authorization": writer}
    response = await client.patch(ORG, json={"recording_retention_days": 10}, headers=headers)
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    assert response.json()["errors"][0]["message"] == "Missing required permission 'org.compliance'."
    response = await client.patch(ORG, json={"name": "Acme Dental North"}, headers=headers)
    assert response.status_code == 200, response.text
    stored = await stored_org(identity, member.org.id)
    assert (stored.name, stored.retention_days, stored.recording_retention_days) == (
        "Acme Dental North",
        30,
        90,
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 201}, "name"),
        ({"retention_days": 0}, "retention_days"),
        ({"retention_days": 4000}, "retention_days"),
        ({"recording_retention_days": -1}, "recording_retention_days"),
        ({"settings": {"timezone": "UTC"}}, "settings"),
    ],
)
async def test_invalid_patch_bodies(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    field: str,
) -> None:
    await signed_in(client, identity, container)
    assert error_of(await patch(client, body)) == (400, "INVALID_INPUT", field)


async def test_empty_patch_returns_the_org_unchanged(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    before = await stored_org(identity, member.org.id)
    response = await patch(client, {})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Acme Dental"
    assert (await stored_org(identity, member.org.id)).updated_at == before.updated_at


async def test_session_patch_needs_csrf(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    response = await client.patch(ORG, json={"name": "No Token"}, headers={"Origin": DASHBOARD_URL})
    assert error_of(response)[:2] == (403, "FORBIDDEN")


async def test_patch_never_touches_another_org(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    other = await make_member(identity, container.password_hasher)
    member = await signed_in(client, identity, container)
    response = await patch(
        client, {"name": "Acme Dental West", "retention_days": 30}, **{"X-VA-Org-Id": str(other.org.id)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == str(member.org.id)
    untouched = await stored_org(identity, other.org.id)
    assert (untouched.name, untouched.retention_days) == ("Acme Dental", 365)
    other_key, _ = await store_api_key(container, other.org.id)
    data = (await client.get(ORG, headers={"Authorization": other_key})).json()["data"]
    assert data["id"] == str(other.org.id)
