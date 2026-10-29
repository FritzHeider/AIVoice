"""SSO enforcement of password logins (specs/04-auth-tenancy-rbac.md §5 step 6; specs/31 ``SSO_REQUIRED``)."""

from __future__ import annotations

import httpx
from fake_oidc import configure_sso
from identity_db import IdentityDb
from session_helpers import (
    API_BASE_URL,
    DASHBOARD_URL,
    PASSWORD,
    SESSION_PROBE_PATH,
    SessionContainer,
    error_of,
    login,
    make_member,
)
from sqlalchemy import update

from va_api.auth.login_throttle import email_failure_key
from va_api.routers.auth import SSO_REQUIRED_MESSAGE
from va_db.models import User


async def domain_user(identity: IdentityDb, container: SessionContainer, email: str) -> None:
    """A verified password user (in an org of their own) whose address is ``email``."""
    member = await make_member(identity, container.password_hasher)
    async with identity.database.global_session() as session:
        await session.execute(update(User).where(User.id == member.user.id).values(email=email))


async def password_login(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(
        "/internal/auth/login", json={"email": email, "password": PASSWORD}, headers={"Origin": DASHBOARD_URL}
    )


async def test_enforced_domain_requires_sso(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org = await configure_sso(sso, identity, container, enforce=True)
    email = org.email().replace("sam-", "Sam-")
    await domain_user(identity, container, email)
    response = await password_login(sso, f"  {email.upper()} ")
    assert error_of(response) == (403, "SSO_REQUIRED", None)
    item = response.json()["errors"][0]
    assert item["message"] == SSO_REQUIRED_MESSAGE
    sso_url = response.json()["errors"][0]["details"]["sso_url"]
    assert sso_url.startswith(f"{API_BASE_URL}/internal/auth/sso/start?email=")
    assert email.upper().replace("@", "%40") in sso_url
    assert await container.redis.get(email_failure_key(email)) is None
    assert "va_session" not in sso.cookies


async def test_unenforced_domain_logs_in_with_a_password(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org = await configure_sso(sso, identity, container, enforce=False)
    email = org.email()
    await domain_user(identity, container, email)
    await login(sso, email)


async def test_enforcement_is_off_when_oidc_is_disabled(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await configure_sso(sso, identity, container, enforce=True)
    email = org.email()
    await domain_user(identity, container, email)
    await login(client, email)


async def test_api_keys_still_work_while_enforced(
    sso: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org = await configure_sso(sso, identity, container, enforce=True)
    plaintext, _ = container.store.issue(org_id=org.owner.org.id)
    response = await sso.get(SESSION_PROBE_PATH, headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200, response.text
    assert response.json()["org_id"] == str(org.owner.org.id)
