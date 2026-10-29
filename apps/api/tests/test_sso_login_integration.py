"""OIDC login: start, callback, and JIT provisioning against a real database (specs/04 §5)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fake_oidc import (
    CALLBACK,
    ISSUER,
    OTHER_KEY,
    START,
    FakeOidcProvider,
    SsoOrg,
    configure_sso,
    login_error,
    mocked_idp,
    session_cookie_set,
    sso_app,
    sso_login,
)
from identity_db import IdentityDb
from session_helpers import (
    API_BASE_URL,
    DASHBOARD_URL,
    SESSION_PROBE_PATH,
    SessionContainer,
    audit_rows,
    login,
    make_member,
    session_client,
    write_headers,
)
from sqlalchemy import select, update

from va_api.auth.cookies import SSO_STATE_COOKIE
from va_api.auth.oidc_discovery import DISCOVERY_PATH
from va_api.routers.auth_sso import REASONS
from va_common.auth import generate_token
from va_db.models import Session, User
from va_db.repositories import IdentityRepo, MembershipRepo

API_HOST = urlsplit(API_BASE_URL).hostname or ""


@pytest.fixture
def provider(container: SessionContainer) -> FakeOidcProvider:
    return FakeOidcProvider(now=container.clock.now)


@pytest.fixture
def idp(provider: FakeOidcProvider) -> Iterator[respx.MockRouter]:
    with mocked_idp(provider) as router:
        yield router


@pytest.fixture
async def sso(container: SessionContainer, idp: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(sso_app(container)) as http:
        yield http


@pytest.fixture
async def org(sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> SsoOrg:
    return await configure_sso(sso, identity, container)


async def user_of(identity: IdentityDb, email: str) -> User | None:
    async with identity.database.global_session() as session:
        return await IdentityRepo(session).get_user_by_email(email)


async def role_of(identity: IdentityDb, org: SsoOrg, user: User) -> str | None:
    async with identity.database.tenant_session(org.owner.org.id) as session:
        membership = await MembershipRepo(session, org.owner.org.id).get_for_user(user.id)
        return None if membership is None else membership.role


async def failed(response: httpx.Response, reason: str) -> None:
    assert reason in REASONS
    assert response.status_code == 302, response.text
    assert response.headers["location"] == login_error(reason)
    assert not session_cookie_set(response)


def sso_cookie_cleared(response: httpx.Response) -> bool:
    return any(
        item.startswith(f'{SSO_STATE_COOKIE}=""') and "Max-Age=0" in item
        for item in response.headers.get_list("set-cookie")
    )


async def test_start_redirects_to_the_provider_and_binds_the_browser(
    sso: httpx.AsyncClient, org: SsoOrg, container: SessionContainer
) -> None:
    email = org.email()
    response = await sso.get(START, params={"email": email})
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    query = {name: values[0] for name, values in parse_qs(location.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == "voiceagents-dashboard"
    assert query["scope"] == "openid email profile"
    assert query["redirect_uri"] == f"{API_BASE_URL}{CALLBACK}"
    assert query["login_hint"] == email
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) == len(query["nonce"]) == 43
    cookie = next(item for item in response.headers.get_list("set-cookie") if item.startswith("va_sso="))
    assert cookie.startswith(f"va_sso={query['state']};")
    for attribute in ("HttpOnly", "Max-Age=600", "Path=/internal/auth/sso", "SameSite=lax", "Secure"):
        assert attribute in cookie
    ttl = await container.redis.ttl(f"sso:state:{query['state']}")
    assert 0 < ttl <= 600
    stored = await container.redis.get(f"sso:state:{query['state']}")
    assert stored is not None
    verifier = str(json.loads(stored)["code_verifier"])
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == challenge


@pytest.mark.parametrize("email", ["", "nope", "a@b", "@acme-dental.test"])
async def test_start_refuses_invalid_emails(sso: httpx.AsyncClient, email: str) -> None:
    await failed(await sso.get(START, params={"email": email}), "invalid_email")


async def test_start_failures(sso: httpx.AsyncClient, org: SsoOrg, idp: respx.MockRouter) -> None:
    await failed(await sso.get(START, params={"email": "sam@unclaimed.acme-dental.test"}), "not_configured")
    idp.get(f"{ISSUER}{DISCOVERY_PATH}").respond(500)
    await failed(await sso.get(START, params={"email": org.email()}), "provider_unavailable")


async def test_disabled_deployment(client: httpx.AsyncClient) -> None:
    await failed(await client.get(START, params={"email": "sam@acme-dental.test"}), "disabled")
    response = await client.get(CALLBACK, params={"state": generate_token(), "code": "code-1"})
    await failed(response, "disabled")


async def test_new_user_is_provisioned_and_signed_in(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    email = org.email()
    response = await sso_login(sso, provider, email)
    assert response.status_code == 302, response.text
    assert response.headers["location"] == f"{DASHBOARD_URL}/"
    assert session_cookie_set(response)
    assert sso_cookie_cleared(response)
    session = await sso.get("/internal/auth/session")
    assert session.status_code == 200, session.text
    data = session.json()["data"]
    assert data["user"]["email"] == email
    assert data["mfa_passed"] is True
    assert data["active_org_id"] == str(org.owner.org.id)
    assert [item["role"] for item in data["memberships"]] == ["developer"]
    user = await user_of(identity, email)
    assert user is not None
    assert user.password_hash is None
    assert user.email_verified_at is not None
    assert user.name == "Sam Rivera"
    assert user.last_login_at is not None
    async with identity.database.global_session() as db:
        rows = (await db.scalars(select(Session).where(Session.user_id == user.id))).all()
    assert [(row.mfa_passed, row.mfa_passed_at) for row in rows] == [(True, provider.now())]
    probe = await sso.get(SESSION_PROBE_PATH)
    assert probe.status_code == 200, probe.text


async def test_existing_users_join_and_keep_their_roles(
    sso: httpx.AsyncClient,
    org: SsoOrg,
    provider: FakeOidcProvider,
    identity: IdentityDb,
    container: SessionContainer,
) -> None:
    outsider = await make_member(identity, container.password_hasher)
    email = org.email("pat")
    async with identity.database.global_session() as session:
        await session.execute(update(User).where(User.id == outsider.user.id).values(email=email))
    assert (await sso_login(sso, provider, email)).headers["location"] == f"{DASHBOARD_URL}/"
    assert await role_of(identity, org, outsider.user) == "developer"

    sso.cookies.clear()
    owner_email = org.email("owner")
    async with identity.database.global_session() as session:
        await session.execute(
            update(User)
            .where(User.id == org.owner.user.id)
            .values(email=owner_email, email_verified_at=None, mfa_enabled=True)
        )
    response = await sso_login(sso, provider, owner_email)
    assert response.headers["location"] == f"{DASHBOARD_URL}/"
    assert await role_of(identity, org, org.owner.user) == "owner"
    owner = await user_of(identity, owner_email)
    assert owner is not None
    assert owner.email_verified_at is not None
    assert (await sso.get(SESSION_PROBE_PATH)).status_code == 200


async def test_disabled_user_is_refused(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    async with identity.database.global_session() as session:
        await session.execute(
            update(User)
            .where(User.id == org.owner.user.id)
            .values(status="disabled", email=f"jane@{org.domain}")
        )
    await failed(await sso_login(sso, provider, f"jane@{org.domain}"), "account_disabled")


async def test_state_must_be_known_unused_and_bound_to_the_browser(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    email = org.email()
    unknown = generate_token()
    sso.cookies.set(SSO_STATE_COOKIE, unknown, domain=API_HOST, path="/internal/auth/sso")
    response = await sso.get(CALLBACK, params={"state": unknown, "code": "code-1"})
    await failed(response, "invalid_state")
    assert sso_cookie_cleared(response)

    start = await sso.get(START, params={"email": email})
    state = provider.authorize(start.headers["location"])
    sso.cookies.clear()
    await failed(await sso.get(CALLBACK, params={"state": state, "code": "code-1"}), "invalid_state")
    sso.cookies.set(SSO_STATE_COOKIE, generate_token(), domain=API_HOST, path="/internal/auth/sso")
    await failed(await sso.get(CALLBACK, params={"state": state, "code": "code-1"}), "invalid_state")
    await failed(await sso.get(CALLBACK, params={"state": "short", "code": "code-1"}), "invalid_state")

    sso.cookies.clear()
    assert (await sso_login(sso, provider, email)).headers["location"] == f"{DASHBOARD_URL}/"
    sso.cookies.set(
        SSO_STATE_COOKIE, provider.authorized["state"], domain=API_HOST, path="/internal/auth/sso"
    )
    replay = await sso.get(CALLBACK, params={"state": provider.authorized["state"], "code": "code-1"})
    await failed(replay, "invalid_state")
    assert provider.token_calls == 1
    assert await user_of(identity, email) is not None


async def test_provider_errors(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    email = org.email()
    await failed(await sso_login(sso, provider, email, error="access_denied"), "provider_error")
    await failed(await sso_login(sso, provider, email, code=""), "provider_error")
    await failed(await sso_login(sso, provider, email, code="code-2"), "provider_error")
    provider.token_status = 503
    await failed(await sso_login(sso, provider, email), "provider_error")
    assert await user_of(identity, email) is None


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "another-nonce"},
        {"iss": "https://evil.acme-dental.test"},
        {"aud": "another-client"},
        {"exp": 1_700_000_000},
    ],
)
async def test_invalid_id_tokens(
    sso: httpx.AsyncClient,
    org: SsoOrg,
    provider: FakeOidcProvider,
    identity: IdentityDb,
    claims: dict[str, Any],
) -> None:
    provider.claims = claims
    email = org.email()
    await failed(await sso_login(sso, provider, email), "invalid_token")
    assert await user_of(identity, email) is None


async def test_bad_signature(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    provider.key = OTHER_KEY
    email = org.email()
    await failed(await sso_login(sso, provider, email), "invalid_token")
    assert await user_of(identity, email) is None


async def test_email_rules(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    email = org.email()
    provider.claims = {"email_verified": False}
    await failed(await sso_login(sso, provider, email), "email_not_verified")
    provider.claims = {"email": "sam@elsewhere.acme-dental.test"}
    await failed(await sso_login(sso, provider, email), "email_domain_mismatch")
    assert await user_of(identity, email) is None
    provider.claims = {"email_verified": None}
    assert (await sso_login(sso, provider, email)).headers["location"] == f"{DASHBOARD_URL}/"
    assert await user_of(identity, email) is not None


async def test_connection_removed_between_start_and_callback(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider
) -> None:
    start = await sso.get(START, params={"email": org.email()})
    state = provider.authorize(start.headers["location"])
    await login(sso, org.owner.user.email)
    deleted = await sso.delete("/v1/org/sso", headers=write_headers(sso))
    assert deleted.status_code == 200
    await failed(await sso.get(CALLBACK, params={"state": state, "code": "code-1"}), "not_configured")


async def test_presented_session_is_revoked(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    await login(sso, org.owner.user.email)
    previous = sso.cookies.get("va_session")
    assert previous is not None
    assert (await sso_login(sso, provider, org.email())).headers["location"] == f"{DASHBOARD_URL}/"
    assert sso.cookies.get("va_session") != previous
    async with identity.database.global_session() as session:
        assert await IdentityRepo(session).get_active_session(previous, provider.now()) is None


async def test_logins_are_audited_in_the_connection_org(
    sso: httpx.AsyncClient, org: SsoOrg, provider: FakeOidcProvider, identity: IdentityDb
) -> None:
    email = org.email()
    assert (await sso_login(sso, provider, email)).headers["location"] == f"{DASHBOARD_URL}/"
    sso.cookies.clear()
    assert (await sso_login(sso, provider, email)).headers["location"] == f"{DASHBOARD_URL}/"
    user = await user_of(identity, email)
    assert user is not None
    async with identity.database.global_session() as session:
        statement = (
            select(Session.id).where(Session.user_id == user.id).order_by(Session.created_at, Session.id)
        )
        session_ids = [str(row) for row in await session.scalars(statement)]
    rows = await audit_rows(identity, org.owner.org.id, "auth.sso_login")
    assert [(row.actor_type, row.actor_id, row.resource_type, row.resource_id) for row in rows] == [
        ("user", user.id, "user", str(user.id))
    ] * 2
    assert [row.changes["after"] for row in rows] == [
        {"method": "sso", "session_id": session_ids[0], "user_created": True, "membership_created": True},
        {"method": "sso", "session_id": session_ids[1], "user_created": False, "membership_created": False},
    ]
    assert {str(row.ip) for row in rows} == {"127.0.0.1"}
