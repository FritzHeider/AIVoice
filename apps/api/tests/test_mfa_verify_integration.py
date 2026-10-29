"""MFA verify, the user-or-org MFA gate, the failure lock, recent auth, and disable (specs/04 §4.1-§4.5).

Each test enrols a user through the real routes, then signs in again in a second client (a fresh session that
has not passed MFA). The frozen clock moves 30 s whenever a test needs a code the replay guard has not seen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    MFA_DISABLE,
    MFA_VERIFY,
    PASSWORD,
    SESSION_PROBE_PATH,
    Member,
    SessionContainer,
    enrol_mfa,
    error_of,
    login,
    make_member,
    mfa_code,
    session_app,
    session_client,
    write_headers,
)
from sqlalchemy import delete

from va_api.auth.mfa_guard import MFA_FAILURE_LIMIT, ORG_REQUIRES_MFA_MESSAGE, mfa_failure_key
from va_db.models import Membership
from va_db.repositories import IdentityRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_SESSION = "/internal/auth/session"
_LOGOUT = "/internal/auth/logout"
_LOGOUT_ALL = "/internal/auth/logout-all"
_CHANGE = "/internal/auth/password/change"
_NEW_PASSWORD = "copper-meadow-lighthouse-93"  # noqa: S105 - test password


@pytest.fixture
async def member(identity: IdentityDb, container: SessionContainer) -> Member:
    return await make_member(identity, container.password_hasher)


@asynccontextmanager
async def _signed_in(container: SessionContainer, member: Member) -> AsyncIterator[httpx.AsyncClient]:
    """A second client with a fresh password session of ``member``."""
    async with session_client(session_app(container)) as other:
        await login(other, member.user.email)
        yield other


async def _verify(client: httpx.AsyncClient, **body: str) -> httpx.Response:
    return await client.post(MFA_VERIFY, json=body, headers=write_headers(client))


async def test_enabled_mfa_gates_new_sessions_until_verified(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, codes = await enrol_mfa(client, container)
    async with _signed_in(container, member) as other:
        login_again = await login(other, member.user.email)
        assert login_again.json()["data"]["mfa_required"] is True
        assert error_of(await other.get(SESSION_PROBE_PATH)) == (401, "MFA_REQUIRED", None)
        info = await other.get(_SESSION)
        assert info.status_code == 200
        assert (info.json()["data"]["mfa_required"], info.json()["data"]["mfa_passed"]) == (True, False)
        assert error_of(await other.post(_LOGOUT_ALL, headers=write_headers(other))) == (
            401,
            "MFA_REQUIRED",
            None,
        )

        verified = await _verify(other, code=mfa_code(container, secret))
        assert verified.status_code == 200, verified.text
        assert verified.json()["data"] == {"mfa_passed": True, "recovery_codes_remaining": len(codes)}
        assert (await other.get(SESSION_PROBE_PATH)).status_code == 200
        assert (await other.get(_SESSION)).json()["data"]["mfa_required"] is False
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200


async def test_a_code_is_accepted_once(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, _ = await enrol_mfa(client, container)
    code = mfa_code(container, secret)
    async with _signed_in(container, member) as first:
        assert (await _verify(first, code=code)).status_code == 200
    async with _signed_in(container, member) as second:
        assert error_of(await _verify(second, code=code)) == (401, "INVALID_MFA_CODE", "code")
        container.clock.advance(30)
        assert (await _verify(second, code=mfa_code(container, secret))).status_code == 200


async def test_recovery_codes_verify_once(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    _, codes = await enrol_mfa(client, container)
    typed = codes[0].replace("-", "").upper()
    async with _signed_in(container, member) as other:
        used = await _verify(other, recovery_code=typed)
        assert used.status_code == 200, used.text
        assert used.json()["data"] == {"mfa_passed": True, "recovery_codes_remaining": 9}
        assert (await other.get(SESSION_PROBE_PATH)).status_code == 200
    async with _signed_in(container, member) as again:
        assert error_of(await _verify(again, recovery_code=codes[0])) == (
            401,
            "INVALID_MFA_CODE",
            "recovery_code",
        )
        assert error_of(await _verify(again, recovery_code="not-a-code")) == (
            401,
            "INVALID_MFA_CODE",
            "recovery_code",
        )
    async with identity.database.global_session() as session:
        user = await IdentityRepo(session).get_user(member.user.id)
    assert user is not None
    assert len(user.mfa_recovery_codes_hash) == 9


@pytest.mark.parametrize("body", [{}, {"code": "123456", "recovery_code": "abcd-efgh-ijkl"}])
async def test_verify_needs_exactly_one_kind_of_code(
    member: Member, client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    await login(client, member.user.email)
    response = await client.post(MFA_VERIFY, json=body, headers=write_headers(client))
    assert error_of(response) == (400, "INVALID_INPUT", None)
    assert response.json()["errors"][0]["message"] == "Send either code or recovery_code."


async def test_verify_and_disable_need_enrolment(member: Member, client: httpx.AsyncClient) -> None:
    await login(client, member.user.email)
    for path in (MFA_VERIFY, MFA_DISABLE):
        response = await client.post(path, json={"code": "123456"}, headers=write_headers(client))
        assert error_of(response) == (409, "CONFLICT", None)
        assert response.json()["errors"][0]["message"] == "Multi-factor authentication is not enabled."


async def test_wrong_codes_lock_and_success_clears_the_counter(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, _ = await enrol_mfa(client, container)
    async with _signed_in(container, member) as other:
        for _ in range(MFA_FAILURE_LIMIT - 1):
            assert error_of(await _verify(other, code="000000")) == (401, "INVALID_MFA_CODE", "code")
        assert (await _verify(other, code=mfa_code(container, secret))).status_code == 200
        assert await container.redis.exists(mfa_failure_key(member.user.id)) == 0
        for _ in range(MFA_FAILURE_LIMIT - 1):
            assert error_of(await _verify(other, code="000000"))[1] == "INVALID_MFA_CODE"
        assert error_of(await _verify(other, recovery_code="aaaa-bbbb-cccc"))[1] == "INVALID_MFA_CODE"
        container.clock.advance(30)
        locked = await _verify(other, code=mfa_code(container, secret))
        assert error_of(locked) == (429, "AUTH_LOCKED", None)
        assert locked.headers["Retry-After"] == str(
            await container.redis.ttl(mfa_failure_key(member.user.id))
        )
        assert locked.json()["errors"][0]["message"].startswith("Too many invalid codes; try again in ")


async def test_mfa_verification_refreshes_recent_auth(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, _ = await enrol_mfa(client, container)
    container.clock.advance(timedelta(minutes=30))
    body = {"current_password": PASSWORD, "new_password": _NEW_PASSWORD}
    stale = await client.post(_CHANGE, json=body, headers=write_headers(client))
    assert error_of(stale) == (401, "REAUTH_REQUIRED", None)
    assert (await _verify(client, code=mfa_code(container, secret))).status_code == 200
    changed = await client.post(_CHANGE, json=body, headers=write_headers(client))
    assert changed.status_code == 200, changed.text


async def test_user_without_membership_is_gated_on_auth_routes(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, _ = await enrol_mfa(client, container)
    async with identity.database.system_session() as session:
        await session.execute(delete(Membership).where(Membership.user_id == member.user.id))
    async with _signed_in(container, member) as other:
        body = {"current_password": PASSWORD, "new_password": _NEW_PASSWORD}
        assert error_of(await other.post(_CHANGE, json=body, headers=write_headers(other))) == (
            401,
            "MFA_REQUIRED",
            None,
        )
        assert (await _verify(other, code=mfa_code(container, secret))).status_code == 200
        assert (await other.post(_CHANGE, json=body, headers=write_headers(other))).status_code == 200
        assert (await other.post(_LOGOUT, headers=write_headers(other))).status_code == 200


async def test_disable_is_refused_when_any_org_requires_mfa(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, _ = await enrol_mfa(client, container)
    strict = await make_member(identity, container.password_hasher, org_settings={"mfa_required": True})
    async with identity.database.tenant_session(strict.org.id) as session:
        session.add(Membership(org_id=strict.org.id, user_id=member.user.id, role="viewer"))
    code = mfa_code(container, secret)
    refused = await client.post(MFA_DISABLE, json={"code": code}, headers=write_headers(client))
    assert error_of(refused) == (403, "FORBIDDEN", None)
    assert refused.json()["errors"][0]["message"] == ORG_REQUIRES_MFA_MESSAGE
    assert (await _verify(client, code=code)).status_code == 200


async def test_disable_with_a_totp_code(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    secret, codes = await enrol_mfa(client, container)
    as_code = await client.post(MFA_DISABLE, json={"code": codes[0]}, headers=write_headers(client))
    assert error_of(as_code) == (401, "INVALID_MFA_CODE", "code")
    wrong = await client.post(MFA_DISABLE, json={"code": "000000"}, headers=write_headers(client))
    assert error_of(wrong) == (401, "INVALID_MFA_CODE", "code")
    disabled = await client.post(
        MFA_DISABLE, json={"code": mfa_code(container, secret)}, headers=write_headers(client)
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["data"] == {"mfa_enabled": False}
    async with identity.database.global_session() as session:
        user = await IdentityRepo(session).get_user(member.user.id)
    assert user is not None
    assert (user.mfa_enabled, user.mfa_totp_secret_enc, user.mfa_recovery_codes_hash) == (False, None, [])
    assert (await client.get(_SESSION)).json()["data"]["mfa_passed"] is True
    again = await client.post(MFA_DISABLE, json={"code": "123456"}, headers=write_headers(client))
    assert error_of(again) == (409, "CONFLICT", None)
    async with _signed_in(container, member) as other:
        assert (await other.get(SESSION_PROBE_PATH)).status_code == 200
