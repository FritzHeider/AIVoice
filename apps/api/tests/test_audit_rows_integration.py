"""Audit rows of the org, member, invitation, SSO, sub-account and API key routes (specs/04 §13 decision).

Each mutation writes exactly one row (two for a funded sub-account) in the same transaction as the change; a
refused request or a no-op writes none.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

import httpx
from fake_oidc import configure_sso
from identity_db import IdentityDb, unique_email
from session_helpers import (
    INVITATIONS,
    SessionContainer,
    add_member,
    audit_rows,
    error_of,
    fund,
    invite,
    login,
    signed_in,
    store_api_key,
    sub_account,
    write_headers,
)

from va_common.auth import API_KEY_RE
from va_db.audit import REDACTED
from va_db.models import AuditLog

KEYS: Final = "/v1/api_keys"
LOOPBACK: Final = "127.0.0.1"


async def send(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None, **headers: str
) -> httpx.Response:
    return await client.request(method, path, json=body, headers={**write_headers(client), **headers})


async def ok(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None, **headers: str
) -> dict[str, Any]:
    response = await send(client, method, path, body, **headers)
    assert response.status_code in {200, 201}, response.text
    data: dict[str, Any] = response.json()
    return data


def iso(stamp: str) -> str:
    """An API timestamp (``...Z``) as the audit log stores it (``isoformat()``, ``+00:00``)."""
    return stamp.replace("Z", "+00:00")


def assert_entry(
    row: AuditLog,
    *,
    actor: tuple[str, uuid.UUID],
    resource: tuple[str, uuid.UUID | str],
    before: object,
    after: object,
) -> None:
    assert (row.actor_type, row.actor_id) == actor
    assert (row.resource_type, row.resource_id) == (resource[0], str(resource[1]))
    assert (row.changes["before"], row.changes["after"]) == (before, after)
    assert str(row.ip) == LOOPBACK
    assert (row.user_agent or "").startswith("python-httpx/")


async def test_org_update_records_the_changed_fields_only(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    await ok(client, "PATCH", "/v1/org", {"name": "Austin Clinic", "retention_days": 400})
    await ok(client, "PATCH", "/v1/org", {"name": "Austin Clinic", "retention_days": 400})
    await ok(client, "PATCH", "/v1/org", {})
    (row,) = await audit_rows(identity, owner.org.id, "org.update")
    assert_entry(
        row,
        actor=("user", owner.user.id),
        resource=("org", owner.org.id),
        before={"name": "Acme Dental", "retention_days": 365},
        after={"name": "Austin Clinic", "retention_days": 400},
    )


async def test_member_role_change_remove_and_ownership_transfer(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    hasher = container.password_hasher
    developer = await add_member(identity, hasher, owner.org.id, "developer")
    leaving = await add_member(identity, hasher, owner.org.id, "viewer")
    await ok(client, "PATCH", f"/v1/org/members/{developer.id}", {"role": "analyst"})
    await ok(client, "PATCH", f"/v1/org/members/{developer.id}", {"role": "analyst"})
    await ok(client, "DELETE", f"/v1/org/members/{leaving.id}")
    await ok(client, "POST", "/v1/org/transfer-ownership", {"user_id": str(developer.id)})
    actor = ("user", owner.user.id)
    (changed,) = await audit_rows(identity, owner.org.id, "member.role_change")
    assert_entry(
        changed,
        actor=actor,
        resource=("membership", developer.id),
        before={"role": "developer"},
        after={"role": "analyst"},
    )
    (removed,) = await audit_rows(identity, owner.org.id, "member.remove")
    removed_member = {"user_id": str(leaving.id), "email": leaving.email, "role": "viewer"}
    assert_entry(removed, actor=actor, resource=("membership", leaving.id), before=removed_member, after=None)
    (transferred,) = await audit_rows(identity, owner.org.id, "org.ownership_transfer")
    assert_entry(
        transferred,
        actor=actor,
        resource=("org", owner.org.id),
        before={"owner_user_id": str(owner.user.id)},
        after={"owner_user_id": str(developer.id)},
    )


async def test_refused_requests_write_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    developer = await signed_in(client, identity, container, "developer")
    viewer = await add_member(identity, container.password_hasher, developer.org.id, "viewer")
    refused = await send(client, "PATCH", f"/v1/org/members/{viewer.id}", {"role": "admin"})
    assert error_of(refused)[:2] == (403, "FORBIDDEN")
    own = (await ok(client, "POST", KEYS, {"name": "dev", "scopes": ["calls:read"]}))["data"]
    await ok(client, "POST", f"{KEYS}/{own['id']}/revoke")
    conflict = await send(client, "POST", f"{KEYS}/{own['id']}/roll", {})
    assert error_of(conflict)[:2] == (409, "CONFLICT")
    actions = [entry.action for entry in await audit_rows(identity, developer.org.id)]
    assert actions == ["api_key.create", "api_key.revoke"]


async def test_invitation_create_and_revoke(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    email = unique_email("invitee")
    await invite(client, container, email, "billing")
    listed = (await client.get(INVITATIONS)).json()["data"]
    (invitation,) = listed
    await ok(client, "DELETE", f"{INVITATIONS}/{invitation['id']}")
    actor = ("user", owner.user.id)
    resource = ("invitation", invitation["id"])
    (invited,) = await audit_rows(identity, owner.org.id, "member.invite")
    after = {"email": email, "role": "billing", "expires_at": iso(invitation["expires_at"])}
    assert_entry(invited, actor=actor, resource=resource, before=None, after=after)
    (revoked,) = await audit_rows(identity, owner.org.id, "invitation.revoke")
    assert_entry(
        revoked, actor=actor, resource=resource, before={"email": email, "role": "billing"}, after=None
    )


async def test_sso_put_and_delete_redact_the_client_secret(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    configured = await configure_sso(sso, identity, container)
    owner = configured.owner
    await login(sso, owner.user.email)
    current = (await sso.get("/v1/org/sso")).json()["data"]
    body = {
        key: current[key] for key in ("issuer", "client_id", "allowed_domains", "default_role", "enforce")
    }
    await ok(sso, "PUT", "/v1/org/sso", {**body, "enforce": True})
    await ok(sso, "DELETE", "/v1/org/sso")
    created, updated, deleted = await audit_rows(identity, owner.org.id, "org.sso_update")
    view = {key: value for key, value in current.items() if key not in {"created_at", "updated_at"}}
    actor = ("user", owner.user.id)
    resource = ("sso_connection", current["id"])
    assert_entry(
        created, actor=actor, resource=resource, before=None, after={**view, "client_secret": REDACTED}
    )
    assert_entry(updated, actor=actor, resource=resource, before=view, after={**view, "enforce": True})
    assert_entry(deleted, actor=actor, resource=resource, before={**view, "enforce": True}, after=None)


async def test_sub_account_rows_land_in_the_parent_log(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    await fund(container, owner.org.id, "20")
    created = await ok(
        client, "POST", "/v1/orgs/create", {"org_display_name": "Austin", "initial_credits": 7.5}
    )
    child_id = uuid.UUID(created["org_id"])
    await ok(client, "POST", f"/v1/orgs/{child_id}/suspend")
    await ok(client, "POST", f"/v1/orgs/{child_id}/suspend")
    await ok(client, "POST", f"/v1/orgs/{child_id}/reactivate")
    actor = ("user", owner.user.id)
    resource = ("org", child_id)
    entries = await audit_rows(identity, owner.org.id)
    assert [entry.action for entry in entries] == [
        "subaccount.create",
        "billing.credit_transfer",
        "subaccount.suspend",
        "subaccount.reactivate",
    ]
    create, transfer, suspend, reactivate = entries
    after = {"name": "Austin", "plan_id": created["org_plan"], "initial_credits_micros": 7_500_000}
    assert_entry(create, actor=actor, resource=resource, before=None, after=after)
    assert_entry(transfer, actor=actor, resource=resource, before=None, after={"amount_micros": 7_500_000})
    assert_entry(
        suspend, actor=actor, resource=resource, before={"status": "active"}, after={"status": "suspended"}
    )
    assert_entry(
        reactivate, actor=actor, resource=resource, before={"status": "suspended"}, after={"status": "active"}
    )
    assert API_KEY_RE.search(json.dumps([entry.changes for entry in entries])) is None
    assert await audit_rows(identity, child_id) == []


async def test_api_key_lifecycle_never_records_the_plaintext(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    created = (await ok(client, "POST", KEYS, {"name": "server"}))["data"]
    key_id = created["id"]
    await ok(client, "PATCH", f"{KEYS}/{key_id}", {"name": "server (prod)"})
    await ok(client, "PATCH", f"{KEYS}/{key_id}", {"name": "server (prod)"})
    await ok(client, "PATCH", f"{KEYS}/{key_id}", {})
    rolled = (await ok(client, "POST", f"{KEYS}/{key_id}/roll", {"expire_old_in_hours": 0}))["data"]
    await ok(client, "POST", f"{KEYS}/{rolled['id']}/revoke")
    revoked = (await ok(client, "POST", f"{KEYS}/{rolled['id']}/revoke"))["data"]
    entries = await audit_rows(identity, owner.org.id)
    assert [entry.action for entry in entries] == [
        "api_key.create",
        "api_key.update",
        "api_key.roll",
        "api_key.revoke",
    ]
    create, update, roll, revoke = entries
    actor = ("user", owner.user.id)
    view = {name: value for name, value in created.items() if name != "key"}
    view["created_at"] = iso(view["created_at"])
    assert_entry(create, actor=actor, resource=("api_key", key_id), before=None, after=view)
    assert_entry(
        update,
        actor=actor,
        resource=("api_key", key_id),
        before={"name": "server"},
        after={"name": "server (prod)"},
    )
    rolled_after = {
        "expires_at": roll.changes["after"]["expires_at"],
        "new_key_id": rolled["id"],
        "new_prefix": rolled["prefix"],
    }
    assert_entry(
        roll, actor=actor, resource=("api_key", key_id), before={"expires_at": None}, after=rolled_after
    )
    revoked_at = iso(revoked["revoked_at"])
    assert_entry(
        revoke,
        actor=actor,
        resource=("api_key", rolled["id"]),
        before={"revoked_at": None},
        after={"revoked_at": revoked_at},
    )
    assert API_KEY_RE.search(json.dumps([entry.changes for entry in entries])) is None


async def test_acting_parent_rows_land_in_the_child_with_the_parent_marker(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    key, key_row = await store_api_key(container, owner.org.id)
    await ok(
        client,
        "PATCH",
        "/v1/org",
        {"name": "Austin Clinic"},
        Authorization=f"Bearer {key}",
        **{"X-VA-Org-Id": str(child_id)},
    )
    (row,) = await audit_rows(identity, child_id, "org.update")
    assert_entry(
        row,
        actor=("api_key", key_row.id),
        resource=("org", child_id),
        before={"name": "Acme Dental - Austin"},
        after={"name": "Austin Clinic"},
    )
    assert row.changes["acting_parent_org_id"] == str(owner.org.id)
    assert await audit_rows(identity, owner.org.id, "org.update") == []
