"""``GET /v1/audit_logs`` against a real database (specs/04 §13, §3.2 "Audit logs" row)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    Member,
    SessionContainer,
    error_of,
    make_member,
    signed_in,
    store_api_key,
    sub_account,
)
from sqlalchemy import delete

from va_api.routers.audit_logs import NAIVE_DATETIME_MESSAGE, WINDOW_MESSAGE
from va_db.models import AuditLog, MemberRole

LOGS: Final = "/v1/audit_logs"
T0: Final = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
VIEW_FIELDS: Final = {
    "id",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "ip",
    "user_agent",
    "changes",
    "created_at",
}


def bearer(key: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **headers}


async def entries(identity: IdentityDb, org_id: uuid.UUID, count: int) -> list[AuditLog]:
    """``count`` API-key entries one minute apart from :data:`T0`, alternating two actions."""
    rows = [
        AuditLog(
            org_id=org_id,
            actor_type="api_key",
            actor_id=uuid.UUID(int=minute % 2 + 1),
            action="api_key.create" if minute % 2 else "member.invite",
            resource_type="api_key" if minute % 2 else "invitation",
            resource_id=f"res-{minute}",
            ip="198.51.100.7",
            user_agent="curl/8.7.1",
            changes={"before": None, "after": {"minute": minute}},
            created_at=T0 + timedelta(minutes=minute),
            updated_at=T0,
        )
        for minute in range(count)
    ]
    async with identity.database.system_session() as session:
        session.add_all(rows)
    return rows


async def signed_in_owner(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> Member:
    """An owner signed in on ``client`` whose org log no longer holds the sign-in's ``auth.login`` row, so it
    lists only the entries a test seeds (va_system may delete audit rows; va_app may not)."""
    owner = await signed_in(client, identity, container)
    async with identity.database.system_session() as session:
        await session.execute(
            delete(AuditLog).where(AuditLog.org_id == owner.org.id, AuditLog.action == "auth.login")
        )
    return owner


async def listed(client: httpx.AsyncClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    response = await client.get(LOGS, params=params, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def ids(body: dict[str, Any]) -> list[str]:
    return [item["resource_id"] for item in body["data"]]


async def test_owner_session_reads_entries_newest_first(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in_owner(client, identity, container)
    rows = await entries(identity, owner.org.id, 3)
    body = await listed(client, {})
    assert ids(body) == ["res-2", "res-1", "res-0"]
    assert body["errors"] is None
    assert body["pagination"] == {"next_cursor": None, "has_more": False}
    first = body["data"][0]
    assert set(first) == VIEW_FIELDS
    assert first == {
        "id": str(rows[2].id),
        "actor_type": "api_key",
        "actor_id": str(uuid.UUID(int=1)),
        "action": "member.invite",
        "resource_type": "invitation",
        "resource_id": "res-2",
        "ip": "198.51.100.7",
        "user_agent": "curl/8.7.1",
        "changes": {"before": None, "after": {"minute": 2}},
        "created_at": "2026-10-29T09:02:00Z",
    }


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("admin", True), ("developer", False), ("analyst", False), ("billing", False), ("viewer", False)],
)
async def test_only_owners_and_admins_read_the_log(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    role: MemberRole,
    allowed: bool,
) -> None:
    await signed_in(client, identity, container, role)
    response = await client.get(LOGS)
    if allowed:
        assert response.status_code == 200, response.text
    else:
        assert error_of(response)[:2] == (403, "FORBIDDEN")


@pytest.mark.parametrize(
    ("scopes", "allowed"), [(("*",), True), (("org:read",), False), (("calls:read",), False)]
)
async def test_api_keys_need_star_for_the_audit_permission(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    scopes: tuple[str, ...],
    allowed: bool,
) -> None:
    owner = await make_member(identity, container.password_hasher)
    await entries(identity, owner.org.id, 1)
    key, _ = await store_api_key(container, owner.org.id, scopes)
    response = await client.get(LOGS, headers=bearer(key))
    if allowed:
        assert ids(response.json()) == ["res-0"]
    else:
        assert error_of(response)[:2] == (403, "FORBIDDEN")


async def test_filters_window_and_cursor_pages(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await make_member(identity, container.password_hasher)
    await entries(identity, owner.org.id, 6)
    key, _ = await store_api_key(container, owner.org.id)
    headers = bearer(key)
    assert ids(await listed(client, headers, action="api_key.create")) == ["res-5", "res-3", "res-1"]
    assert ids(await listed(client, headers, actor_id=str(uuid.UUID(int=1)))) == ["res-4", "res-2", "res-0"]
    assert ids(await listed(client, headers, resource_type="invitation", resource_id="res-2")) == ["res-2"]
    window = {"since": "2026-10-29T09:01:00Z", "until": "2026-10-29T10:03:00+01:00"}
    assert ids(await listed(client, headers, **window)) == ["res-2", "res-1"]

    seen: list[str] = []
    params: dict[str, Any] = {"limit": 4}
    while True:
        page = await listed(client, headers, **params)
        seen.extend(ids(page))
        if not page["pagination"]["has_more"]:
            assert page["pagination"]["next_cursor"] is None
            break
        params["cursor"] = page["pagination"]["next_cursor"]
    assert seen == [f"res-{minute}" for minute in range(5, -1, -1)]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"since": "2026-10-29T09:00:00"}, "since"),
        ({"until": "2026-10-29T09:00:00"}, "until"),
        ({"since": "2026-10-29T10:00:00Z", "until": "2026-10-29T09:00:00Z"}, "since"),
        ({"since": "yesterday"}, "since"),
        ({"actor_id": "not-a-uuid"}, "actor_id"),
        ({"cursor": "%%%"}, "cursor"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
    ],
)
async def test_invalid_queries_name_the_field(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    params: dict[str, Any],
    field: str,
) -> None:
    await signed_in(client, identity, container)
    response = await client.get(LOGS, params=params)
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_window_messages(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    naive = await client.get(LOGS, params={"until": "2026-10-29T09:00:00"})
    assert naive.json()["errors"][0]["message"] == f"until {NAIVE_DATETIME_MESSAGE}"
    reversed_window = await client.get(
        LOGS, params={"since": "2026-10-30T00:00:00Z", "until": "2026-10-29T00:00:00Z"}
    )
    assert reversed_window.json()["errors"][0]["message"] == WINDOW_MESSAGE


async def test_another_orgs_entries_are_invisible(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org_a = await make_member(identity, container.password_hasher)
    org_b = await make_member(identity, container.password_hasher)
    await entries(identity, org_a.org.id, 2)
    key_b, _ = await store_api_key(container, org_b.org.id)
    body = await listed(client, bearer(key_b))
    assert body["data"] == []
    assert await listed(client, bearer(key_b), resource_id="res-0") == {
        "data": [],
        "errors": None,
        "pagination": {"next_cursor": None, "has_more": False},
    }


async def test_a_parent_reads_its_sub_accounts_log(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in_owner(client, identity, container)
    child_id, _ = await sub_account(client)
    await entries(identity, child_id, 1)
    key, _ = await store_api_key(container, owner.org.id)
    child = await listed(client, bearer(key, **{"X-VA-Org-Id": str(child_id)}))
    assert ids(child) == ["res-0"]
    parent = await listed(client, bearer(key))
    assert [item["action"] for item in parent["data"]] == ["subaccount.create"]
