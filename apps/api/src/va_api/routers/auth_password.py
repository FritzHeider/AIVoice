"""Password forgot, reset, and change under ``/internal/auth/password`` (specs/04-auth-tenancy-rbac.md §4.1).

The routes attach to :data:`va_api.routers.auth.internal_auth_router`; ``va_api.main`` imports this module so
they exist before the router is included. ``forgot`` always answers 200 whatever happens, so it reveals
nothing about which addresses have accounts. ``reset`` needs no session (the link may open in another
browser) and revokes every session of the user; ``change`` keeps the caller's session and revokes the others.
"""

from __future__ import annotations

from typing import Literal

from fastapi import BackgroundTasks, Request, Response

from va_api.auth.cookies import clear_auth_cookies
from va_api.auth.password_reset import (
    CURRENT_PASSWORD_MESSAGE,
    RESET_HOURLY_LIMIT,
    RESET_LIMIT_WINDOW,
    RESET_TOKEN_INVALID_MESSAGE,
    require_recent_auth,
    send_password_reset,
)
from va_api.auth.session_auth import (
    active_membership,
    check_dashboard_origin,
    client_ip,
    clock_of,
    cookie_policy_of,
    database_of,
    email_sender_of,
    enforce_mfa,
    login_throttle_of,
    password_hasher_of,
    sessions_of,
    settings_of,
)
from va_api.auth.sessions import SESSION_INVALID, ActiveSession
from va_api.responses import DataResponse
from va_api.routers.auth import PUBLIC_MARKER, CurrentSession, internal_auth_router
from va_api.routers.auth_schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResult,
    ResetPasswordRequest,
    RevokedSessionsResult,
)
from va_common.auth import check_password_policy_async, looks_like_token
from va_common.errors import AppError
from va_common.logging import get_logger
from va_db.models import User
from va_db.repositories import PASSWORD_RESET_TTL, IdentityRepo

__all__ = ["password_change", "password_forgot", "password_reset"]

type SuppressionReason = Literal["unknown_user", "disabled", "limit"]

_log = get_logger(__name__)


def _invalid_reset_token() -> AppError:
    return AppError("TOKEN_EXPIRED", RESET_TOKEN_INVALID_MESSAGE, field="token")


async def _issue_reset(request: Request, email: str) -> tuple[User, str] | None:
    """The user and a new 1 h reset token, or ``None`` (logged with the reason) when no email may go out."""
    now = clock_of(request).now()
    reason: SuppressionReason
    async with database_of(request).global_session() as session:
        repo = IdentityRepo(session)
        user = await repo.get_user_by_email(email)
        if user is None:
            reason = "unknown_user"
        elif user.status != "active":
            reason = "disabled"
        elif await repo.count_issued_since(user.id, "reset_password", now - RESET_LIMIT_WINDOW) >= (
            RESET_HOURLY_LIMIT
        ):
            reason = "limit"
        else:
            issued = await repo.issue_email_token(
                user_id=user.id, purpose="reset_password", now=now, ttl=PASSWORD_RESET_TTL
            )
            return user, issued.plaintext
    _log.info("password_reset_suppressed", reason=reason)
    return None


@internal_auth_router.post(
    "/password/forgot", operation_id="auth_password_forgot", openapi_extra=PUBLIC_MARKER
)
async def password_forgot(
    body: ForgotPasswordRequest, request: Request, background: BackgroundTasks
) -> DataResponse[ForgotPasswordResult]:
    """Email a password reset link when the account exists and is active (3 per hour); always 200."""
    check_dashboard_origin(request)
    issued = await _issue_reset(request, body.email)
    if issued is not None:
        user, token = issued
        background.add_task(
            send_password_reset,
            email_sender_of(request),
            to=user.email,
            name=user.name,
            token=token,
            dashboard_base_url=settings_of(request).dashboard_base_url,
        )
    return DataResponse(data=ForgotPasswordResult(accepted=True))


async def _reset_target(request: Request, token: str) -> User:
    """The active user of a usable reset token, read without using the token up."""
    if not looks_like_token(token):
        raise _invalid_reset_token()
    async with database_of(request).global_session() as session:
        repo = IdentityRepo(session)
        user_id = await repo.peek_email_token(token, "reset_password", clock_of(request).now())
        user = await repo.get_user(user_id) if user_id is not None else None
    if user is None or user.status != "active":
        raise _invalid_reset_token()
    return user


async def _apply_reset(request: Request, token: str, user: User, password_hash: str) -> list[str]:
    """One transaction: use the token, set the password, verify the email, retire the user's other reset
    links, and revoke every session; the revoked sessions' token hashes."""
    now = clock_of(request).now()
    async with database_of(request).global_session() as session:
        repo = IdentityRepo(session)
        if await repo.consume_email_token(token, "reset_password", now) != user.id:
            raise _invalid_reset_token()
        await repo.set_password_hash(user.id, password_hash)
        await repo.mark_email_verified(user.id, now)
        await repo.retire_email_tokens(user.id, "reset_password", now)
        return await repo.revoke_user_sessions(user.id, now)


@internal_auth_router.post("/password/reset", operation_id="auth_password_reset", openapi_extra=PUBLIC_MARKER)
async def password_reset(
    body: ResetPasswordRequest, request: Request, response: Response
) -> DataResponse[RevokedSessionsResult]:
    """Set a new password from a reset link, sign the user out everywhere, and clear the login lock."""
    check_dashboard_origin(request)
    user = await _reset_target(request, body.token)
    await check_password_policy_async(body.password, email=user.email)
    password_hash = await password_hasher_of(request).hash_async(body.password)
    hashes = await _apply_reset(request, body.token, user, password_hash)
    await sessions_of(request).forget(hashes)
    await login_throttle_of(request).clear(user.email)
    clear_auth_cookies(response, cookie_policy_of(request))
    return DataResponse(data=RevokedSessionsResult(revoked_sessions=len(hashes)))


async def _verify_current(request: Request, active: ActiveSession, current_password: str) -> User:
    """The session's user once ``current_password`` matches; failures count toward the login lock."""
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    throttle = login_throttle_of(request)
    ip = client_ip(request)
    await throttle.check(user.email, ip)
    if not await password_hasher_of(request).verify_async(user.password_hash, current_password):
        await throttle.record_failure(user.email, ip)
        raise AppError("INVALID_CREDENTIALS", CURRENT_PASSWORD_MESSAGE, field="current_password")
    await throttle.clear(user.email)
    return user


async def _check_new_password(password: str, email: str) -> None:
    """The §4.2 policy, reported against the ``new_password`` field of the change body."""
    try:
        await check_password_policy_async(password, email=email)
    except AppError as exc:
        raise AppError(exc.code, exc.message, field="new_password") from exc


@internal_auth_router.post(
    "/password/change", operation_id="auth_password_change", openapi_extra=PUBLIC_MARKER
)
async def password_change(
    body: ChangePasswordRequest, request: Request, active: CurrentSession
) -> DataResponse[RevokedSessionsResult]:
    """Change the password of a recently signed-in user; their other sessions are revoked."""
    active, record = await active_membership(request, active)
    enforce_mfa(record, active)
    require_recent_auth(active, clock_of(request).now())
    user = await _verify_current(request, active, body.current_password)
    await _check_new_password(body.new_password, user.email)
    password_hash = await password_hasher_of(request).hash_async(body.new_password)
    now = clock_of(request).now()
    async with database_of(request).global_session() as session:
        repo = IdentityRepo(session)
        await repo.set_password_hash(user.id, password_hash)
        await repo.retire_email_tokens(user.id, "reset_password", now)
        hashes = await repo.revoke_user_sessions(user.id, now, except_session_id=active.session_id)
    await sessions_of(request).forget(hashes)
    return DataResponse(data=RevokedSessionsResult(revoked_sessions=len(hashes)))
