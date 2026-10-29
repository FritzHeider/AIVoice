"""``/v1/org/sso`` against a real database (specs/04-auth-tenancy-rbac.md §3.3, §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fake_oidc import discovery_document, new_domain
from identity_db import IdentityDb
from session_helpers import (
    SessionContainer,
    error_of,
    make_member,
    session_app,
    session_client,
    signed_in,
    write_headers,
)
from sqlalchemy import select

from va_api.auth.oidc_discovery import DISCOVERY_PATH
from va_api.routers.org_sso import decrypt_client_secret
from va_db.models import MemberRole, SsoConnection

SSO = "/v1/org/sso"
ISSUER = "https://login.acme-dental.test"
SECRET = "idp-client-credential-7f3a"  # noqa: S105 - test client secret


@pytest.fixture
def domain() -> str:
    return new_domain()


@pytest.fixture
def body(domain: str) -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "client_id": "voiceagents-dashboard",
        "client_secret": SECRET,
        "allowed_domains": [domain],
    }


@pytest.fixture
def idp() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        for issuer in (ISSUER, "https://sso.acme-dental.test"):
            router.get(f"{issuer}{DISCOVERY_PATH}").respond(json=discovery_document(issuer))
        yield router


@pytest.fixture
async def sso_client(container: SessionContainer, idp: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(container, oidc_enabled=True)) as http:
        yield http


async def put(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.put(SSO, json=body, headers=write_headers(client))


async def stored(identity: IdentityDb, org_id: Any) -> SsoConnection | None:
    async with identity.database.tenant_session(org_id) as session:
        return await session.scalar(select(SsoConnection).where(SsoConnection.org_id == org_id))


def contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


async def test_owner_creates_reads_updates_and_deletes(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    domain: str,
) -> None:
    member = await signed_in(sso_client, identity, container)
    created = await put(sso_client, body)
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert not contains_key(created.json(), "client_secret")
    assert SECRET not in created.text
    assert data["issuer"] == ISSUER
    assert data["allowed_domains"] == [domain]
    assert (data["provider"], data["default_role"], data["enforce"]) == ("oidc", "viewer", False)
    row = await stored(identity, member.org.id)
    assert row is not None
    assert SECRET.encode() not in row.client_secret_enc
    assert await decrypt_client_secret(identity.envelope, row) == SECRET

    fetched = await sso_client.get(SSO)
    assert fetched.status_code == 200
    assert fetched.json()["data"] == data

    update = {k: v for k, v in body.items() if k != "client_secret"}
    update |= {
        "issuer": "https://sso.acme-dental.test/",
        "enforce": True,
        "allowed_domains": [new_domain()],
    }
    updated = await put(sso_client, update)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["id"] == data["id"]
    assert updated.json()["data"]["issuer"] == "https://sso.acme-dental.test"
    assert updated.json()["data"]["enforce"] is True
    row = await stored(identity, member.org.id)
    assert row is not None
    assert await decrypt_client_secret(identity.envelope, row) == SECRET

    deleted = await sso_client.delete(SSO, headers=write_headers(sso_client))
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"id": data["id"], "deleted": True}
    assert error_of(await sso_client.get(SSO)) == (404, "NOT_FOUND", None)
    assert error_of(await sso_client.delete(SSO, headers=write_headers(sso_client))) == (
        404,
        "NOT_FOUND",
        None,
    )


async def test_first_put_needs_the_client_secret(
    sso_client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    await signed_in(sso_client, identity, container)
    response = await put(sso_client, {k: v for k, v in body.items() if k != "client_secret"})
    assert error_of(response) == (400, "INVALID_INPUT", "client_secret")


async def test_admin_may_manage(
    sso_client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    await signed_in(sso_client, identity, container, role="admin")
    assert (await put(sso_client, body)).status_code == 200


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_other_roles_are_forbidden(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    role: MemberRole,
) -> None:
    await signed_in(sso_client, identity, container, role=role)
    assert error_of(await sso_client.get(SSO))[:2] == (403, "FORBIDDEN")
    assert error_of(await put(sso_client, body))[:2] == (403, "FORBIDDEN")


async def test_api_keys_never_manage_sso(
    sso_client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, _ = container.store.issue(org_id=member.org.id, scopes=("*",))
    headers = {"Authorization": key}
    for response in (
        await sso_client.get(SSO, headers=headers),
        await sso_client.put(SSO, json=body, headers=headers),
        await sso_client.delete(SSO, headers=headers),
    ):
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == "Missing required permission 'sso.manage'."


async def test_csrf_and_authentication(
    sso_client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    assert error_of(await sso_client.get(SSO))[:2] == (401, "AUTH_FAILURE")
    await signed_in(sso_client, identity, container)
    no_csrf = {"Origin": "https://app.voiceagents.test"}
    assert error_of(await sso_client.put(SSO, json=body, headers=no_csrf))[:2] == (403, "FORBIDDEN")
    assert error_of(await sso_client.delete(SSO, headers=no_csrf))[:2] == (403, "FORBIDDEN")


async def test_discovery_failure_writes_nothing(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    idp: respx.MockRouter,
) -> None:
    member = await signed_in(sso_client, identity, container)
    idp.get(f"https://broken.acme-dental.test{DISCOVERY_PATH}").respond(503)
    response = await put(sso_client, {**body, "issuer": "https://broken.acme-dental.test"})
    assert error_of(response) == (400, "INVALID_INPUT", "issuer")
    assert response.json()["errors"][0]["message"] == "Could not validate the OIDC issuer: HTTP 503."
    assert await stored(identity, member.org.id) is None


async def test_domains_claimed_by_another_org_conflict(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    domain: str,
) -> None:
    await signed_in(sso_client, identity, container)
    assert (await put(sso_client, {**body, "allowed_domains": [new_domain(), domain]})).status_code == 200
    assert (await put(sso_client, {k: v for k, v in body.items() if k != "client_secret"})).status_code == 200

    await signed_in(sso_client, identity, container)
    response = await put(sso_client, {**body, "allowed_domains": [new_domain(), domain.upper()]})
    assert error_of(response) == (409, "CONFLICT", "allowed_domains")
    message = f"Domain {domain} is already claimed by another organization's SSO connection."
    assert response.json()["errors"][0]["message"] == message
    assert error_of(await sso_client.get(SSO))[:2] == (404, "NOT_FOUND")


async def test_domains_are_normalised(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    domain: str,
) -> None:
    await signed_in(sso_client, identity, container)
    other = new_domain()
    domains = [f" {domain.upper()} ", domain, other]
    response = await put(sso_client, {**body, "allowed_domains": domains, "default_role": "developer"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["allowed_domains"] == [domain, other]
    assert response.json()["data"]["default_role"] == "developer"


@pytest.mark.parametrize(
    "case",
    [
        ({"allowed_domains": ["*.acme-dental.test"]}, "allowed_domains"),
        ({"allowed_domains": ["localhost"]}, "allowed_domains"),
        ({"allowed_domains": []}, "allowed_domains"),
        ({"allowed_domains": [f"d{n}.acme-dental.test" for n in range(51)]}, "allowed_domains"),
        ({"default_role": "owner"}, "default_role"),
        ({"provider": "saml"}, "provider"),
        ({"issuer": "https://login.acme-dental.test?x=1"}, "issuer"),
        ({"client_secret": ""}, "client_secret"),
        ({"scim": True}, "scim"),
    ],
)
async def test_invalid_bodies(
    sso_client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    case: tuple[dict[str, Any], str],
) -> None:
    override, field = case
    await signed_in(sso_client, identity, container)
    assert error_of(await put(sso_client, {**body, **override})) == (400, "INVALID_INPUT", field)


async def test_disabled_deployment_refuses(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    await signed_in(client, identity, container)
    for response in (await client.get(SSO), await put(client, body)):
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert response.json()["errors"][0]["message"] == "SSO is not enabled on this deployment."


async def test_connections_are_tenant_isolated(
    sso_client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, body: dict[str, Any]
) -> None:
    await signed_in(sso_client, identity, container)
    assert (await put(sso_client, body)).status_code == 200
    await signed_in(sso_client, identity, container)
    assert error_of(await sso_client.get(SSO)) == (404, "NOT_FOUND", None)
    assert error_of(await sso_client.delete(SSO, headers=write_headers(sso_client)))[:2] == (404, "NOT_FOUND")
