"""OIDC single sign-on: ``GET /internal/auth/sso/start`` and ``/sso/callback`` (specs/04 §5).

Both are top-level browser navigations, so they answer 302 redirects only: to the identity provider, to the
dashboard after a login, or to ``{DASHBOARD_BASE_URL}/login?sso_error=<reason>`` on any failure (logged as
``sso_login_failed`` with the reason, never the email or claims). The start stores a single-use state (with
the ``nonce`` and PKCE verifier) in Redis and binds it to the browser with the ``va_sso`` cookie; the callback
consumes it, exchanges the code, validates the ID token, and provisions the user just in time in one tenant
transaction. The pool holds one connection per role, so the provider requests run with no session open and
no two sessions overlap. A login writes ``auth.sso_login`` in the connection's org in that same transaction.
The routes attach to :data:`va_api.routers.auth.internal_auth_router`;
``va_api.main`` imports this module so they exist before the router is included.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import Request
from fastapi.responses import RedirectResponse

from va_api.audit import audit_user, audited
from va_api.auth.cookies import (
    SSO_STATE_COOKIE,
    clear_sso_state_cookie,
    set_sso_state_cookie,
)
from va_api.auth.oidc_client import authorization_url, token_request
from va_api.auth.oidc_discovery import DiscoveryError, ProviderMetadata
from va_api.auth.oidc_tokens import IdTokenClaims, IdTokenError, validate_id_token
from va_api.auth.session_auth import (
    client_ip,
    clock_of,
    cookie_policy_of,
    database_of,
    envelope_of,
    oidc_discovery_of,
    sessions_of,
    settings_of,
    sso_states_of,
)
from va_api.auth.sessions import ActiveSession
from va_api.auth.sso_state import SsoState, SsoStateUnavailableError
from va_api.auth.sso_urls import callback_url, dashboard_home_url, email_domain, login_error_url
from va_api.routers.auth import PUBLIC_MARKER, internal_auth_router, revoke_presented, set_session_cookies
from va_api.routers.org_sso import decrypt_client_secret
from va_common.auth import generate_token, looks_like_token, tokens_equal
from va_common.errors import AppError
from va_common.logging import get_logger
from va_db.models import SsoConnection, User
from va_db.repositories import (
    IdentityRepo,
    MembershipRepo,
    NewUser,
    SsoConnectionLookupRepo,
    SsoConnectionRepo,
)

__all__ = ["MAX_NAME_LENGTH", "REASONS", "SsoLoginError", "sso_callback", "sso_start"]

MAX_NAME_LENGTH: Final = 200
REASONS: Final = frozenset(
    {
        "disabled",
        "invalid_email",
        "not_configured",
        "provider_unavailable",
        "invalid_state",
        "provider_error",
        "invalid_token",
        "email_not_verified",
        "email_domain_mismatch",
        "account_disabled",
    }
)
_REDIRECT: Final = 302
_REDIRECT_DOC: Final[dict[int | str, dict[str, Any]]] = {_REDIRECT: {"description": "Redirect (no body)."}}

_log = get_logger(__name__)


class SsoLoginError(Exception):
    """The login cannot continue; ``reason`` (one of :data:`REASONS`) goes to the dashboard login page."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=_REDIRECT)


def _failed(request: Request, failure: SsoLoginError, step: str) -> RedirectResponse:
    _log.warning("sso_login_failed", step=step, reason=failure.reason, detail=failure.detail)
    return _redirect(login_error_url(settings_of(request), failure.reason))


def _ensure_enabled(request: Request) -> None:
    if not settings_of(request).oidc_enabled:
        raise SsoLoginError("disabled")


async def _connection_for(request: Request, email: str) -> SsoConnection:
    domain = email_domain(email)
    if domain is None:
        raise SsoLoginError("invalid_email")
    # specs/03-data-model.md §2: SsoConnectionLookupRepo is a system-scope repository.
    async with database_of(request).system_session() as session:
        connection = await SsoConnectionLookupRepo(session).for_domain(domain)
    if connection is None:
        raise SsoLoginError("not_configured")
    return connection


async def _metadata(request: Request, issuer: str) -> ProviderMetadata:
    try:
        return await oidc_discovery_of(request).fetch(issuer)
    except DiscoveryError as exc:
        raise SsoLoginError("provider_unavailable", exc.reason) from exc


async def _begin(request: Request, email: str) -> tuple[str, str]:
    """The authorization URL and the state of a new login for ``email``."""
    _ensure_enabled(request)
    connection = await _connection_for(request, email)
    metadata = await _metadata(request, connection.issuer)
    state, nonce, code_verifier = generate_token(), generate_token(), generate_token()
    record = SsoState(
        connection_id=connection.id,
        org_id=connection.org_id,
        nonce=nonce,
        code_verifier=code_verifier,
        issuer=metadata.issuer,
        token_endpoint=metadata.token_endpoint,
        jwks_uri=metadata.jwks_uri,
    )
    try:
        await sso_states_of(request).put(state, record)
    except SsoStateUnavailableError as exc:
        raise SsoLoginError("provider_unavailable", "state store") from exc
    url = authorization_url(
        metadata.authorization_endpoint,
        client_id=connection.client_id,
        redirect_uri=callback_url(settings_of(request)),
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        login_hint=email.strip(),
    )
    return url, state


@internal_auth_router.get(
    "/sso/start",
    operation_id="auth_sso_start",
    status_code=_REDIRECT,
    response_class=RedirectResponse,
    responses=_REDIRECT_DOC,
    openapi_extra=PUBLIC_MARKER,
)
async def sso_start(request: Request, email: str = "") -> RedirectResponse:
    """Redirect to the identity provider of the email's domain (or back to the login page on failure)."""
    try:
        url, state = await _begin(request, email)
    except SsoLoginError as failure:
        return _failed(request, failure, "start")
    response = _redirect(url)
    set_sso_state_cookie(response, cookie_policy_of(request), state)
    return response


async def _take_state(request: Request, state: str) -> SsoState:
    """The login state of ``state``, bound to this browser by the ``va_sso`` cookie; consumed once."""
    cookie = request.cookies.get(SSO_STATE_COOKIE)
    if not looks_like_token(state) or cookie is None or not tokens_equal(state, cookie):
        raise SsoLoginError("invalid_state")
    try:
        record = await sso_states_of(request).take(state)
    except SsoStateUnavailableError as exc:
        raise SsoLoginError("provider_unavailable", "state store") from exc
    if record is None:
        raise SsoLoginError("invalid_state")
    return record


async def _current_connection(request: Request, record: SsoState) -> SsoConnection:
    """The state's connection, still configured and unchanged in identity."""
    async with database_of(request).tenant_session(record.org_id) as session:
        connection = await SsoConnectionRepo(session, record.org_id).get()
    if connection is None or connection.id != record.connection_id:
        raise SsoLoginError("not_configured")
    return connection


async def _exchange(request: Request, connection: SsoConnection, record: SsoState, code: str) -> str:
    """The ID token the provider returns for ``code``."""
    secret = await decrypt_client_secret(envelope_of(request), connection)
    body, headers = token_request(
        client_id=connection.client_id,
        client_secret=secret,
        code=code,
        redirect_uri=callback_url(settings_of(request)),
        code_verifier=record.code_verifier,
    )
    try:
        tokens = await oidc_discovery_of(request).post_form(record.token_endpoint, body, headers)
    except DiscoveryError as exc:
        raise SsoLoginError("provider_error", exc.reason) from exc
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise SsoLoginError("provider_error", "no id_token")
    return id_token


async def _claims(
    request: Request, connection: SsoConnection, record: SsoState, id_token: str
) -> IdTokenClaims:
    try:
        jwks = await oidc_discovery_of(request).fetch_json(record.jwks_uri)
    except DiscoveryError as exc:
        raise SsoLoginError("provider_unavailable", exc.reason) from exc
    try:
        return validate_id_token(
            id_token,
            jwks=jwks,
            issuer=record.issuer,
            client_id=connection.client_id,
            nonce=record.nonce,
            now=clock_of(request).now(),
        )
    except IdTokenError as exc:
        raise SsoLoginError("invalid_token", exc.reason) from exc


def _check_email(claims: IdTokenClaims, connection: SsoConnection) -> None:
    """The email's domain must be one of the connection's; ``email_verified: false`` is refused."""
    domain = email_domain(claims.email)
    if domain is None or domain not in connection.allowed_domains:
        raise SsoLoginError("email_domain_mismatch")
    if claims.email_verified is False:
        raise SsoLoginError("email_not_verified")


async def _user(repo: IdentityRepo, claims: IdTokenClaims, request: Request) -> tuple[User, bool]:
    """The user of the email (created verified and without a password when absent) and whether it is new."""
    now = clock_of(request).now()
    user = await repo.get_user_by_email(claims.email)
    if user is None:
        name = claims.name.strip()[:MAX_NAME_LENGTH]
        created = await repo.create_user(NewUser(email=claims.email, name=name, email_verified_at=now))
        return created, True
    if user.status != "active":
        raise SsoLoginError("account_disabled")
    if user.email_verified_at is None:
        await repo.mark_email_verified(user.id, now)
    return user, False


async def _provision(
    request: Request, connection: SsoConnection, claims: IdTokenClaims
) -> tuple[str, ActiveSession, bool, bool]:
    """One transaction: the user, a membership with the default role when absent, and an MFA-passed
    session in the connection's org. Returns the cookie token, the session, and whether the user and the
    membership were created."""
    org_id = connection.org_id
    try:
        async with database_of(request).tenant_session(org_id) as session:
            user, user_created = await _user(IdentityRepo(session), claims, request)
            memberships = MembershipRepo(session, org_id)
            membership_created = await memberships.get_for_user(user.id) is None
            if membership_created:
                await memberships.add(user.id, connection.default_role)
            token, active = await sessions_of(request).start_in(
                session,
                user_id=user.id,
                active_org_id=org_id,
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                mfa_passed=True,
            )
            await audit_user(
                session,
                request,
                [org_id],
                user.id,
                "auth.sso_login",
                after={
                    "method": "sso",
                    "session_id": active.session_id,
                    "user_created": user_created,
                    "membership_created": membership_created,
                },
            )
    except AppError as exc:
        # A concurrent login created the same user or membership first.
        raise SsoLoginError("provider_unavailable", exc.code) from exc
    return token, active, user_created, membership_created


async def _complete(request: Request, state: str, code: str, error: str) -> tuple[str, ActiveSession]:
    """Finish the login of ``state``: the cookie token and the new session."""
    _ensure_enabled(request)
    record = await _take_state(request, state)
    if error or not code:
        raise SsoLoginError("provider_error", error[:100] or "missing code")
    connection = await _current_connection(request, record)
    id_token = await _exchange(request, connection, record, code)
    claims = await _claims(request, connection, record, id_token)
    _check_email(claims, connection)
    token, active, user_created, membership_created = await _provision(request, connection, claims)
    _log.info(
        "sso_login",
        user_id=str(active.user_id),
        org_id=str(connection.org_id),
        user_created=user_created,
        membership_created=membership_created,
    )
    return token, active


@internal_auth_router.get(
    "/sso/callback",
    operation_id="auth_sso_callback",
    status_code=_REDIRECT,
    response_class=RedirectResponse,
    responses=_REDIRECT_DOC,
    openapi_extra={**PUBLIC_MARKER, **audited("auth.sso_login")},
)
async def sso_callback(
    request: Request, state: str = "", code: str = "", error: str = ""
) -> RedirectResponse:
    """Complete the login and redirect to the dashboard (or back to the login page on failure)."""
    policy = cookie_policy_of(request)
    try:
        token, active = await _complete(request, state, code, error)
    except SsoLoginError as failure:
        response = _failed(request, failure, "callback")
        clear_sso_state_cookie(response, policy)
        return response
    await sessions_of(request).remember(active)
    await revoke_presented(request)
    response = _redirect(dashboard_home_url(settings_of(request)))
    clear_sso_state_cookie(response, policy)
    set_session_cookies(request, response, token)
    return response
