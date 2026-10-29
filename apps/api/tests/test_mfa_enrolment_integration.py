"""MFA setup and enable through the real app on a migrated Postgres (specs/04-auth-tenancy-rbac.md §4.5).

A frozen clock drives the TOTP codes; fake Redis holds the failure counter and the replay guard.
"""

from __future__ import annotations

import re

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    MFA_DISABLE,
    MFA_ENABLE,
    MFA_SETUP,
    MFA_VERIFY,
    SESSION_PROBE_PATH,
    Member,
    SessionContainer,
    enrol_mfa,
    error_of,
    login,
    make_member,
    mfa_code,
    write_headers,
)

from va_api.auth.mfa_guard import mfa_secret_aad
from va_common.auth import TOTP_SECRET_LENGTH, normalise_recovery_code, totp_code, totp_counter
from va_db.models import User
from va_db.repositories import IdentityRepo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_SESSION = "/internal/auth/session"
_RECOVERY_CODE_RE = re.compile(r"[a-z2-7]{4}-[a-z2-7]{4}-[a-z2-7]{4}")


@pytest.fixture
async def member(identity: IdentityDb, container: SessionContainer) -> Member:
    return await make_member(identity, container.password_hasher)


async def _user(identity: IdentityDb, member: Member) -> User:
    async with identity.database.global_session() as session:
        user = await IdentityRepo(session).get_user(member.user.id)
    assert user is not None
    return user


async def _setup(client: httpx.AsyncClient) -> str:
    response = await client.post(MFA_SETUP, json={}, headers=write_headers(client))
    assert response.status_code == 200, response.text
    secret: str = response.json()["data"]["secret_base32"]
    return secret


async def test_setup_stores_an_encrypted_pending_secret(
    identity: IdentityDb, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    response = await client.post(MFA_SETUP, headers=write_headers(client))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    secret = data["secret_base32"]
    assert re.fullmatch(r"[A-Z2-7]+", secret) is not None
    assert len(secret) == TOTP_SECRET_LENGTH
    assert data["otpauth_uri"].startswith("otpauth://totp/VoiceAgents:")
    assert f"secret={secret}" in data["otpauth_uri"]
    assert member.user.email.split("@")[0] in data["otpauth_uri"]
    assert data["qr_svg"].startswith("<svg")
    user = await _user(identity, member)
    assert user.mfa_enabled is False
    assert user.mfa_totp_secret_enc is not None
    assert secret.encode() not in user.mfa_totp_secret_enc
    plaintext = await identity.envelope.platform_decrypt_str(
        user.mfa_totp_secret_enc, mfa_secret_aad(user.id)
    )
    assert plaintext == secret

    replacement = await _setup(client)
    assert replacement != secret
    stored = (await _user(identity, member)).mfa_totp_secret_enc
    assert stored is not None
    assert await identity.envelope.platform_decrypt_str(stored, mfa_secret_aad(user.id)) == replacement


async def test_enable_returns_recovery_codes_and_passes_the_session(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    no_setup = await client.post(MFA_ENABLE, json={"code": "123456"}, headers=write_headers(client))
    assert error_of(no_setup) == (409, "CONFLICT", None)
    assert no_setup.json()["errors"][0]["message"] == "Start multi-factor setup first."

    secret = await _setup(client)
    wrong = totp_code(secret, totp_counter(container.clock.now()) + 5)
    rejected = await client.post(MFA_ENABLE, json={"code": wrong}, headers=write_headers(client))
    assert error_of(rejected) == (401, "INVALID_MFA_CODE", "code")
    assert rejected.json()["errors"][0]["message"] == "Invalid or expired code."

    enabled = await client.post(
        MFA_ENABLE, json={"code": mfa_code(container, secret)}, headers=write_headers(client)
    )
    assert enabled.status_code == 200, enabled.text
    data = enabled.json()["data"]
    assert data["mfa_enabled"] is True
    codes = data["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10
    assert all(_RECOVERY_CODE_RE.fullmatch(code) for code in codes)
    user = await _user(identity, member)
    assert user.mfa_enabled is True
    assert len(user.mfa_recovery_codes_hash) == 10
    assert not set(codes) & set(user.mfa_recovery_codes_hash)

    info = (await client.get(_SESSION)).json()["data"]
    assert (info["mfa_passed"], info["mfa_required"], info["user"]["mfa_enabled"]) == (True, False, True)
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200

    again = await client.post(
        MFA_ENABLE, json={"code": mfa_code(container, secret)}, headers=write_headers(client)
    )
    assert error_of(again) == (409, "CONFLICT", None)
    assert again.json()["errors"][0]["message"] == "Multi-factor authentication is already enabled."
    resetup = await client.post(MFA_SETUP, headers=write_headers(client))
    assert error_of(resetup) == (409, "CONFLICT", None)


async def test_enable_checks_the_current_pending_secret(
    container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    first = await _setup(client)
    await _setup(client)
    stale = await client.post(
        MFA_ENABLE, json={"code": mfa_code(container, first)}, headers=write_headers(client)
    )
    assert error_of(stale) == (401, "INVALID_MFA_CODE", "code")


async def test_recovery_codes_are_stored_in_canonical_form(
    identity: IdentityDb, container: SessionContainer, member: Member, client: httpx.AsyncClient
) -> None:
    await login(client, member.user.email)
    _, codes = await enrol_mfa(client, container)
    assert all(normalise_recovery_code(code) == code for code in codes)
    hashes = (await _user(identity, member)).mfa_recovery_codes_hash
    assert container.password_hasher.verify(hashes[0], codes[0])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (MFA_SETUP, {"secret": "x"}),
        (MFA_ENABLE, {"code": "123456", "remember": True}),
        (MFA_ENABLE, {}),
        (MFA_ENABLE, {"code": ""}),
        (MFA_VERIFY, {"code": "123456", "trust_device": True}),
        (MFA_DISABLE, {"code": "1" * 33}),
    ],
)
async def test_bodies_are_validated(
    member: Member, client: httpx.AsyncClient, path: str, body: dict[str, object]
) -> None:
    await login(client, member.user.email)
    status, code, _ = error_of(await client.post(path, json=body, headers=write_headers(client)))
    assert (status, code) == (400, "INVALID_INPUT")


@pytest.mark.parametrize("path", [MFA_SETUP, MFA_ENABLE, MFA_VERIFY, MFA_DISABLE])
async def test_routes_need_a_session_csrf_and_origin(
    member: Member, client: httpx.AsyncClient, path: str
) -> None:
    body = {} if path == MFA_SETUP else {"code": "123456"}
    assert error_of(await client.post(path, json=body)) == (401, "AUTH_FAILURE", None)
    await login(client, member.user.email)
    no_csrf = await client.post(path, json=body, headers={"Origin": "https://app.voiceagents.test"})
    assert error_of(no_csrf) == (403, "FORBIDDEN", None)
    no_origin = await client.post(path, json=body, headers=write_headers(client, origin=None))
    assert error_of(no_origin) == (403, "FORBIDDEN", None)


async def test_setup_and_enable_are_reachable_while_the_org_gate_applies(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    member = await make_member(identity, container.password_hasher, org_settings={"mfa_required": True})
    await login(client, member.user.email)
    assert error_of(await client.get(SESSION_PROBE_PATH)) == (401, "MFA_REQUIRED", None)
    await enrol_mfa(client, container)
    assert (await client.get(SESSION_PROBE_PATH)).status_code == 200
