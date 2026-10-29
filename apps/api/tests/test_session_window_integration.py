"""Sliding session windows from the active org's ``session_max_age_days`` against a real database
(specs/04-auth-tenancy-rbac.md §4.3; §7 Decision "Enforcing ip_allowlist and session_max_age_days
(P02-T12c)")."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
from identity_db import IdentityDb
from session_helpers import (
    SessionContainer,
    error_of,
    login,
    make_member,
    write_headers,
)
from sqlalchemy import select

from va_api.auth.cookies import SESSION_COOKIE
from va_api.auth.sessions import SESSION_INVALID, session_cache_key
from va_common import json
from va_common.auth import sha256_hex
from va_db.models import Session
from va_db.repositories import MembershipRepo

ME = "/v1/me"


async def stored_expiry(identity: IdentityDb, user_id: uuid.UUID) -> datetime:
    async with identity.database.system_session() as session:
        rows = list((await session.scalars(select(Session).where(Session.user_id == user_id))).all())
    assert len(rows) == 1
    return rows[0].expires_at


async def cached_expiry(container: SessionContainer, client: httpx.AsyncClient) -> datetime:
    raw = await container.redis.get(session_cache_key(sha256_hex(client.cookies[SESSION_COOKIE])))
    assert raw is not None
    return datetime.fromisoformat(json.loads(raw)["expires_at"])


async def signed_in_with_window(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, days: int | None
) -> uuid.UUID:
    settings = {} if days is None else {"session_max_age_days": days}
    member = await make_member(identity, container.password_hasher, org_settings=settings)
    await login(client, member.user.email)
    return member.user.id


async def test_a_one_day_window_shortens_the_session(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, 1)
    clock = container.clock
    assert await stored_expiry(identity, user_id) == clock.now() + timedelta(days=14)
    clock.advance(timedelta(hours=2))
    assert (await client.get(ME)).status_code == 200
    expected = clock.now() + timedelta(days=1)
    assert await stored_expiry(identity, user_id) == expected
    assert await cached_expiry(container, client) == expected
    clock.advance(timedelta(days=1, seconds=1))
    assert error_of(await client.get(ME)) == (401, "AUTH_FAILURE", None)
    assert (await client.get(ME)).json()["errors"][0]["message"] == SESSION_INVALID


async def test_a_thirty_day_window_outlasts_the_default(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, 30)
    clock = container.clock
    clock.advance(timedelta(hours=2))
    assert (await client.get(ME)).status_code == 200
    assert await stored_expiry(identity, user_id) == clock.now() + timedelta(days=30)
    clock.advance(timedelta(days=29))
    assert (await client.get(ME)).status_code == 200


async def test_an_org_without_the_setting_keeps_fourteen_days(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, None)
    clock = container.clock
    clock.advance(timedelta(hours=2))
    assert (await client.get(ME)).status_code == 200
    assert await stored_expiry(identity, user_id) == clock.now() + timedelta(days=14)


async def test_lowering_the_setting_applies_on_the_next_request(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, None)
    patched = await client.patch(
        "/v1/org/settings", json={"session_max_age_days": 1}, headers=write_headers(client)
    )
    assert patched.status_code == 200, patched.text
    clock = container.clock
    clock.advance(timedelta(minutes=5))
    assert (await client.get(ME)).status_code == 200
    assert await stored_expiry(identity, user_id) == clock.now() + timedelta(days=1)


async def test_switching_into_a_stricter_org_applies_its_window(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, None)
    strict = await make_member(identity, container.password_hasher, org_settings={"session_max_age_days": 1})
    async with identity.database.tenant_session(strict.org.id) as session:
        await MembershipRepo(session, strict.org.id).add(user_id, "developer")
    switched = await client.post(
        "/internal/auth/switch-org", json={"org_id": str(strict.org.id)}, headers=write_headers(client)
    )
    assert switched.status_code == 200, switched.text
    clock = container.clock
    clock.advance(timedelta(minutes=1))
    assert (await client.get(ME)).status_code == 200
    assert await stored_expiry(identity, user_id) == clock.now() + timedelta(days=1)


async def test_the_absolute_cap_still_holds_with_a_ninety_day_window(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    user_id = await signed_in_with_window(client, identity, container, 90)
    clock = container.clock
    created = clock.now()
    clock.advance(timedelta(days=13))
    assert (await client.get(ME)).status_code == 200
    assert await stored_expiry(identity, user_id) == created + timedelta(days=90)
    clock.advance(timedelta(days=77))
    assert error_of(await client.get(ME)) == (401, "AUTH_FAILURE", None)
