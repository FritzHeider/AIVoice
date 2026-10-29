"""TOTP MFA setup, enable, verify, and disable under ``/internal/auth/mfa`` (specs/04 §4.1, §4.5).

The routes attach to :data:`va_api.routers.auth.internal_auth_router`; ``va_api.main`` imports this module so
they exist before the router is included. They are session-authenticated (CSRF and ``Origin``) and exempt from
the MFA gate (§4.3), because they are how a gated session passes it. Every code check goes through the
:class:`va_api.auth.mfa_guard.MfaGuard` failure lock first; a TOTP match must also claim its time step, so a
code is accepted at most once. The secret is decrypted only for the request that checks a code against it.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import Request

from va_api.auth.mfa_guard import (
    ALREADY_ENABLED_MESSAGE,
    NOT_ENABLED_MESSAGE,
    ORG_REQUIRES_MFA_MESSAGE,
    SETUP_CHANGED_MESSAGE,
    SETUP_FIRST_MESSAGE,
    invalid_code,
    mfa_secret_aad,
)
from va_api.auth.session_auth import (
    clock_of,
    database_of,
    envelope_of,
    memberships_of_user,
    mfa_guard_of,
    password_hasher_of,
    sessions_of,
)
from va_api.auth.sessions import SESSION_INVALID, ActiveSession
from va_api.responses import DataResponse
from va_api.routers.auth import PUBLIC_MARKER, CurrentSession, OptionalEmptyBody, internal_auth_router
from va_api.routers.auth_mfa_schemas import (
    MfaCodeRequest,
    MfaDisableResult,
    MfaEnableResult,
    MfaSetupResult,
    MfaVerifyRequest,
    MfaVerifyResult,
)
from va_common.auth import (
    hash_recovery_codes,
    match_recovery_code,
    match_totp,
    new_recovery_codes,
    new_totp_secret,
    otpauth_uri,
    qr_svg,
)
from va_common.errors import AppError
from va_db.models import User
from va_db.repositories import IdentityRepo, MfaRepo

__all__ = ["mfa_disable", "mfa_enable", "mfa_setup", "mfa_verify"]


async def _load_user(request: Request, active: ActiveSession) -> User:
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    return user


async def _decrypt_secret(request: Request, user_id: uuid.UUID, blob: bytes) -> str:
    return await envelope_of(request).platform_decrypt_str(blob, mfa_secret_aad(user_id))


async def _enrolled_secret(request: Request, user: User) -> str:
    """The TOTP secret of an enrolled user; ``CONFLICT`` when MFA is not enabled."""
    if not user.mfa_enabled or user.mfa_totp_secret_enc is None:
        raise AppError("CONFLICT", NOT_ENABLED_MESSAGE)
    return await _decrypt_secret(request, user.id, user.mfa_totp_secret_enc)


async def _check_totp(request: Request, user_id: uuid.UUID, secret: str, code: str) -> None:
    """Accept ``code`` once (failure lock, ±1 step match, replay guard); ``INVALID_MFA_CODE`` otherwise."""
    guard = mfa_guard_of(request)
    await guard.check(user_id)
    counter = match_totp(secret, code, clock_of(request).now())
    if counter is None or not await guard.claim_step(user_id, counter):
        await guard.record_failure(user_id)
        raise invalid_code("code")
    await guard.clear(user_id)


async def _spend_recovery_code(request: Request, user: User, raw: str) -> int:
    """Use up the recovery code ``raw`` matches; how many codes remain. ``INVALID_MFA_CODE`` otherwise."""
    guard = mfa_guard_of(request)
    await guard.check(user.id)
    hashes = list(user.mfa_recovery_codes_hash)
    matched = await match_recovery_code(password_hasher_of(request), hashes, raw)
    spent = False
    if matched is not None:
        async with database_of(request).global_session() as session:
            spent = await MfaRepo(session).consume_recovery_code(user.id, matched)
    if not spent:
        await guard.record_failure(user.id)
        raise invalid_code("recovery_code")
    await guard.clear(user.id)
    return len(hashes) - 1


@internal_auth_router.post("/mfa/setup", operation_id="auth_mfa_setup", openapi_extra=PUBLIC_MARKER)
async def mfa_setup(
    request: Request, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[MfaSetupResult]:
    """Start (or restart) enrolment: a new pending TOTP secret, stored encrypted, MFA still off."""
    user = await _load_user(request, active)
    if user.mfa_enabled:
        raise AppError("CONFLICT", ALREADY_ENABLED_MESSAGE)
    secret = new_totp_secret()
    blob = await envelope_of(request).platform_encrypt_str(secret, mfa_secret_aad(user.id))
    async with database_of(request).global_session() as session:
        stored = await MfaRepo(session).begin_setup(user.id, blob)
    if not stored:
        raise AppError("CONFLICT", ALREADY_ENABLED_MESSAGE)
    uri = otpauth_uri(secret, user.email)
    svg = await asyncio.to_thread(qr_svg, uri)
    return DataResponse(data=MfaSetupResult(secret_base32=secret, otpauth_uri=uri, qr_svg=svg))


@internal_auth_router.post("/mfa/enable", operation_id="auth_mfa_enable", openapi_extra=PUBLIC_MARKER)
async def mfa_enable(
    body: MfaCodeRequest, request: Request, active: CurrentSession
) -> DataResponse[MfaEnableResult]:
    """Turn MFA on with a code from the pending secret; returns 10 recovery codes (shown once)."""
    user = await _load_user(request, active)
    if user.mfa_enabled:
        raise AppError("CONFLICT", ALREADY_ENABLED_MESSAGE)
    blob = user.mfa_totp_secret_enc
    if blob is None:
        raise AppError("CONFLICT", SETUP_FIRST_MESSAGE)
    await _check_totp(request, user.id, await _decrypt_secret(request, user.id, blob), body.code)
    codes = new_recovery_codes()
    hashes = await hash_recovery_codes(password_hasher_of(request), codes)
    async with database_of(request).global_session() as session:
        enabled = await MfaRepo(session).enable(user.id, blob, hashes)
    if not enabled:
        raise AppError("CONFLICT", SETUP_CHANGED_MESSAGE)
    await sessions_of(request).mark_mfa_passed(active)
    return DataResponse(data=MfaEnableResult(mfa_enabled=True, recovery_codes=codes))


@internal_auth_router.post("/mfa/verify", operation_id="auth_mfa_verify", openapi_extra=PUBLIC_MARKER)
async def mfa_verify(
    body: MfaVerifyRequest, request: Request, active: CurrentSession
) -> DataResponse[MfaVerifyResult]:
    """Pass MFA for the current session with a TOTP code or a recovery code (which is used up)."""
    user = await _load_user(request, active)
    secret = await _enrolled_secret(request, user)
    if body.recovery_code is not None:
        remaining = await _spend_recovery_code(request, user, body.recovery_code)
    else:
        await _check_totp(request, user.id, secret, body.code or "")
        remaining = len(user.mfa_recovery_codes_hash)
    await sessions_of(request).mark_mfa_passed(active)
    return DataResponse(data=MfaVerifyResult(mfa_passed=True, recovery_codes_remaining=remaining))


@internal_auth_router.post("/mfa/disable", operation_id="auth_mfa_disable", openapi_extra=PUBLIC_MARKER)
async def mfa_disable(
    body: MfaCodeRequest, request: Request, active: CurrentSession
) -> DataResponse[MfaDisableResult]:
    """Turn MFA off with a TOTP code, unless any of the user's orgs requires MFA."""
    user = await _load_user(request, active)
    secret = await _enrolled_secret(request, user)
    if any(record.mfa_required for record in await memberships_of_user(request, user.id)):
        raise AppError("FORBIDDEN", ORG_REQUIRES_MFA_MESSAGE)
    await _check_totp(request, user.id, secret, body.code)
    async with database_of(request).global_session() as session:
        disabled = await MfaRepo(session).disable(user.id)
    if not disabled:
        raise AppError("CONFLICT", NOT_ENABLED_MESSAGE)
    await mfa_guard_of(request).forget_steps(user.id)
    return DataResponse(data=MfaDisableResult(mfa_enabled=False))
