"""Org members: list, change role, remove, transfer ownership (specs/04-auth-tenancy-rbac.md §6, §3.2).

Every route runs in one ``tenant_session`` of the request context's org. Changes lock the org row first, so
the owner checks of :class:`va_db.repositories.MembershipRepo` see every committed concurrent change and an
org always keeps an owner. Changing or removing members needs ``members.manage``; touching an owner (making
someone an owner, or demoting or removing one) additionally needs ``org.owner_transfer``, which only owner
sessions hold. Transferring ownership also needs a recent sign-in. A removed member's sessions are not
revoked: the next request re-reads the membership and falls back to the user's other org, if any.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import Depends, Request

from va_api.auth.context import RequestContext
from va_api.auth.password_reset import require_recent_auth
from va_api.auth.permissions import holds_permission, require_permission
from va_api.auth.session_auth import clock_of, database_of
from va_api.auth.sessions import ActiveSession
from va_api.responses import DataResponse
from va_api.routers.org_members_schemas import (
    MemberRemoved,
    MemberRolePatch,
    MemberView,
    OwnershipTransferred,
    TransferOwnershipRequest,
    member_view,
)
from va_api.routers.org_sso import ReadOrg, WriteOrg, org_router
from va_common.errors import AppError
from va_db.models import MemberRole
from va_db.repositories import MemberRecord, MembershipRepo

__all__ = [
    "MEMBER_NOT_FOUND_MESSAGE",
    "PARENT_TRANSFER_MESSAGE",
    "SELF_TRANSFER_MESSAGE",
    "TRANSFER_REAUTH_MESSAGE",
    "org_members_list",
    "org_members_remove",
    "org_members_update",
    "org_transfer_ownership",
    "owner_guard",
]

MEMBER_NOT_FOUND_MESSAGE: Final = "Member not found."
TRANSFER_REAUTH_MESSAGE: Final = "Sign in again to transfer ownership."
SELF_TRANSFER_MESSAGE: Final = "You already own this organization."
PARENT_TRANSFER_MESSAGE: Final = "Ownership of a sub-account cannot be transferred by a parent-org principal."
_MANAGE: Final = "members.manage"
_OWNER_TRANSFER: Final = "org.owner_transfer"


def _found[T](value: T | None) -> T:
    if value is None:
        raise AppError("NOT_FOUND", MEMBER_NOT_FOUND_MESSAGE)
    return value


def owner_guard(ctx: RequestContext, target_role: MemberRole, new_role: MemberRole | None = None) -> None:
    """``FORBIDDEN`` when the change touches an owner (``target_role`` or ``new_role``) and the principal
    lacks ``org.owner_transfer``; invitations pass the invited role as ``target_role``."""
    if "owner" in {target_role, new_role} and not holds_permission(ctx, _OWNER_TRANSFER):
        raise AppError("FORBIDDEN", f"Missing required permission '{_OWNER_TRANSFER}'.")


async def _locked_target(repo: MembershipRepo, user_id: uuid.UUID) -> MemberRecord:
    """The member ``user_id`` after locking the org row; ``NOT_FOUND`` when not a member of this org."""
    await repo.lock_org()
    return _found(await repo.member(user_id))


@org_router.get("/members", operation_id="org_members_list", openapi_extra={"x-scope": "org:read"})
async def org_members_list(request: Request, ctx: ReadOrg) -> DataResponse[list[MemberView]]:
    """Every member of the org, oldest membership first."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        records = await MembershipRepo(session, ctx.org_id).list_members()
    return DataResponse(data=[member_view(record) for record in records])


@org_router.patch(
    "/members/{user_id}",
    operation_id="org_members_update",
    dependencies=[Depends(require_permission(_MANAGE))],
    openapi_extra={"x-scope": "org:write", "x-permission": _MANAGE},
)
async def org_members_update(
    user_id: uuid.UUID, body: MemberRolePatch, request: Request, ctx: WriteOrg
) -> DataResponse[MemberView]:
    """Change a member's role; owners can be made or demoted only by owners, and the last owner stays."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = MembershipRepo(session, ctx.org_id)
        target = await _locked_target(repo, user_id)
        owner_guard(ctx, target.role, body.role)
        updated = _found(await repo.change_role(user_id, body.role))
    return DataResponse(data=member_view(updated))


@org_router.delete(
    "/members/{user_id}",
    operation_id="org_members_remove",
    dependencies=[Depends(require_permission(_MANAGE))],
    openapi_extra={"x-scope": "org:write", "x-permission": _MANAGE},
)
async def org_members_remove(
    user_id: uuid.UUID, request: Request, ctx: WriteOrg
) -> DataResponse[MemberRemoved]:
    """Remove a member (also oneself); owners can be removed only by owners, and the last owner stays."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = MembershipRepo(session, ctx.org_id)
        target = await _locked_target(repo, user_id)
        owner_guard(ctx, target.role)
        if not await repo.remove_member(user_id):
            raise AppError("NOT_FOUND", MEMBER_NOT_FOUND_MESSAGE)
    return DataResponse(data=MemberRemoved(user_id=user_id))


def _session_of(request: Request) -> ActiveSession:
    active = getattr(request.state, "session", None)
    if not isinstance(active, ActiveSession):
        raise AppError("FORBIDDEN", f"Missing required permission '{_OWNER_TRANSFER}'.")
    return active


@org_router.post(
    "/transfer-ownership",
    operation_id="org_transfer_ownership",
    dependencies=[Depends(require_permission(_OWNER_TRANSFER))],
    openapi_extra={"x-scope": "org:write", "x-permission": _OWNER_TRANSFER},
)
async def org_transfer_ownership(
    body: TransferOwnershipRequest, request: Request, ctx: WriteOrg
) -> DataResponse[OwnershipTransferred]:
    """Make another member an owner and the calling owner an admin; needs a sign-in in the last 10 minutes.

    A parent-org principal acting on a sub-account is not one of its members and cannot transfer it."""
    if ctx.acting_parent_org_id is not None:
        raise AppError("FORBIDDEN", PARENT_TRANSFER_MESSAGE)
    require_recent_auth(_session_of(request), clock_of(request).now(), message=TRANSFER_REAUTH_MESSAGE)
    if body.user_id == ctx.principal_id:
        raise AppError("INVALID_INPUT", SELF_TRANSFER_MESSAGE, field="user_id")
    async with database_of(request).tenant_session(ctx.org_id) as session:
        previous, owner = _found(
            await MembershipRepo(session, ctx.org_id).transfer_ownership(ctx.principal_id, body.user_id)
        )
    return DataResponse(
        data=OwnershipTransferred(previous_owner=member_view(previous), owner=member_view(owner))
    )
