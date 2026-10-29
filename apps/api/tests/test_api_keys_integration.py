"""``/v1/api_keys`` against a real database (specs/04-auth-tenancy-rbac.md §9, §3.2 "API keys" rows, §2.1)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    SessionContainer,
    add_member,
    error_of,
    login,
    session_app,
    session_client,
    signed_in,
    store_api_key,
    sub_account,
    write_headers,
)
from sqlalchemy import select, text

from va_api.auth.api_key_auth import cache_key
from va_api.auth.api_keys import NOT_OWN_KEY_MESSAGE, TEST_KEYS_ONLY_MESSAGE
from va_api.idempotency import DbIdempotencyStore
from va_api.routers.api_keys import API_KEY_NOT_FOUND_MESSAGE, NOT_ROLLABLE_MESSAGE
from va_common.auth import api_key_prefix
from va_common.ids import new_id
from va_db.models import ApiKey, MemberRole

KEYS = "/v1/api_keys"
KEY_SHAPE = re.compile(r"va_(live|test)_[0-9A-Za-z]{32}")
VIEW_FIELDS = {
    "id",
    "name",
    "prefix",
    "scopes",
    "environment",
    "created_by",
    "last_used_at",
    "expires_at",
    "revoked_at",
    "created_at",
}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
async def bare(container: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    """A second client without a session, for requests authenticated by API keys only."""
    async with session_client(session_app(container)) as other:
        yield other


async def post(client: httpx.AsyncClient, path: str, body: dict[str, Any], **headers: str) -> httpx.Response:
    """A POST with the session's CSRF headers (harmless for key requests)."""
    return await client.post(path, json=body, headers={**write_headers(client), **headers})


async def patch(
    client: httpx.AsyncClient, key_id: str, body: dict[str, Any], **headers: str
) -> httpx.Response:
    return await client.patch(f"{KEYS}/{key_id}", json=body, headers={**write_headers(client), **headers})


async def created(
    client: httpx.AsyncClient, body: dict[str, Any] | None = None, **headers: str
) -> dict[str, Any]:
    """``POST /v1/api_keys`` that must succeed; the ``data`` of the response."""
    response = await post(client, KEYS, body or {"name": "server"}, **headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def rolled(
    client: httpx.AsyncClient, key_id: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = await post(client, f"{KEYS}/{key_id}/roll", body or {})
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def stored_key(identity: IdentityDb, key_id: str) -> ApiKey:
    async with identity.database.system_session() as session:
        key = await session.get(ApiKey, uuid.UUID(key_id))
    assert key is not None
    return key


async def authenticates(client: httpx.AsyncClient, key: str) -> bool:
    response = await client.get("/v1/me", headers=bearer(key))
    assert response.status_code in {200, 401}, response.text
    return response.status_code == 200


async def test_owner_creates_a_default_key_that_works_and_lists_by_prefix(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    data = await created(client, {"name": "  Zapier integration  "})
    assert KEY_SHAPE.fullmatch(data["key"])
    assert data["key"].startswith("va_live_")
    assert set(data) == VIEW_FIELDS | {"key"}
    assert (data["name"], data["prefix"], data["scopes"], data["environment"]) == (
        "Zapier integration",
        api_key_prefix(data["key"]),
        ["*"],
        "live",
    )
    assert (data["created_by"], data["expires_at"], data["revoked_at"]) == (str(owner.user.id), None, None)
    row = await stored_key(identity, data["id"])
    assert row.org_id == owner.org.id
    assert data["key"] not in row.secret_hash
    assert container.api_key_hasher.verify(data["key"], row.secret_hash, row.pepper_version)
    me = await bare.get("/v1/me", headers=bearer(data["key"]))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["api_key"]["id"] == data["id"]
    second = await created(bare, {"name": "by key", "scopes": ["calls:read"]}, **bearer(data["key"]))
    assert second["created_by"] == str(owner.user.id)
    listing = await client.get(KEYS)
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]
    assert [item["id"] for item in items] == [data["id"], second["id"]]
    assert all(set(item) == VIEW_FIELDS for item in items)
    assert data["key"] not in listing.text


async def test_create_and_roll_replays_withhold_the_plaintext(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container, "idempotency", DbIdempotencyStore(identity.database, container.clock))
    await signed_in(client, identity, container)
    headers = {"Idempotency-Key": "create-server-1"}
    first = await post(client, KEYS, {"name": "server"}, **headers)
    second = await post(client, KEYS, {"name": "server"}, **headers)
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert first.json()["data"]["key"].startswith("va_live_")
    assert second.json()["data"]["key"] is None
    roll_path = f"{KEYS}/{first.json()['data']['id']}/roll"
    roll_one = await post(client, roll_path, {}, **{"Idempotency-Key": "roll-server-1"})
    roll_two = await post(client, roll_path, {}, **{"Idempotency-Key": "roll-server-1"})
    assert roll_one.status_code == roll_two.status_code == 200
    assert roll_two.json()["data"]["id"] == roll_one.json()["data"]["id"]
    assert roll_two.json()["data"]["key"] is None
    async with identity.database.system_session() as session:
        stored = list((await session.scalars(text("SELECT response_body::text FROM idempotency_keys"))).all())
    assert len(stored) == 2
    for plaintext in (first.json()["data"]["key"], roll_one.json()["data"]["key"]):
        assert all(plaintext not in body for body in stored)


async def test_keys_grant_only_scopes_they_hold(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    star = await created(client, {"name": "star"})
    narrow = await created(
        client, {"name": "narrow", "scopes": ["calls:read", "api_keys:write", "calls:read"]}
    )
    assert narrow["scopes"] == ["api_keys:write", "calls:read"]
    auth = bearer(narrow["key"])
    child = await created(bare, {"name": "child", "scopes": ["calls:read"]}, **auth)
    assert child["scopes"] == ["calls:read"]
    for scopes, missing in ((["*"], "*"), (["calls:read", "calls:write"], "calls:write")):
        response = await post(bare, KEYS, {"name": "wider", "scopes": scopes}, **auth)
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == f"Cannot grant scopes you do not hold: {missing}."
    response = await bare.post(f"{KEYS}/{star['id']}/roll", json={}, headers=auth)
    assert error_of(response) == (403, "FORBIDDEN", None)
    collapsed = await created(bare, {"name": "all", "scopes": ["mcp", "*"]}, **bearer(star["key"]))
    assert collapsed["scopes"] == ["*"]


async def test_create_validates_its_body(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    past = (container.clock.now() - timedelta(minutes=1)).isoformat()
    cases: list[tuple[dict[str, Any], str]] = [
        ({"name": "server", "scopes": ["calls:read", "calls:delete"]}, "scopes"),
        ({"name": "server", "scopes": []}, "scopes"),
        ({"name": "server", "scopes": ["calls:read"] * 51}, "scopes"),
        ({"name": "   "}, "name"),
        ({"name": "k" * 101}, "name"),
        ({"name": "server", "environment": "staging"}, "environment"),
        ({"name": "server", "expires_at": past}, "expires_at"),
        ({"name": "server", "expires_at": container.clock.now().isoformat()}, "expires_at"),
        ({"name": "server", "expires_at": "2099-01-01T00:00:00"}, "expires_at"),
    ]
    for body, field in cases:
        assert error_of(await post(client, KEYS, body)) == (400, "INVALID_INPUT", field), body
    response = await post(client, KEYS, {"name": "server", "role": "owner"})
    assert error_of(response)[:2] == (400, "INVALID_INPUT")
    unknown = await post(client, KEYS, {"name": "server", "scopes": ["calls:delete"]})
    assert unknown.json()["errors"][0]["message"] == "Unknown scope 'calls:delete'."
    assert (await client.get(KEYS)).json()["data"] == []


async def test_expires_at_stops_the_key(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    expires_at = container.clock.now() + timedelta(hours=1)
    data = await created(client, {"name": "temp", "expires_at": expires_at.isoformat()})
    assert (await stored_key(identity, data["id"])).expires_at == expires_at
    assert await authenticates(bare, data["key"])
    container.clock.advance(timedelta(hours=1))
    assert not await authenticates(bare, data["key"])


async def test_every_route_needs_api_keys_write(
    bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner_org = (await signed_in(bare, identity, container)).org.id
    bare.cookies.clear()
    reader, _ = await store_api_key(container, owner_org, scopes=("calls:read", "org:read"))
    writer, row = await store_api_key(container, owner_org, scopes=("api_keys:write",))
    requests = [
        ("GET", KEYS, None),
        ("POST", KEYS, {"name": "server"}),
        ("PATCH", f"{KEYS}/{row.id}", {"name": "renamed"}),
        ("POST", f"{KEYS}/{row.id}/revoke", None),
        ("POST", f"{KEYS}/{row.id}/roll", {}),
    ]
    for method, path, body in requests:
        response = await bare.request(method, path, json=body, headers=bearer(reader))
        assert error_of(response) == (403, "FORBIDDEN", None), path
        assert response.json()["errors"][0]["message"] == "Missing required scope 'api_keys:write'."
    listing = await bare.get(KEYS, headers=bearer(writer))
    assert listing.status_code == 200, listing.text
    assert {item["prefix"] for item in listing.json()["data"]} == {
        api_key_prefix(reader),
        api_key_prefix(writer),
    }


async def test_test_keys_manage_only_test_keys(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    live = await created(client, {"name": "live"})
    test_key, _ = await store_api_key(container, owner.org.id, environment="test")
    auth = bearer(test_key)
    data = await created(bare, {"name": "sandbox"}, **auth)
    assert data["environment"] == "test"
    assert data["key"].startswith("va_test_")
    response = await post(bare, KEYS, {"name": "escalate", "environment": "live"}, **auth)
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert response.json()["errors"][0]["message"] == TEST_KEYS_ONLY_MESSAGE
    response = await post(bare, f"{KEYS}/{live['id']}/roll", {}, **auth)
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert response.json()["errors"][0]["message"] == TEST_KEYS_ONLY_MESSAGE
    test_from_live = await created(client, {"name": "sandbox 2", "environment": "test"})
    assert test_from_live["environment"] == "test"


async def test_developers_create_within_their_scopes_and_manage_only_their_keys(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    owners_key = await created(client, {"name": "owner's"})
    developer = await add_member(identity, container.password_hasher, owner.org.id, "developer")
    await login(bare, developer.email)
    response = await post(bare, KEYS, {"name": "everything"})
    assert error_of(response) == (403, "FORBIDDEN", None)
    own = await created(bare, {"name": "mine", "scopes": ["calls:read", "calls:write"]})
    assert own["created_by"] == str(developer.id)
    for response in (
        await patch(bare, owners_key["id"], {"name": "taken"}),
        await post(bare, f"{KEYS}/{owners_key['id']}/revoke", {}),
        await post(bare, f"{KEYS}/{owners_key['id']}/roll", {}),
    ):
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == NOT_OWN_KEY_MESSAGE
    assert (await patch(bare, own["id"], {"name": "mine (prod)"})).json()["data"]["name"] == "mine (prod)"
    assert (await rolled(bare, own["id"]))["scopes"] == ["calls:read", "calls:write"]
    revoked = await post(bare, f"{KEYS}/{own['id']}/revoke", {})
    assert revoked.status_code == 200, revoked.text
    listing = await bare.get(KEYS)
    assert listing.status_code == 200
    assert owners_key["id"] in {item["id"] for item in listing.json()["data"]}
    owner_patch = await patch(client, own["id"], {"name": "audited"})
    assert owner_patch.status_code == 200, owner_patch.text


@pytest.mark.parametrize("role", ["analyst", "billing", "viewer"])
async def test_roles_without_api_keys_write_are_refused(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    await signed_in(client, identity, container, role=role)
    assert error_of(await client.get(KEYS)) == (403, "FORBIDDEN", None)
    assert error_of(await post(client, KEYS, {"name": "server"})) == (403, "FORBIDDEN", None)


async def test_revoke_stops_a_cached_key_at_once_and_keeps_the_first_time(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    data = await created(client)
    assert await authenticates(bare, data["key"])
    assert await container.redis.exists(cache_key(data["prefix"]))
    revoke_path = f"{KEYS}/{data['id']}/revoke"
    first = await post(client, revoke_path, {})
    assert first.status_code == 200, first.text
    revoked_at = first.json()["data"]["revoked_at"]
    assert revoked_at is not None
    assert "key" not in first.json()["data"]
    assert not await authenticates(bare, data["key"])
    container.clock.advance(timedelta(hours=1))
    again = await post(client, revoke_path, {})
    assert again.status_code == 200, again.text
    assert again.json()["data"]["revoked_at"] == revoked_at
    renamed = await patch(client, data["id"], {"name": "retired"})
    assert renamed.json()["data"]["name"] == "retired"
    assert error_of(await post(client, f"{KEYS}/{data['id']}/roll", {})) == (409, "CONFLICT", None)
    response = await post(client, f"{KEYS}/{data['id']}/roll", {})
    assert response.json()["errors"][0]["message"] == NOT_ROLLABLE_MESSAGE


async def test_a_key_may_revoke_itself(
    bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner_org = (await signed_in(bare, identity, container)).org.id
    data = await created(bare)
    bare.cookies.clear()
    response = await bare.post(f"{KEYS}/{data['id']}/revoke", headers=bearer(data["key"]))
    assert response.status_code == 200, response.text
    assert not await authenticates(bare, data["key"])
    assert (await stored_key(identity, data["id"])).org_id == owner_org


async def test_roll_copies_the_key_and_expires_the_old_one_later(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    old = await created(client, {"name": "billing sync", "scopes": ["billing:read", "org:read"]})
    assert await authenticates(bare, old["key"])
    now = container.clock.now()
    new = await rolled(client, old["id"])
    assert new["id"] != old["id"]
    assert new["key"] != old["key"]
    assert (new["name"], new["scopes"], new["environment"], new["expires_at"]) == (
        "billing sync",
        ["billing:read", "org:read"],
        "live",
        None,
    )
    assert new["created_by"] == str(owner.user.id)
    assert (await stored_key(identity, old["id"])).expires_at == now + timedelta(hours=24)
    assert await authenticates(bare, new["key"])
    assert await authenticates(bare, old["key"])
    container.clock.advance(timedelta(hours=24))
    assert not await authenticates(bare, old["key"])
    assert await authenticates(bare, new["key"])
    assert error_of(await post(client, f"{KEYS}/{old['id']}/roll", {})) == (409, "CONFLICT", None)


async def test_roll_with_zero_hours_expires_the_old_key_at_once(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    old = await created(client)
    assert await authenticates(bare, old["key"])
    for hours in (-1, 169, "soon"):
        response = await post(client, f"{KEYS}/{old['id']}/roll", {"expire_old_in_hours": hours})
        assert error_of(response) == (400, "INVALID_INPUT", "expire_old_in_hours")
    new = await rolled(client, old["id"], {"expire_old_in_hours": 0})
    assert not await authenticates(bare, old["key"])
    assert await authenticates(bare, new["key"])


async def test_roll_keeps_an_earlier_expiry(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    soon = container.clock.now() + timedelta(hours=2)
    old = await created(client, {"name": "short", "expires_at": soon.isoformat()})
    await rolled(client, old["id"], {"expire_old_in_hours": 48})
    assert (await stored_key(identity, old["id"])).expires_at == soon


async def test_patch_renames_and_empty_patch_changes_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    data = await created(client)
    renamed = await patch(client, data["id"], {"name": " Zapier (prod) "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["name"] == "Zapier (prod)"
    unchanged = await patch(client, data["id"], {})
    assert unchanged.json()["data"] == renamed.json()["data"]
    assert error_of(await patch(client, data["id"], {"name": ""})) == (400, "INVALID_INPUT", "name")
    assert error_of(await patch(client, data["id"], {"scopes": ["*"]}))[:2] == (400, "INVALID_INPUT")
    assert error_of(await patch(client, "not-a-uuid", {"name": "x"}))[:2] == (400, "INVALID_INPUT")


async def test_other_orgs_keys_are_not_found(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    theirs = await created(client)
    await signed_in(bare, identity, container)
    for key_id in (theirs["id"], str(new_id())):
        for response in (
            await patch(bare, key_id, {"name": "stolen"}),
            await post(bare, f"{KEYS}/{key_id}/revoke", {}),
            await post(bare, f"{KEYS}/{key_id}/roll", {}),
        ):
            assert error_of(response) == (404, "NOT_FOUND", None)
            assert response.json()["errors"][0]["message"] == API_KEY_NOT_FOUND_MESSAGE
    assert (await bare.get(KEYS)).json()["data"] == []
    assert (await stored_key(identity, theirs["id"])).revoked_at is None


async def test_parent_manages_a_sub_accounts_keys_with_the_org_header(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    parent_key = await created(client, {"name": "parent"})
    child_id, _ = await sub_account(client)
    acting = {"X-VA-Org-Id": str(child_id)}
    data = await created(client, {"name": "child ops", "scopes": ["calls:read"]}, **acting)
    row = await stored_key(identity, data["id"])
    assert (row.org_id, row.created_by) == (child_id, owner.user.id)
    child_listing = await client.get(KEYS, headers=acting)
    assert len(child_listing.json()["data"]) == 2
    assert parent_key["id"] not in {item["id"] for item in child_listing.json()["data"]}
    async with identity.database.system_session() as session:
        parent_ids = set(
            (await session.scalars(select(ApiKey.id).where(ApiKey.org_id == owner.org.id))).all()
        )
    assert parent_ids == {uuid.UUID(parent_key["id"])}
    assert error_of(await post(client, f"{KEYS}/{parent_key['id']}/revoke", {}, **acting)) == (
        404,
        "NOT_FOUND",
        None,
    )
