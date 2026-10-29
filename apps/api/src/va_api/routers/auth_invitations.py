"""Invitation details and acceptance under ``/internal/auth/invitations`` (specs/04 §4.1, §6).

The routes attach to :data:`va_api.routers.auth.internal_auth_router`; ``va_api.main`` imports this module so
they exist before the router is included. Both know only the invitation token, so the invitation is looked up
through the system-scope :class:`va_db.repositories.InvitationLookupRepo` first. Accepting works in three
ways: a signed-in user whose email is the invited one joins the org (which becomes the session's active org);
an invited email that already has an account must sign in first; otherwise ``name`` and ``password`` create
the account (verified, since the link proves the address) and sign it in. Every success consumes the
invitation and adds the membership in one transaction of the invitation's org, so a failure leaves the link
usable.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.api_key_auth import ORG_SUSPENDED_MESSAGE
from va_api.auth.invitations import ALREADY_MEMBER_MESSAGE, INVITATION_INVALID_MESSAGE
from va_api.auth.session_auth import (
    MFA_REQUIRED_MESSAGE,
    ORG_CLOSED_MESSAGE,
    check_dashboard_origin,
    client_ip,
    clock_of,
    database_of,
    optional_session,
    password_hasher_of,
    sessions_of,
)
from va_api.auth.sessions import SESSION_INVALID, ActiveSession
from va_api.responses import DataResponse
from va_api.routers.auth import PUBLIC_MARKER, check_sso_enforced, internal_auth_router, set_session_cookies
from va_api.routers.auth_invitations_schemas import (
    AcceptInvitationRequest,
    InvitationAccepted,
    InvitationDetails,
)
from va_api.routers.auth_schemas import SessionUser
from va_common.auth import check_password_policy_async, looks_like_token
from va_common.errors import AppError
from va_db.models import User
from va_db.repositories import (
    IdentityRepo,
    InvitationLookup,
    InvitationLookupRepo,
    InvitationRepo,
    MembershipRepo,
    NewUser,
)

__all__ = [
    "NAME_REQUIRED_MESSAGE",
    "OTHER_EMAIL_MESSAGE",
    "PASSWORD_REQUIRED_MESSAGE",
    "SIGN_IN_MESSAGE",
    "accept_invitation",
    "invitation_details",
]

SIGN_IN_MESSAGE = "Sign in to accept this invitation."
OTHER_EMAIL_MESSAGE = "This invitation was sent to a different email address."
NAME_REQUIRED_MESSAGE = "Name is required to create your account."
PASSWORD_REQUIRED_MESSAGE = "Password is required to create your account."  # noqa: S105 - error text


def _invalid_invitation() -> AppError:
    return AppError("TOKEN_EXPIRED", INVITATION_INVALID_MESSAGE, field="token")


async def _lookup_usable(request: Request, token: str) -> InvitationLookup:
    """The open, unexpired invitation of ``token``; ``TOKEN_EXPIRED`` for anything else."""
    if not looks_like_token(token):
        raise _invalid_invitation()
    # specs/03-data-model.md §2: InvitationLookupRepo is a system-scope repository.
    async with database_of(request).system_session() as session:
        found = await InvitationLookupRepo(session).by_token(token)
    if found is None or not found.usable(clock_of(request).now()):
        raise _invalid_invitation()
    return found


def _check_org_open(found: InvitationLookup) -> None:
    if found.org_status == "closed":
        raise AppError("ORG_SUSPENDED", ORG_CLOSED_MESSAGE)
    if found.org_status == "suspended":
        raise AppError("ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)


async def _join(session: AsyncSession, found: InvitationLookup, user: User, now: datetime) -> None:
    """Consume the invitation and make ``user`` a member with its role, in the caller's tenant transaction."""
    members = MembershipRepo(session, found.org_id)
    if await members.get_for_user(user.id) is not None:
        raise AppError("CONFLICT", ALREADY_MEMBER_MESSAGE)
    if await InvitationRepo(session, found.org_id).consume(found.id, now) is None:
        raise _invalid_invitation()
    await members.add(user.id, found.role)


def _accepted(user: User, found: InvitationLookup) -> InvitationAccepted:
    return InvitationAccepted(
        user=SessionUser.of(user), org_id=found.org_id, org_name=found.org_name, role=found.role
    )


async def _accept_signed_in(
    request: Request, found: InvitationLookup, active: ActiveSession
) -> InvitationAccepted:
    """The signed-in user (MFA passed when enabled, same email) joins; the org becomes the active one."""
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(active.user_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    if user.mfa_enabled and not active.mfa_passed:
        raise AppError("MFA_REQUIRED", MFA_REQUIRED_MESSAGE)
    if user.email.casefold() != found.email.casefold():
        raise AppError("FORBIDDEN", OTHER_EMAIL_MESSAGE)
    now = clock_of(request).now()
    async with database_of(request).tenant_session(found.org_id) as session:
        await _join(session, found, user, now)
        repo = IdentityRepo(session)
        await repo.mark_email_verified(user.id, now)
        joined = await repo.get_user(user.id)
        if joined is None:
            raise AppError("AUTH_FAILURE", SESSION_INVALID)
        result = _accepted(joined, found)
    await sessions_of(request).set_active_org(active, found.org_id)
    return result


def _account_fields(body: AcceptInvitationRequest) -> tuple[str, str]:
    """The new account's name and password; ``INVALID_INPUT`` naming the missing one."""
    if body.name is None:
        raise AppError("INVALID_INPUT", NAME_REQUIRED_MESSAGE, field="name")
    if body.password is None:
        raise AppError("INVALID_INPUT", PASSWORD_REQUIRED_MESSAGE, field="password")
    return body.name, body.password


async def _accept_new_user(
    request: Request, response: Response, found: InvitationLookup, body: AcceptInvitationRequest
) -> InvitationAccepted:
    """Create the verified user, the membership, and a session in the invited org; set both cookies."""
    check_dashboard_origin(request)
    name, password = _account_fields(body)
    await check_sso_enforced(request, found.email)
    await check_password_policy_async(password, email=found.email)
    password_hash = await password_hasher_of(request).hash_async(password)
    now = clock_of(request).now()
    sessions = sessions_of(request)
    new = NewUser(email=found.email, name=name, password_hash=password_hash, email_verified_at=now)
    async with database_of(request).tenant_session(found.org_id) as session:
        user = await IdentityRepo(session).create_user(new)
        await _join(session, found, user, now)
        token, active = await sessions.start_in(
            session,
            user_id=user.id,
            active_org_id=found.org_id,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        result = _accepted(user, found)
    await sessions.remember(active)
    set_session_cookies(request, response, token)
    return result


@internal_auth_router.get(
    "/invitations/{token}", operation_id="auth_invitation_details", openapi_extra=PUBLIC_MARKER
)
async def invitation_details(token: str, request: Request) -> DataResponse[InvitationDetails]:
    """The org, role, email, and inviter of an open invitation (no session needed)."""
    found = await _lookup_usable(request, token)
    details = InvitationDetails(
        org_name=found.org_name,
        role=found.role,
        email=found.email,
        inviter_name=found.inviter_name,
        expires_at=found.expires_at,
        account_exists=found.account_exists,
    )
    return DataResponse(data=details)


@internal_auth_router.post(
    "/invitations/accept", operation_id="auth_invitation_accept", openapi_extra=PUBLIC_MARKER
)
async def accept_invitation(
    body: AcceptInvitationRequest, request: Request, response: Response
) -> DataResponse[InvitationAccepted]:
    """Accept an invitation as the signed-in user, or create the account when the email has none."""
    found = await _lookup_usable(request, body.token)
    _check_org_open(found)
    active = await optional_session(request)
    if active is not None:
        result = await _accept_signed_in(request, found, active)
    elif found.account_exists:
        raise AppError("AUTH_FAILURE", SIGN_IN_MESSAGE)
    else:
        result = await _accept_new_user(request, response, found, body)
    return DataResponse(data=result)
