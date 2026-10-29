"""Session-cookie authentication of requests (specs/04-auth-tenancy-rbac.md §1, §4.3, §4.4).

:func:`current_session` resolves the ``va_session`` cookie and, for writes, enforces the CSRF double submit
and the dashboard ``Origin``. :func:`session_principal` turns the session into a :class:`RequestContext` for
its active org: the membership role gives the scopes (§3.3), sessions that have not passed MFA are gated when
the org requires MFA or the user has enabled it, and the org status gates writes like it does for API keys. A
session without an active org falls back to the user's oldest membership and persists it.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Final

from fastapi import Request

from va_api.auth.api_key_auth import ORG_SUSPENDED_MESSAGE, READ_METHODS
from va_api.auth.context import RequestContext
from va_api.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, CookiePolicy
from va_api.auth.csrf import check_csrf, check_origin
from va_api.auth.login_throttle import LoginThrottle
from va_api.auth.mfa_guard import MfaGuard
from va_api.auth.oidc_discovery import OidcDiscovery
from va_api.auth.rbac import scopes_for_role
from va_api.auth.sessions import SESSION_INVALID, ActiveSession, SessionManager
from va_api.auth.sso_state import SsoStateStore
from va_api.auth.verification import ResendLimiter
from va_api.middleware import request_id_of
from va_api.settings import ApiSettings
from va_common.auth import PasswordHasher
from va_common.crypto import Envelope
from va_common.email import EmailSender
from va_common.errors import AppError
from va_common.time import Clock
from va_db.provisioning import OrgProvisioner
from va_db.repositories import IdentityRepo, MembershipLookupRepo, MembershipRecord
from va_db.session import Database

__all__ = [
    "MFA_REQUIRED_MESSAGE",
    "NO_MEMBERSHIP_MESSAGE",
    "ORG_CLOSED_MESSAGE",
    "active_membership",
    "authenticate_session",
    "check_dashboard_origin",
    "client_ip",
    "clock_of",
    "cookie_policy_of",
    "current_session",
    "database_of",
    "email_sender_of",
    "envelope_of",
    "gated_membership",
    "has_session_cookie",
    "login_throttle_of",
    "memberships_of_user",
    "mfa_due",
    "mfa_guard_of",
    "oidc_discovery_of",
    "password_hasher_of",
    "pick_active",
    "provisioner_of",
    "resend_limiter_of",
    "session_principal",
    "sessions_of",
    "settings_of",
    "sso_states_of",
]

MFA_REQUIRED_MESSAGE: Final = "Multi-factor authentication is required for this organization."
NO_MEMBERSHIP_MESSAGE: Final = "You are not a member of any organization."
ORG_CLOSED_MESSAGE: Final = "Organization is closed."
_USER_AGENT: Final = "user-agent"


def settings_of(request: Request) -> ApiSettings:
    settings: ApiSettings = request.app.state.settings
    return settings


def cookie_policy_of(request: Request) -> CookiePolicy:
    return CookiePolicy.from_settings(settings_of(request))


def sessions_of(request: Request) -> SessionManager:
    sessions: SessionManager = request.app.state.container.sessions
    return sessions


def login_throttle_of(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.container.login_throttle
    return throttle


def password_hasher_of(request: Request) -> PasswordHasher:
    hasher: PasswordHasher = request.app.state.container.password_hasher
    return hasher


def database_of(request: Request) -> Database:
    database: Database = request.app.state.container.database
    return database


def clock_of(request: Request) -> Clock:
    clock: Clock = request.app.state.container.clock
    return clock


def provisioner_of(request: Request) -> OrgProvisioner:
    provisioner: OrgProvisioner = request.app.state.container.provisioner
    return provisioner


def email_sender_of(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.container.email_sender
    return sender


def resend_limiter_of(request: Request) -> ResendLimiter:
    limiter: ResendLimiter = request.app.state.container.resend_limiter
    return limiter


def envelope_of(request: Request) -> Envelope:
    envelope: Envelope = request.app.state.container.envelope
    return envelope


def mfa_guard_of(request: Request) -> MfaGuard:
    guard: MfaGuard = request.app.state.container.mfa_guard
    return guard


def oidc_discovery_of(request: Request) -> OidcDiscovery:
    discovery: OidcDiscovery = request.app.state.container.oidc_discovery
    return discovery


def sso_states_of(request: Request) -> SsoStateStore:
    states: SsoStateStore = request.app.state.container.sso_states
    return states


def client_ip(request: Request) -> str | None:
    """The peer address when it is an IP address (test transports report names such as ``testclient``)."""
    host = request.client.host if request.client is not None else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def check_dashboard_origin(request: Request) -> None:
    """Reject a foreign ``Origin``/``Referer`` on a cookie-less auth flow; requests without either pass."""
    check_origin(
        request.headers.get("origin"),
        request.headers.get("referer"),
        settings_of(request).dashboard_base_url,
        required=False,
    )


def has_session_cookie(request: Request) -> bool:
    return bool(request.cookies.get(SESSION_COOKIE))


async def current_session(request: Request) -> ActiveSession:
    """The request's valid session (``AUTH_FAILURE`` otherwise); writes also pass CSRF and Origin checks.

    Stores the session on ``request.state.session``.
    """
    token = request.cookies.get(SESSION_COOKIE)
    active = await sessions_of(request).resolve(token) if token else None
    if active is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    if request.method.upper() not in READ_METHODS:
        check_csrf(request.headers.get(CSRF_HEADER), request.cookies.get(CSRF_COOKIE))
        check_origin(
            request.headers.get("origin"),
            request.headers.get("referer"),
            settings_of(request).dashboard_base_url,
            required=True,
        )
    request.state.session = active
    return active


def pick_active(records: list[MembershipRecord], active_org_id: uuid.UUID | None) -> MembershipRecord | None:
    """The membership in ``active_org_id``, else the oldest one, else ``None``."""
    for record in records:
        if record.org_id == active_org_id:
            return record
    return records[0] if records else None


async def memberships_of_user(request: Request, user_id: uuid.UUID) -> list[MembershipRecord]:
    """The user's memberships, oldest first."""
    # specs/03-data-model.md §2: MembershipLookupRepo is a system-scope repository.
    async with database_of(request).system_session() as session:
        return await MembershipLookupRepo(session).for_user(user_id)


async def active_membership(
    request: Request, active: ActiveSession
) -> tuple[ActiveSession, MembershipRecord | None]:
    """The membership the session acts in; persists a fallback org when the active one is unset or gone."""
    record: MembershipRecord | None = None
    if active.active_org_id is not None:
        # specs/03-data-model.md §2: MembershipLookupRepo is a system-scope repository.
        async with database_of(request).system_session() as session:
            record = await MembershipLookupRepo(session).for_user_in_org(active.user_id, active.active_org_id)
    if record is None:
        record = pick_active(await memberships_of_user(request, active.user_id), active.active_org_id)
        if record is not None:
            active = await sessions_of(request).set_active_org(active, record.org_id)
    return active, record


def mfa_due(*, org_requires: bool, user_enabled: bool, passed: bool) -> bool:
    """Whether a session must pass MFA first: the org requires it or the user enabled it, and it has not."""
    return (org_requires or user_enabled) and not passed


async def _user_mfa_enabled(request: Request, active: ActiveSession) -> bool:
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    return user.mfa_enabled


async def gated_membership(
    request: Request, active: ActiveSession
) -> tuple[ActiveSession, MembershipRecord | None]:
    """:func:`active_membership` after the MFA gate (``MFA_REQUIRED``); a user without memberships passes it
    only when their own MFA is off or verified."""
    active, record = await active_membership(request, active)
    if record is not None:
        org_requires, user_enabled = record.mfa_required, record.user_mfa_enabled
    else:
        org_requires, user_enabled = False, await _user_mfa_enabled(request, active)
    if mfa_due(org_requires=org_requires, user_enabled=user_enabled, passed=active.mfa_passed):
        raise AppError("MFA_REQUIRED", MFA_REQUIRED_MESSAGE)
    return active, record


def _check_org_status(record: MembershipRecord, method: str) -> None:
    if record.org_status == "closed":
        raise AppError("ORG_SUSPENDED", ORG_CLOSED_MESSAGE)
    if record.org_status == "suspended" and method.upper() not in READ_METHODS:
        raise AppError("ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)


async def session_principal(
    request: Request, active: ActiveSession
) -> tuple[RequestContext, MembershipRecord]:
    """The user principal of ``active`` in its active org, after the MFA and org status gates."""
    active, record = await gated_membership(request, active)
    if record is None:
        raise AppError("FORBIDDEN", NO_MEMBERSHIP_MESSAGE)
    _check_org_status(record, request.method)
    context = RequestContext(
        request_id=request_id_of(request),
        org_id=record.org_id,
        principal_type="user",
        principal_id=active.user_id,
        role=record.role,
        scopes=scopes_for_role(record.role),
        environment="live",
        plan_id=record.plan_id,
        ip=client_ip(request),
        user_agent=request.headers.get(_USER_AGENT),
        acting_parent_org_id=None,
    )
    return context, record


async def authenticate_session(request: Request) -> tuple[RequestContext, int | None]:
    """The session principal of the request and its org's ``rate_per_sec`` override."""
    context, record = await session_principal(request, await current_session(request))
    return context, record.rate_per_sec
