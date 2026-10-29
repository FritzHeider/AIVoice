"""Session-cookie authentication of requests (specs/04-auth-tenancy-rbac.md §1, §4.3, §4.4).

:func:`current_session` resolves the ``va_session`` cookie and, for writes, enforces the CSRF double submit
and the dashboard ``Origin``. :func:`session_principal` turns the session into a :class:`RequestContext` for
its active org: the membership role gives the scopes (§3.3), the org's ``mfa_required`` setting gates sessions
that have not passed MFA, and the org status gates writes like it does for API keys. A session without an
active org falls back to the user's oldest membership and persists it.
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
from va_api.auth.rbac import scopes_for_role
from va_api.auth.sessions import SESSION_INVALID, ActiveSession, SessionManager
from va_api.middleware import request_id_of
from va_api.settings import ApiSettings
from va_common.auth import PasswordHasher
from va_common.errors import AppError
from va_db.repositories import MembershipLookupRepo, MembershipRecord
from va_db.session import Database

__all__ = [
    "MFA_REQUIRED_MESSAGE",
    "NO_MEMBERSHIP_MESSAGE",
    "ORG_CLOSED_MESSAGE",
    "active_membership",
    "authenticate_session",
    "client_ip",
    "cookie_policy_of",
    "current_session",
    "database_of",
    "enforce_mfa",
    "has_session_cookie",
    "login_throttle_of",
    "memberships_of_user",
    "password_hasher_of",
    "pick_active",
    "session_principal",
    "sessions_of",
    "settings_of",
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


def client_ip(request: Request) -> str | None:
    """The peer address when it is an IP address (test transports report names such as ``testclient``)."""
    host = request.client.host if request.client is not None else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


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


def enforce_mfa(record: MembershipRecord | None, active: ActiveSession) -> None:
    """``MFA_REQUIRED`` when the org requires MFA and the session has not passed it."""
    if record is not None and record.mfa_required and not active.mfa_passed:
        raise AppError("MFA_REQUIRED", MFA_REQUIRED_MESSAGE)


def _check_org_status(record: MembershipRecord, method: str) -> None:
    if record.org_status == "closed":
        raise AppError("ORG_SUSPENDED", ORG_CLOSED_MESSAGE)
    if record.org_status == "suspended" and method.upper() not in READ_METHODS:
        raise AppError("ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)


async def session_principal(
    request: Request, active: ActiveSession
) -> tuple[RequestContext, MembershipRecord]:
    """The user principal of ``active`` in its active org, after the MFA and org status gates."""
    active, record = await active_membership(request, active)
    if record is None:
        raise AppError("FORBIDDEN", NO_MEMBERSHIP_MESSAGE)
    enforce_mfa(record, active)
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
