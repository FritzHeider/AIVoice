"""Bland-compatible sub-accounts under ``/v1/orgs`` against a real database (specs/04 §8.2, §8.4, §3.2)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from identity_db import IdentityDb
from redis.exceptions import ConnectionError as RedisConnectionError
from session_helpers import (
    SessionContainer,
    error_of,
    fund,
    hold,
    make_member,
    session_app,
    session_client,
    set_org,
    signed_in,
    store_api_key,
    stored_org,
    write_headers,
)
from sqlalchemy import func, select, text
from structlog.testing import capture_logs

from va_api.idempotency import DbIdempotencyStore
from va_api.routers import orgs as orgs_module
from va_api.routers.orgs import (
    CLOSED_SUB_ACCOUNT_MESSAGE,
    NESTED_SUB_ACCOUNT_MESSAGE,
    SUB_ACCOUNT_KEY_NAME,
    SUB_ACCOUNT_NOT_FOUND_MESSAGE,
)
from va_common.auth import api_key_prefix, looks_like_api_key
from va_common.ids import new_id
from va_db.models import ApiKey, CreditLedger, MemberRole, Org

CREATE = "/v1/orgs/create"
ORGS = "/v1/orgs"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def child_ids(identity: IdentityDb, parent_id: uuid.UUID) -> list[uuid.UUID]:
    async with identity.database.system_session() as session:
        rows = await session.scalars(select(Org.id).where(Org.parent_org_id == parent_id).order_by(Org.id))
        return list(rows.all())


async def keys_of(identity: IdentityDb, org_id: uuid.UUID) -> list[ApiKey]:
    async with identity.database.system_session() as session:
        return list((await session.scalars(select(ApiKey).where(ApiKey.org_id == org_id))).all())


async def ledger_of(identity: IdentityDb, org_id: uuid.UUID) -> list[CreditLedger]:
    async with identity.database.system_session() as session:
        statement = (
            select(CreditLedger).where(CreditLedger.org_id == org_id).order_by(CreditLedger.created_at)
        )
        return list((await session.scalars(statement)).all())


async def ledger_sum(identity: IdentityDb, org_id: uuid.UUID) -> int:
    async with identity.database.system_session() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount_micros), 0)).where(
                CreditLedger.org_id == org_id
            )
        )
    return int(total or 0)


async def create(client: httpx.AsyncClient, body: dict[str, Any], **headers: str) -> httpx.Response:
    """``POST /v1/orgs/create`` with the session's CSRF headers (harmless for key requests)."""
    return await client.post(CREATE, json=body, headers={**write_headers(client), **headers})


async def created_child(
    client: httpx.AsyncClient, name: str = "Acme Dental - Austin", **headers: str
) -> dict[str, Any]:
    response = await create(client, {"org_display_name": name}, **headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_owner_session_creates_a_sub_account_with_a_working_key(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await set_org(identity, member.org.id, plan_id="scale", kyc_level=2)
    response = await create(client, {"org_display_name": "  Acme Dental - Austin  "})
    assert response.status_code == 200, response.text
    body = response.json()
    child_id = uuid.UUID(body["org_id"])
    assert body["status"] == "success"
    assert (body["org_display_name"], body["org_plan"], body["kyc_level"], body["credits"]) == (
        "Acme Dental - Austin",
        "scale",
        2,
        0.0,
    )
    assert body["created_at"]
    assert looks_like_api_key(body["api_key"])
    assert body["api_key"].startswith("va_live_")
    child = await stored_org(identity, child_id)
    assert (child.parent_org_id, child.plan_id, child.kyc_level, child.status) == (
        member.org.id,
        "scale",
        2,
        "active",
    )
    [key] = await keys_of(identity, child_id)
    assert (key.name, key.scopes, key.environment, key.created_by) == (
        SUB_ACCOUNT_KEY_NAME,
        ["*"],
        "live",
        member.user.id,
    )
    assert key.prefix == api_key_prefix(body["api_key"])
    assert body["api_key"] not in key.secret_hash
    assert container.api_key_hasher.verify(body["api_key"], key.secret_hash, key.pepper_version)
    async with session_client(session_app(container)) as other:
        me = await other.get("/v1/me", headers=bearer(body["api_key"]))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["org"]["id"] == str(child_id)
    assert me.json()["data"]["api_key"]["id"] == str(key.id)


async def test_admin_session_and_star_key_may_create(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container, role="admin")
    await created_child(client)
    key, _ = await store_api_key(container, member.org.id)
    async with session_client(session_app(container)) as other:
        response = await other.post(
            CREATE, json={"org_display_name": "Dallas", "org_plan": "start"}, headers=bearer(key)
        )
    assert response.status_code == 200, response.text
    assert response.json()["org_plan"] == "start"
    [_, from_key] = await child_ids(identity, member.org.id)
    [row] = await keys_of(identity, from_key)
    assert row.created_by is None


async def test_initial_credits_move_from_the_parent(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await fund(container, member.org.id, "50")
    response = await create(
        client, {"org_display_name": "Austin", "org_plan": "build", "initial_credits": 10.5}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    child_id = uuid.UUID(body["org_id"])
    assert (body["credits"], body["org_plan"]) == (10.5, "build")
    parent, child = await stored_org(identity, member.org.id), await stored_org(identity, child_id)
    assert (parent.credits_balance_micros, child.credits_balance_micros) == (39_500_000, 10_500_000)
    assert await ledger_sum(identity, member.org.id) == 39_500_000
    assert await ledger_sum(identity, child_id) == 10_500_000
    out_entry = (await ledger_of(identity, member.org.id))[-1]
    [in_entry] = await ledger_of(identity, child_id)
    assert (out_entry.entry_type, in_entry.entry_type) == ("transfer_out", "transfer_in")
    assert out_entry.idempotency_key == in_entry.idempotency_key
    assert out_entry.idempotency_key.startswith("xfer:")
    listed = (await client.get(f"{ORGS}/{child_id}")).json()["org"]
    assert listed["credits"] == 10.5


async def test_insufficient_credits_create_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await fund(container, member.org.id, "20")
    await hold(container, member.org.id, "15")
    await hold(container, member.org.id, "15", status="captured")
    response = await create(client, {"org_display_name": "Austin", "initial_credits": 5.01})
    assert error_of(response) == (402, "INSUFFICIENT_CREDITS", None)
    assert response.json()["status"] == "error"
    assert await child_ids(identity, member.org.id) == []
    assert (await stored_org(identity, member.org.id)).credits_balance_micros == 20_000_000
    allowed = await create(client, {"org_display_name": "Austin", "initial_credits": 5})
    assert allowed.status_code == 200, allowed.text
    assert len(await child_ids(identity, member.org.id)) == 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"org_display_name": ""}, "org_display_name"),
        ({"org_display_name": "   "}, "org_display_name"),
        ({"org_display_name": "x" * 201}, "org_display_name"),
        ({}, "org_display_name"),
        ({"org_display_name": "Austin", "org_plan": "enterprise"}, "org_plan"),
        ({"org_display_name": "Austin", "initial_credits": -1}, "initial_credits"),
        ({"org_display_name": "Austin", "initial_credits": 1.0000001}, "initial_credits"),
        ({"org_display_name": "Austin", "initial_credits": 1_000_001}, "initial_credits"),
    ],
)
async def test_invalid_create_bodies(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    field: str,
) -> None:
    member = await signed_in(client, identity, container)
    response = await create(client, body)
    assert error_of(response) == (400, "INVALID_INPUT", field)
    assert response.json()["status"] == "error"
    assert await child_ids(identity, member.org.id) == []


async def test_unknown_fields_are_ignored(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    response = await create(
        client, {"org_display_name": "Austin", "org_region": "us-east", "initial_credits": 0}
    )
    assert response.status_code == 200, response.text
    assert response.json()["credits"] == 0.0


async def test_a_sub_account_cannot_create_sub_accounts(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child = await created_child(client)
    async with session_client(session_app(container)) as other:
        response = await other.post(
            CREATE, json={"org_display_name": "Grandchild"}, headers=bearer(child["api_key"])
        )
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert response.json()["message"] == NESTED_SUB_ACCOUNT_MESSAGE
    assert await child_ids(identity, uuid.UUID(child["org_id"])) == []
    assert len(await child_ids(identity, member.org.id)) == 1


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_other_roles_are_refused(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    for response in (
        await create(client, {"org_display_name": "Austin"}),
        await client.get(ORGS),
        await client.get(f"{ORGS}/{new_id()}"),
        await client.post(f"{ORGS}/{new_id()}/suspend", headers=write_headers(client)),
    ):
        assert error_of(response)[:2] == (403, "FORBIDDEN"), response.text
    assert await child_ids(identity, member.org.id) == []


@pytest.mark.parametrize("scopes", [("org:read",), ("org:write",), ("org:read", "org:write")])
async def test_keys_without_star_are_refused(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, scopes: tuple[str, ...]
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id, scopes=scopes)
    create_response = await client.post(CREATE, json={"org_display_name": "Austin"}, headers=bearer(key))
    list_response = await client.get(ORGS, headers=bearer(key))
    assert error_of(create_response)[:2] == (403, "FORBIDDEN")
    assert error_of(list_response)[:2] == (403, "FORBIDDEN")
    assert await child_ids(identity, member.org.id) == []


async def test_list_shows_only_direct_children_oldest_first(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    first = await created_child(client, "Austin")
    second = await created_child(client, "Dallas")
    stranger = await make_member(identity, container.password_hasher)
    stranger_key, _ = await store_api_key(container, stranger.org.id)
    async with session_client(session_app(container)) as other:
        foreign = await other.post(
            CREATE, json={"org_display_name": "Elsewhere"}, headers=bearer(stranger_key)
        )
        assert foreign.status_code == 200, foreign.text
        child_list = await other.get(ORGS, headers=bearer(first["api_key"]))
    response = await client.get(ORGS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert [item["org_id"] for item in body["orgs"]] == [first["org_id"], second["org_id"]]
    assert body["orgs"][0] == {
        "org_id": first["org_id"],
        "org_display_name": "Austin",
        "org_plan": first["org_plan"],
        "kyc_level": 0,
        "credits": 0.0,
        "status": "active",
        "created_at": first["created_at"],
    }
    assert child_list.status_code == 200, child_list.text
    assert child_list.json() == {"status": "success", "orgs": []}


async def test_get_finds_only_direct_children(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child = await created_child(client)
    found = await client.get(f"{ORGS}/{child['org_id']}")
    assert found.status_code == 200, found.text
    assert found.json()["status"] == "success"
    assert found.json()["org"]["org_id"] == child["org_id"]
    stranger = await make_member(identity, container.password_hasher)
    stranger_key, _ = await store_api_key(container, stranger.org.id)
    async with session_client(session_app(container)) as other:
        sibling = (
            await other.post(CREATE, json={"org_display_name": "Other"}, headers=bearer(stranger_key))
        ).json()
        upward = await other.get(f"{ORGS}/{member.org.id}", headers=bearer(child["api_key"]))
    for org_id in (sibling["org_id"], str(member.org.id), str(stranger.org.id), str(new_id())):
        response = await client.get(f"{ORGS}/{org_id}")
        assert error_of(response) == (404, "NOT_FOUND", None)
        assert response.json()["message"] == SUB_ACCOUNT_NOT_FOUND_MESSAGE
    assert error_of(upward) == (404, "NOT_FOUND", None)
    assert error_of(await client.get(f"{ORGS}/not-a-uuid"))[:2] == (400, "INVALID_INPUT")


async def test_suspend_applies_to_cached_keys_at_once_and_reactivate_restores(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    child = await created_child(client)
    child_id, child_key = child["org_id"], bearer(child["api_key"])
    async with session_client(session_app(container)) as other:
        assert (await other.get(ORGS, headers=child_key)).status_code == 200
        suspended = await client.post(f"{ORGS}/{child_id}/suspend", headers=write_headers(client))
        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["org"]["status"] == "suspended"
        again = await client.post(f"{ORGS}/{child_id}/suspend", headers=write_headers(client))
        assert again.json()["org"]["status"] == "suspended"
        write = await other.post(CREATE, json={"org_display_name": "Grandchild"}, headers=child_key)
        assert error_of(write) == (403, "ORG_SUSPENDED", None)
        assert (await other.get(ORGS, headers=child_key)).status_code == 200
        reactivated = await client.post(f"{ORGS}/{child_id}/reactivate", headers=write_headers(client))
        assert reactivated.status_code == 200, reactivated.text
        assert reactivated.json()["org"]["status"] == "active"
        write = await other.post(CREATE, json={"org_display_name": "Grandchild"}, headers=child_key)
    assert error_of(write) == (403, "FORBIDDEN", None)
    assert write.json()["message"] == NESTED_SUB_ACCOUNT_MESSAGE


async def test_closed_and_foreign_sub_accounts_cannot_change(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child = await created_child(client)
    await set_org(identity, uuid.UUID(child["org_id"]), status="closed")
    for action in ("suspend", "reactivate"):
        response = await client.post(f"{ORGS}/{child['org_id']}/{action}", headers=write_headers(client))
        assert error_of(response) == (409, "CONFLICT", None)
        assert response.json()["message"] == CLOSED_SUB_ACCOUNT_MESSAGE
    stranger = await make_member(identity, container.password_hasher)
    for org_id in (stranger.org.id, member.org.id, new_id()):
        response = await client.post(f"{ORGS}/{org_id}/suspend", headers=write_headers(client))
        assert error_of(response) == (404, "NOT_FOUND", None)
    assert (await stored_org(identity, stranger.org.id)).status == "active"
    assert (await stored_org(identity, member.org.id)).status == "active"


async def test_cache_invalidation_failure_is_logged(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(client, identity, container)
    child = await created_child(client)

    async def unavailable(redis: object, prefix: str) -> None:
        raise RedisConnectionError(prefix)

    monkeypatch.setattr(orgs_module, "invalidate_api_key_cache", unavailable)
    with capture_logs() as logs:
        response = await client.post(f"{ORGS}/{child['org_id']}/suspend", headers=write_headers(client))
    assert response.status_code == 200, response.text
    assert [entry["key_prefix"] for entry in logs if entry["event"] == "api_key_cache_unavailable"] == [
        api_key_prefix(child["api_key"])
    ]


async def test_idempotent_create_replays_the_same_sub_account(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container, "idempotency", DbIdempotencyStore(identity.database, container.clock))
    member = await signed_in(client, identity, container)
    headers = {"Idempotency-Key": "create-austin-1"}
    first = await create(client, {"org_display_name": "Austin"}, **headers)
    second = await create(client, {"org_display_name": "Austin"}, **headers)
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["org_id"] == first.json()["org_id"]
    assert first.json()["api_key"].startswith("va_live_")
    assert second.json()["api_key"] is None
    assert await child_ids(identity, member.org.id) == [uuid.UUID(first.json()["org_id"])]
    async with identity.database.system_session() as session:
        stored = list((await session.scalars(text("SELECT response_body::text FROM idempotency_keys"))).all())
    assert len(stored) == 1
    assert first.json()["org_id"] in stored[0]
    assert first.json()["api_key"] not in stored[0]
    assert api_key_prefix(first.json()["api_key"]) not in stored[0]
    async with session_client(session_app(container)) as other:
        me = await other.get("/v1/me", headers=bearer(first.json()["api_key"]))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["org"]["id"] == first.json()["org_id"]
