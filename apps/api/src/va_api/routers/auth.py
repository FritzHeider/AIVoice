"""Dashboard session endpoints under ``/internal/auth`` (specs/04-auth-tenancy-rbac.md §4.1 to §4.4).

Envelope format, excluded from the public OpenAPI document and emitted to ``openapi/internal.json`` (specs/26
§1), and marked ``x-public: session-flow``: they authenticate with the session cookie themselves instead of
``require_scope``. Signup and resend send the verification email after the response (a background task).
The password routes live in :mod:`va_api.routers.auth_password` and attach to the same router.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import BackgroundTasks, Body, Depends, Request, Response

from va_api.auth.cookies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_auth_cookies,
    set_csrf_cookie,
    set_session_cookie,
)
from va_api.auth.session_auth import (
    active_membership,
    check_dashboard_origin,
    client_ip,
    clock_of,
    cookie_policy_of,
    current_session,
    database_of,
    email_sender_of,
    enforce_mfa,
    login_throttle_of,
    memberships_of_user,
    password_hasher_of,
    pick_active,
    provisioner_of,
    resend_limiter_of,
    sessions_of,
    settings_of,
)
from va_api.auth.sessions import SESSION_INVALID, ActiveSession
from va_api.auth.verification import TOKEN_INVALID_MESSAGE, send_verification
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.auth_schemas import (
    EmptyBody,
    LoginRequest,
    LoginResult,
    ResendResult,
    RevokedSessionsResult,
    SessionInfo,
    SessionMembership,
    SessionUser,
    SignupOrg,
    SignupRequest,
    SignupResult,
    VerifyEmailRequest,
    VerifyEmailResult,
)
from va_common.auth import check_password_policy_async, generate_token, looks_like_token
from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import User
from va_db.provisioning import ProvisionedOrg
from va_db.repositories import EMAIL_VERIFY_TTL, IdentityRepo, NewUser

__all__ = ["INVALID_CREDENTIALS_MESSAGE", "PUBLIC_MARKER", "CurrentSession", "internal_auth_router"]

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


async def _revoke_presented(request: Request) -> None:
    """Revoke the session of the ``va_session`` cookie the request carries, if valid (fixation defence)."""
    sessions = sessions_of(request)
    presented = request.cookies.get(SESSION_COOKIE)
    previous = await sessions.resolve(presented) if presented else None
    if previous is not None:
        await sessions.revoke(previous)


def _set_session_cookies(request: Request, response: Response, token: str) -> None:
    policy = cookie_policy_of(request)
    set_session_cookie(response, policy, token)
    set_csrf_cookie(response, policy, generate_token())


async def _issue(request: Request, response: Response, user: User, ip: str | None) -> LoginResult:
    """Start a fresh session (revoking the one presented, if any) and set both cookies."""
    await _revoke_presented(request)
    records = await memberships_of_user(request, user.id)
    token, _ = await sessions_of(request).start(
        user_id=user.id,
        active_org_id=records[0].org_id if records else None,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookies(request, response, token)
    return LoginResult(mfa_required=user.mfa_enabled, user=SessionUser.of(user))


def _owner_of(provisioned: ProvisionedOrg) -> User:
    if provisioned.owner is None:
        msg = "signup provisioning returned no owner"
        raise RuntimeError(msg)
    return provisioned.owner


async def _create_account(
    request: Request, body: SignupRequest, password_hash: str, ip: str | None
) -> tuple[str, ActiveSession, SignupResult, str]:
    """One transaction: the org with its owner, the owner's session, and a verification token.

    Returns the cookie token, the session, the response body, and the verification token.
    """
    org_id = new_id()
    now = clock_of(request).now()
    owner = NewUser(email=body.email, name=body.name, password_hash=password_hash)
    async with database_of(request).tenant_session(org_id) as session:
        provisioned = await provisioner_of(request).provision_in(
            session, org_id, name=body.org_name, owner=owner
        )
        user = _owner_of(provisioned)
        token, active = await sessions_of(request).start_in(
            session,
            user_id=user.id,
            active_org_id=org_id,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
        issued = await IdentityRepo(session).issue_email_token(
            user_id=user.id, purpose="verify_email", now=now, ttl=EMAIL_VERIFY_TTL
        )
        result = SignupResult(user=SessionUser.of(user), org=SignupOrg.of(provisioned.org))
    return token, active, result, issued.plaintext


def _queue_verification(
    request: Request, background: BackgroundTasks, *, to: str, name: str, token: str
) -> None:
    background.add_task(
        send_verification,
        email_sender_of(request),
        to=to,
        name=name,
        token=token,
        dashboard_base_url=settings_of(request).dashboard_base_url,
    )


@internal_auth_router.post(
    "/signup", operation_id="auth_signup", status_code=201, openapi_extra=PUBLIC_MARKER
)
async def signup(
    body: SignupRequest, request: Request, response: Response, background: BackgroundTasks
) -> DataResponse[SignupResult]:
    """Create the owner user (unverified), their org, and a session; email the verification link."""
    check_dashboard_origin(request)
    await check_password_policy_async(body.password, email=body.email)
    password_hash = await password_hasher_of(request).hash_async(body.password)
    token, active, result, email_token = await _create_account(
        request, body, password_hash, client_ip(request)
    )
    await sessions_of(request).remember(active)
    await _revoke_presented(request)
    _set_session_cookies(request, response, token)
    _queue_verification(request, background, to=result.user.email, name=result.user.name, token=email_token)
    return DataResponse(data=result)


@internal_auth_router.post("/verify-email", operation_id="auth_verify_email", openapi_extra=PUBLIC_MARKER)
async def verify_email(body: VerifyEmailRequest, request: Request) -> DataResponse[VerifyEmailResult]:
    """Consume a verification token (no session needed) and mark the user's email verified."""
    check_dashboard_origin(request)
    if not looks_like_token(body.token):
        raise AppError("TOKEN_EXPIRED", TOKEN_INVALID_MESSAGE, field="token")
    now = clock_of(request).now()
    async with database_of(request).global_session() as session:
        repo = IdentityRepo(session)
        user_id = await repo.consume_email_token(body.token, "verify_email", now)
        user = await repo.get_user(user_id) if user_id is not None else None
        if user is None:
            raise AppError("TOKEN_EXPIRED", TOKEN_INVALID_MESSAGE, field="token")
        await repo.mark_email_verified(user.id, now)
        await session.refresh(user)
        result = VerifyEmailResult(user=SessionUser.of(user))
    return DataResponse(data=result)


@internal_auth_router.post(
    "/resend-verification", operation_id="auth_resend_verification", openapi_extra=PUBLIC_MARKER
)
async def resend_verification(
    request: Request, active: CurrentSession, background: BackgroundTasks, _body: OptionalEmptyBody = None
) -> DataResponse[ResendResult]:
    """Email a new verification link (3 per hour); nothing is sent once the email is verified."""
    active, record = await active_membership(request, active)
    enforce_mfa(record, active)
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    if user.email_verified_at is not None:
        return DataResponse(data=ResendResult(sent=False, email_verified=True))
    await resend_limiter_of(request).hit(user.id)
    async with database_of(request).global_session() as session:
        issued = await IdentityRepo(session).issue_email_token(
            user_id=user.id, purpose="verify_email", now=clock_of(request).now(), ttl=EMAIL_VERIFY_TTL
        )
    _queue_verification(request, background, to=user.email, name=user.name, token=issued.plaintext)
    return DataResponse(data=ResendResult(sent=True, email_verified=False))


@internal_auth_router.post("/login", operation_id="auth_login", openapi_extra=PUBLIC_MARKER)
async def login(body: LoginRequest, request: Request, response: Response) -> DataResponse[LoginResult]:
    """Password login: throttled per email and IP, rejects foreign origins, issues a new session."""
    check_dashboard_origin(request)
    ip = client_ip(request)
    throttle = login_throttle_of(request)
    await throttle.check(body.email, ip)
    user = await _authenticate(request, body, ip)
    await throttle.clear(body.email)
    return DataResponse(data=await _issue(request, response, user, ip))


@internal_auth_router.post("/logout", operation_id="auth_logout", openapi_extra=PUBLIC_MARKER)
async def logout(
    request: Request, response: Response, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[RevokedSessionsResult]:
    """Revoke the current session and clear the cookies."""
    revoked = await sessions_of(request).revoke(active)
    clear_auth_cookies(response, cookie_policy_of(request))
    return DataResponse(data=RevokedSessionsResult(revoked_sessions=revoked))


@internal_auth_router.post("/logout-all", operation_id="auth_logout_all", openapi_extra=PUBLIC_MARKER)
async def logout_all(
    request: Request, response: Response, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[RevokedSessionsResult]:
    """Revoke every session of the user (MFA-gated like any other endpoint) and clear the cookies."""
    active, record = await active_membership(request, active)
    enforce_mfa(record, active)
    revoked = await sessions_of(request).revoke_all(active.user_id)
    clear_auth_cookies(response, cookie_policy_of(request))
    return DataResponse(data=RevokedSessionsResult(revoked_sessions=revoked))


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
