"""``/v1/webhooks/signing-secret`` view and rotation (specs/19 §10 Decision P06-T04a; specs/04 §3.2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import orjson
import pytest
from call_helpers import bearer
from identity_db import IdentityDb
from session_helpers import (
    Member,
    SessionContainer,
    add_member,
    audit_rows,
    error_of,
    login,
    make_member,
    signed_in,
    store_api_key,
    stored_org,
    sub_account,
    write_headers,
)
from sqlalchemy import text

from va_api.idempotency import DbIdempotencyStore
from va_db.models import MemberRole
from va_db.webhook_secrets import ROTATION_OVERLAP, active_signing_secrets

pytestmark = pytest.mark.integration

VIEW = "/v1/webhooks/signing-secret"
ROTATE = "/v1/webhooks/signing-secret/rotate"
DENIED_ROLES: tuple[MemberRole, ...] = ("developer", "analyst", "viewer", "billing")


@pytest.fixture
def db_idempotency(
    identity: IdentityDb, container: SessionContainer, monkeypatch: pytest.MonkeyPatch
) -> SessionContainer:
    """The container with the real ``idempotency_keys`` store, so stored replay bodies can be inspected."""
    monkeypatch.setattr(container, "idempotency", DbIdempotencyStore(identity.database, container.clock))
    return container


async def signed_in_owner(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> tuple[Member, str]:
    """A signed-in owner and the org's current signing secret."""
    member = await signed_in(client, identity, container)
    return member, await identity.provisioner.webhook_signing_secret(
        await stored_org(identity, member.org.id)
    )


async def rotate(client: httpx.AsyncClient, **headers: str) -> httpx.Response:
    return await client.post(ROTATE, headers={**write_headers(client), **headers})


async def rotated(client: httpx.AsyncClient, **headers: str) -> dict[str, Any]:
    response = await rotate(client, **headers)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def viewed(client: httpx.AsyncClient, **headers: str) -> dict[str, Any]:
    response = await client.get(VIEW, headers=headers)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_owner_views_the_last_four_characters(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    org = await stored_org(identity, member.org.id)
    secret = await identity.provisioner.webhook_signing_secret(org)
    data = await viewed(client)
    assert data == {
        "secret_last4": secret[-4:],
        "created_at": org.webhook_signing_secret_created_at.isoformat().replace("+00:00", "Z"),
        "previous_secret_last4": None,
        "previous_expires_at": None,
    }
    assert secret not in orjson.dumps(data).decode()


async def test_rotation_returns_the_new_secret_once_and_keeps_the_old_one_for_24_hours(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, old = await signed_in_owner(client, identity, container)
    now = container.clock.now()
    data = await rotated(client)
    new = data["secret"]
    assert new.startswith("whsec_")
    assert new != old
    assert data["created_at"] == now.isoformat().replace("+00:00", "Z")
    assert data["previous_expires_at"] == (now + ROTATION_OVERLAP).isoformat().replace("+00:00", "Z")
    view = await viewed(client)
    assert (view["secret_last4"], view["previous_secret_last4"]) == (new[-4:], old[-4:])
    assert view["previous_expires_at"] == data["previous_expires_at"]
    org = await stored_org(identity, member.org.id)
    active = await active_signing_secrets(identity.envelope, org, now=container.clock.now())
    assert active.secrets == (new, old)
    container.clock.advance(ROTATION_OVERLAP - timedelta(seconds=1))
    assert (await viewed(client))["previous_secret_last4"] == old[-4:]
    container.clock.advance(timedelta(seconds=1))
    later = await viewed(client)
    assert (later["secret_last4"], later["previous_secret_last4"], later["previous_expires_at"]) == (
        new[-4:],
        None,
        None,
    )


async def test_rotation_is_audited_without_any_secret(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, old = await signed_in_owner(client, identity, container)
    new = (await rotated(client))["secret"]
    (row,) = await audit_rows(identity, member.org.id, "webhook.secret_rotate")
    assert (row.resource_type, row.resource_id, row.actor_id) == ("org", str(member.org.id), member.user.id)
    assert row.changes["before"]["secret_last4"] == old[-4:]
    assert row.changes["before"]["previous_secret_last4"] is None
    assert row.changes["after"]["secret_last4"] == new[-4:]
    assert row.changes["after"]["previous_secret_last4"] == old[-4:]
    serialized = orjson.dumps(row.changes).decode()
    assert old not in serialized
    assert new not in serialized


async def test_a_replayed_rotation_withholds_the_secret_and_does_not_rotate_again(
    client: httpx.AsyncClient, identity: IdentityDb, db_idempotency: SessionContainer
) -> None:
    member = await signed_in(client, identity, db_idempotency)
    first = await rotate(client, **{"Idempotency-Key": "rotate-1"})
    second = await rotate(client, **{"Idempotency-Key": "rotate-1"})
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    new = first.json()["data"]["secret"]
    assert second.json()["data"] == {**first.json()["data"], "secret": None}
    assert (await viewed(client))["secret_last4"] == new[-4:]
    assert len(await audit_rows(identity, member.org.id, "webhook.secret_rotate")) == 1
    async with identity.database.system_session() as session:
        stored = list((await session.scalars(text("SELECT response_body::text FROM idempotency_keys"))).all())
    assert stored
    assert all(new not in body for body in stored)


async def test_admins_may_view_and_rotate(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container, role="admin")
    assert (await viewed(client))["secret_last4"]
    assert (await rotated(client))["secret"].startswith("whsec_")


@pytest.mark.parametrize("role", DENIED_ROLES)
async def test_other_roles_are_forbidden(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    user = await add_member(identity, container.password_hasher, owner.org.id, role)
    client.cookies.clear()
    await login(client, user.email)
    view = await client.get(VIEW)
    rotation = await rotate(client)
    assert view.status_code == rotation.status_code == 403
    assert error_of(rotation)[1] == "FORBIDDEN"
    assert await audit_rows(identity, owner.org.id, "webhook.secret_rotate") == []


async def test_a_full_access_api_key_may_rotate(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    key, _ = await store_api_key(container, member.org.id)
    response = await bare.post(ROTATE, headers=bearer(key))
    assert response.status_code == 200, response.text
    assert (await viewed(bare, **bearer(key)))["secret_last4"] == response.json()["data"]["secret"][-4:]


@pytest.mark.parametrize(
    ("scopes", "path"),
    [
        (("webhooks:read", "webhooks:write"), VIEW),
        (("webhooks:read", "webhooks:write"), ROTATE),
        (("calls:read",), VIEW),
    ],
)
async def test_narrow_api_keys_lack_the_secret_permission(
    scopes: tuple[str, ...],
    path: str,
    bare: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id, scopes)
    if path == ROTATE:
        response = await bare.post(path, headers=bearer(key))
    else:
        response = await bare.get(path, headers=bearer(key))
    assert error_of(response)[:2] == (403, "FORBIDDEN")


async def test_a_parent_rotates_its_sub_accounts_secret(
    client: httpx.AsyncClient, bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member, parent_old = await signed_in_owner(client, identity, container)
    child_id, _ = await sub_account(client, "Child Clinic")
    key, _ = await store_api_key(container, member.org.id)
    response = await bare.post(ROTATE, headers={**bearer(key), "X-VA-Org-Id": str(child_id)})
    assert response.status_code == 200, response.text
    child = await stored_org(identity, child_id)
    secrets = (await active_signing_secrets(identity.envelope, child, now=container.clock.now())).secrets
    assert secrets[0] == response.json()["data"]["secret"]
    parent = await stored_org(identity, member.org.id)
    assert await identity.provisioner.webhook_signing_secret(parent) == parent_old
