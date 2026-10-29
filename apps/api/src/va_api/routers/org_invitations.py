"""Org invitations: create, list, revoke under ``/v1/org/members/invitations`` (specs/04 §6).

Every route needs ``members.manage`` (owners and admins, and API keys with ``*``); inviting someone as an
owner, or revoking an owner invitation, also needs ``org.owner_transfer``. The invitation is sent as the
session's user, or as the user who created the calling API key. Its token goes out by email only (after the
response); re-inviting the same address replaces the open invitation, so the old link stops working.
Accepting happens on ``/internal/auth/invitations`` (:mod:`va_api.routers.auth_invitations`).
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.api_keys import creating_user_id
from va_api.auth.context import RequestContext
from va_api.auth.invitations import send_invitation
from va_api.auth.permissions import require_permission
from va_api.auth.session_auth import clock_of, database_of, email_sender_of, settings_of
from va_api.responses import DataResponse
from va_api.routers.org_invitations_schemas import (
    InvitationCreate,
    InvitationRevoked,
    InvitationView,
    invitation_view,
)
from va_api.routers.org_members import owner_guard
from va_api.routers.org_sso import ReadOrg, WriteOrg, org_router
from va_common.errors import AppError
from va_db.models import Org, User
from va_db.repositories import IdentityRepo, InvitationRepo, MembershipRepo

__all__ = [
    "ALREADY_MEMBER_EMAIL_MESSAGE",
    "INVITATION_NOT_FOUND_MESSAGE",
    "NO_KEY_CREATOR_MESSAGE",
    "org_invitations_create",
    "org_invitations_list",
    "org_invitations_revoke",
]

INVITATION_NOT_FOUND_MESSAGE: Final = "Invitation not found."
ALREADY_MEMBER_EMAIL_MESSAGE: Final = "This person is already a member of the organization."
NO_KEY_CREATOR_MESSAGE: Final = "This API key has no creating user to send invitations as."
_MANAGE: Final = "members.manage"
_MANAGE_EXTRA: Final = {"x-permission": _MANAGE}


async def _inviter_of(ctx: RequestContext, session: AsyncSession) -> User:
    """The session's user, or the user who created the calling API key; ``FORBIDDEN`` without one."""
    user_id = await creating_user_id(ctx, session)
    user = await IdentityRepo(session).get_user(user_id) if user_id is not None else None
    if user is None:
        raise AppError("FORBIDDEN", NO_KEY_CREATOR_MESSAGE)
    return user


async def _org_name(session: AsyncSession, org_id: uuid.UUID) -> str:
    org = await session.get(Org, org_id)
    if org is None:
        raise AppError("NOT_FOUND", "Organization not found.")
    return org.name


@org_router.post(
    "/members/invitations",
    operation_id="org_invitations_create",
    status_code=201,
    dependencies=[Depends(require_permission(_MANAGE))],
    openapi_extra={"x-scope": "org:write", **_MANAGE_EXTRA},
)
async def org_invitations_create(
    body: InvitationCreate, request: Request, ctx: WriteOrg, background: BackgroundTasks
) -> DataResponse[InvitationView]:
    """Invite an email address into the org (7 days, single use); only owners can invite owners."""
    owner_guard(ctx, body.role)
    now = clock_of(request).now()
    async with database_of(request).tenant_session(ctx.org_id) as session:
        inviter = await _inviter_of(ctx, session)
        if await MembershipRepo(session, ctx.org_id).member_by_email(body.email) is not None:
            raise AppError("CONFLICT", ALREADY_MEMBER_EMAIL_MESSAGE, field="email")
        row, token = await InvitationRepo(session, ctx.org_id).create(
            email=body.email, role=body.role, invited_by=inviter.id, now=now
        )
        org_name = await _org_name(session, ctx.org_id)
        view = invitation_view(row, now)
    background.add_task(
        send_invitation,
        email_sender_of(request),
        to=view.email,
        org_name=org_name,
        inviter_name=inviter.name or inviter.email,
        role=view.role,
        token=token,
        dashboard_base_url=settings_of(request).dashboard_base_url,
    )
    return DataResponse(data=view)


@org_router.get(
    "/members/invitations",
    operation_id="org_invitations_list",
    dependencies=[Depends(require_permission(_MANAGE))],
    openapi_extra={"x-scope": "org:read", **_MANAGE_EXTRA},
)
async def org_invitations_list(request: Request, ctx: ReadOrg) -> DataResponse[list[InvitationView]]:
    """Every unaccepted invitation, newest first; expired ones are listed with ``status: "expired"``."""
    now = clock_of(request).now()
    async with database_of(request).tenant_session(ctx.org_id) as session:
        rows = await InvitationRepo(session, ctx.org_id).list_open()
        views = [invitation_view(row, now) for row in rows]
    return DataResponse(data=views)


@org_router.delete(
    "/members/invitations/{invitation_id}",
    operation_id="org_invitations_revoke",
    dependencies=[Depends(require_permission(_MANAGE))],
    openapi_extra={"x-scope": "org:write", **_MANAGE_EXTRA},
)
async def org_invitations_revoke(
    invitation_id: uuid.UUID, request: Request, ctx: WriteOrg
) -> DataResponse[InvitationRevoked]:
    """Revoke an unaccepted invitation, so its link stops working; owner invitations only by owners."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = InvitationRepo(session, ctx.org_id)
        row = await repo.get_open(invitation_id)
        if row is None:
            raise AppError("NOT_FOUND", INVITATION_NOT_FOUND_MESSAGE)
        owner_guard(ctx, row.role)
        if not await repo.revoke(invitation_id):
            raise AppError("NOT_FOUND", INVITATION_NOT_FOUND_MESSAGE)
    return DataResponse(data=InvitationRevoked(id=invitation_id))
