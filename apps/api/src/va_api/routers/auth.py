"""Dashboard session endpoints under ``/internal/auth`` (specs/04-auth-tenancy-rbac.md §4.1 to §4.4).

Envelope format, excluded from the public OpenAPI document (specs/26 §1), and marked
``x-public: session-flow``: they authenticate with the session cookie themselves instead of ``require_scope``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import Body, Depends, Request, Response

from va_api.auth.cookies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_auth_cookies,
    set_csrf_cookie,
    set_session_cookie,
)
from va_api.auth.csrf import check_origin
from va_api.auth.session_auth import (
    active_membership,
    client_ip,
    cookie_policy_of,
    current_session,
    database_of,
    enforce_mfa,
    login_throttle_of,
    memberships_of_user,
    password_hasher_of,
    pick_active,
    sessions_of,
    settings_of,
)
from va_api.auth.sessions import SESSION_INVALID, ActiveSession
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.auth_schemas import (
    EmptyBody,
    LoginRequest,
    LoginResult,
    LogoutResult,
    SessionInfo,
    SessionMembership,
    SessionUser,
)
from va_common.auth import generate_token, looks_like_token
from va_common.errors import AppError
from va_db.models import User
from va_db.repositories import IdentityRepo

__all__ = ["INVALID_CREDENTIALS_MESSAGE", "PUBLIC_MARKER", "internal_auth_router"]

INVALID_CREDENTIALS_MESSAGE: Final = "Invalid email or password."
PUBLIC_MARKER: Final[dict[str, Any]] = {"x-public": "session-flow"}

internal_auth_router = envelope_router(prefix="/internal/auth", tags=["Auth"], include_in_schema=False)

type OptionalEmptyBody = Annotated[EmptyBody | None, Body()]
type CurrentSession = Annotated[ActiveSession, Depends(current_session)]


async def _authenticate(request: Request, body: LoginRequest, ip: str | None) -> User:
    """The active user whose password matches; ``INVALID_CREDENTIALS`` (counted as a failure) otherwise.

    One argon2 verification runs whether or not the user exists or has a password. An outdated hash is
    replaced after a successful verification.
    """
    database = database_of(request)
    async with database.global_session() as session:
        user = await IdentityRepo(session).get_user_by_email(body.email)
    hasher = password_hasher_of(request)
    password_hash = user.password_hash if user is not None else None
    verified = await hasher.verify_async(password_hash, body.password)
    if user is None or password_hash is None or not verified or user.status != "active":
        await login_throttle_of(request).record_failure(body.email, ip)
        raise AppError("INVALID_CREDENTIALS", INVALID_CREDENTIALS_MESSAGE)
    if hasher.needs_rehash(password_hash):
        new_hash = await hasher.hash_async(body.password)
        async with database.global_session() as session:
            await IdentityRepo(session).set_password_hash(user.id, new_hash)
    return user


async def _issue(request: Request, response: Response, user: User, ip: str | None) -> LoginResult:
    """Start a fresh session (revoking the one presented, if any) and set both cookies."""
    sessions = sessions_of(request)
    presented = request.cookies.get(SESSION_COOKIE)
    previous = await sessions.resolve(presented) if presented else None
    if previous is not None:
        await sessions.revoke(previous)
    records = await memberships_of_user(request, user.id)
    token, _ = await sessions.start(
        user_id=user.id,
        active_org_id=records[0].org_id if records else None,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    policy = cookie_policy_of(request)
    set_session_cookie(response, policy, token)
    set_csrf_cookie(response, policy, generate_token())
    return LoginResult(mfa_required=user.mfa_enabled, user=SessionUser.of(user))


@internal_auth_router.post("/login", operation_id="auth_login", openapi_extra=PUBLIC_MARKER)
async def login(body: LoginRequest, request: Request, response: Response) -> DataResponse[LoginResult]:
    """Password login: throttled per email and IP, rejects foreign origins, issues a new session."""
    check_origin(
        request.headers.get("origin"),
        request.headers.get("referer"),
        settings_of(request).dashboard_base_url,
        required=False,
    )
    ip = client_ip(request)
    throttle = login_throttle_of(request)
    await throttle.check(body.email, ip)
    user = await _authenticate(request, body, ip)
    await throttle.clear(body.email)
    return DataResponse(data=await _issue(request, response, user, ip))


@internal_auth_router.post("/logout", operation_id="auth_logout", openapi_extra=PUBLIC_MARKER)
async def logout(
    request: Request, response: Response, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[LogoutResult]:
    """Revoke the current session and clear the cookies."""
    revoked = await sessions_of(request).revoke(active)
    clear_auth_cookies(response, cookie_policy_of(request))
    return DataResponse(data=LogoutResult(revoked_sessions=revoked))


@internal_auth_router.post("/logout-all", operation_id="auth_logout_all", openapi_extra=PUBLIC_MARKER)
async def logout_all(
    request: Request, response: Response, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[LogoutResult]:
    """Revoke every session of the user (MFA-gated like any other endpoint) and clear the cookies."""
    active, record = await active_membership(request, active)
    enforce_mfa(record, active)
    revoked = await sessions_of(request).revoke_all(active.user_id)
    clear_auth_cookies(response, cookie_policy_of(request))
    return DataResponse(data=LogoutResult(revoked_sessions=revoked))


@internal_auth_router.get("/session", operation_id="auth_session_get", openapi_extra=PUBLIC_MARKER)
async def get_session(
    request: Request, response: Response, active: CurrentSession
) -> DataResponse[SessionInfo]:
    """The signed-in user, their memberships, the active org, and the CSRF token (cookie set or reused)."""
    records = await memberships_of_user(request, active.user_id)
    record = pick_active(records, active.active_org_id)
    if record is not None and record.org_id != active.active_org_id:
        active = await sessions_of(request).set_active_org(active, record.org_id)
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    csrf = request.cookies.get(CSRF_COOKIE)
    if csrf is None or not looks_like_token(csrf):
        csrf = generate_token()
    set_csrf_cookie(response, cookie_policy_of(request), csrf)
    info = SessionInfo(
        user=SessionUser.of(user),
        memberships=[SessionMembership.of(item) for item in records],
        active_org_id=record.org_id if record is not None else None,
        mfa_passed=active.mfa_passed,
        mfa_required=record is not None and record.mfa_required and not active.mfa_passed,
        csrf_token=csrf,
        expires_at=active.expires_at,
    )
    return DataResponse(data=info)
