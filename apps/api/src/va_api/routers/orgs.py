"""Bland-compatible sub-accounts under ``/v1/orgs`` (specs/04-auth-tenancy-rbac.md §8.2, §8.4; legacy bodies).

Every route needs ``subaccounts.manage`` (owner and admin sessions, API keys with ``*``). The calling org is
always the parent: a sub-account id from the path is used only after ``OrgRepo.child`` confirmed that its
``parent_org_id`` is the caller's org, so siblings, the parent itself, and other tenants read as
``NOT_FOUND``.
Sub-accounts cannot have sub-accounts (depth 1).

Creating one runs in a single ``tenant_session`` of the new org: provisioning (data key, webhook secret; no
members), the sub-account's ``*`` API key, and the ``initial_credits`` transfer from the parent
(:class:`va_db.repositories.CreditTransferRepo`, which switches ``app.org_id`` per ledger row), so any failure
leaves nothing behind. The plaintext key is in the first response only: an ``Idempotency-Key`` replay carries
``"api_key": null`` (:func:`va_api.idempotency.withhold_on_replay`). The pool holds one connection per role,
so the sessions of one request are opened one after another, never nested.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit
from va_api.auth.api_keys import creating_user_id, invalidate_cached_keys, mint_api_key
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.permissions import require_permission
from va_api.auth.session_auth import api_key_hasher_of, database_of, provisioner_of
from va_api.envelopes import legacy_router
from va_api.idempotency import withhold_on_replay
from va_api.routers.orgs_schemas import (
    SubAccountCreate,
    SubAccountCreated,
    SubAccountList,
    SubAccountResult,
    sub_account_item,
)
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.money import dollars_to_micros
from va_db.models import Org, OrgStatus
from va_db.repositories import ApiKeyRepo, CreditTransferRepo, OrgRepo

__all__ = [
    "CLOSED_SUB_ACCOUNT_MESSAGE",
    "NESTED_SUB_ACCOUNT_MESSAGE",
    "SUB_ACCOUNT_KEY_NAME",
    "SUB_ACCOUNT_NOT_FOUND_MESSAGE",
    "orgs_create",
    "orgs_get",
    "orgs_list",
    "orgs_reactivate",
    "orgs_router",
    "orgs_suspend",
]

SUB_ACCOUNT_NOT_FOUND_MESSAGE: Final = "Sub-account not found."
NESTED_SUB_ACCOUNT_MESSAGE: Final = "Sub-accounts cannot create sub-accounts."
CLOSED_SUB_ACCOUNT_MESSAGE: Final = "A closed sub-account cannot be changed."
SUB_ACCOUNT_KEY_NAME: Final = "Sub-account key"
_PARENT_NOT_FOUND_MESSAGE: Final = "Organization not found."
_TRANSFER_DESCRIPTION: Final = "Initial credits of a new sub-account"
_MANAGE: Final = "subaccounts.manage"

orgs_router = legacy_router(prefix="/v1/orgs", tags=["Org"])

ReadOrgs = Annotated[RequestContext, Depends(require_scope("org:read"))]
WriteOrgs = Annotated[RequestContext, Depends(require_scope("org:write"))]
_DEPENDENCIES: Final = [Depends(require_permission(_MANAGE))]


@dataclass(frozen=True, slots=True)
class _Parent:
    org: Org
    created_by: uuid.UUID | None


async def _parent(request: Request, ctx: RequestContext) -> _Parent:
    """The calling org (``NOT_FOUND`` when gone, ``FORBIDDEN`` when itself a sub-account) and the creator."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        org = await OrgRepo(session).get(ctx.org_id)
        if org is None:
            raise AppError("NOT_FOUND", _PARENT_NOT_FOUND_MESSAGE)
        if org.parent_org_id is not None:
            raise AppError("FORBIDDEN", NESTED_SUB_ACCOUNT_MESSAGE)
        return _Parent(org=org, created_by=await creating_user_id(ctx, session))


def _initial_micros(amount: float | None) -> int:
    return 0 if amount is None else dollars_to_micros(amount)


async def _audit_create(session: AsyncSession, ctx: RequestContext, child: Org, amount_micros: int) -> None:
    """``subaccount.create`` and, when funded, ``billing.credit_transfer`` in the calling parent's log."""
    created = {"name": child.name, "plan_id": child.plan_id, "initial_credits_micros": amount_micros}
    await audit(session, ctx, "subaccount.create", "org", child.id, after=created)
    if amount_micros > 0:
        transfer = {"amount_micros": amount_micros}
        await audit(session, ctx, "billing.credit_transfer", "org", child.id, after=transfer)


@orgs_router.post(
    "/create",
    operation_id="orgs_create",
    dependencies=_DEPENDENCIES,
    openapi_extra={"x-scope": "org:write", "x-permission": _MANAGE},
)
async def orgs_create(body: SubAccountCreate, request: Request, ctx: WriteOrgs) -> SubAccountCreated:
    """Create a sub-account with its own API key, optionally funded from this org's available credits."""
    withhold_on_replay(request, "api_key")
    parent = await _parent(request, ctx)
    child_id = new_id()
    minted = mint_api_key(api_key_hasher_of(request), ctx.environment)
    amount = _initial_micros(body.initial_credits)
    async with database_of(request).tenant_session(child_id) as session:
        provisioned = await provisioner_of(request).provision_in(
            session,
            child_id,
            name=body.org_display_name,
            owner=None,
            plan_id=body.org_plan or parent.org.plan_id,
            parent_org_id=parent.org.id,
            kyc_level=parent.org.kyc_level,
        )
        await ApiKeyRepo(session, child_id).add(
            name=SUB_ACCOUNT_KEY_NAME,
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=["*"],
            environment=ctx.environment,
            created_by=parent.created_by,
        )
        if amount > 0:
            await CreditTransferRepo(session).transfer(
                parent.org.id,
                child_id,
                amount,
                idempotency_key=f"xfer:{new_id()}",
                description=_TRANSFER_DESCRIPTION,
            )
        await _audit_create(session, ctx, provisioned.org, amount)
        item = sub_account_item(provisioned.org)
    return SubAccountCreated(
        org_id=item.org_id,
        org_display_name=item.org_display_name,
        org_plan=item.org_plan,
        kyc_level=item.kyc_level,
        api_key=minted.plaintext,
        credits=item.credits,
        created_at=item.created_at,
    )


@orgs_router.get(
    "",
    operation_id="orgs_list",
    dependencies=_DEPENDENCIES,
    openapi_extra={"x-scope": "org:read", "x-permission": _MANAGE},
)
async def orgs_list(request: Request, ctx: ReadOrgs) -> SubAccountList:
    """Every direct sub-account of this org, oldest first."""
    async with database_of(request).global_session() as session:
        children = await OrgRepo(session).children(ctx.org_id)
    return SubAccountList(orgs=[sub_account_item(org) for org in children])


@orgs_router.get(
    "/{org_id}",
    operation_id="orgs_get",
    dependencies=_DEPENDENCIES,
    openapi_extra={"x-scope": "org:read", "x-permission": _MANAGE},
)
async def orgs_get(org_id: uuid.UUID, request: Request, ctx: ReadOrgs) -> SubAccountResult:
    """One direct sub-account of this org."""
    async with database_of(request).global_session() as session:
        child = await OrgRepo(session).child(ctx.org_id, org_id)
    if child is None:
        raise AppError("NOT_FOUND", SUB_ACCOUNT_NOT_FOUND_MESSAGE)
    return SubAccountResult(org=sub_account_item(child))


async def _set_status(request: Request, ctx: RequestContext, org_id: uuid.UUID, status: OrgStatus) -> Org:
    """Set a direct sub-account's status (``NOT_FOUND``; ``CONFLICT`` when closed); drop its key cache."""
    async with database_of(request).tenant_session(org_id) as session:
        repo = OrgRepo(session)
        child = await repo.child(ctx.org_id, org_id)
        if child is None:
            raise AppError("NOT_FOUND", SUB_ACCOUNT_NOT_FOUND_MESSAGE)
        if child.status == "closed":
            raise AppError("CONFLICT", CLOSED_SUB_ACCOUNT_MESSAGE)
        updated = child
        if child.status != status:
            previous = child.status
            updated = await repo.set_status(org_id, status) or child
            action = "subaccount.suspend" if status == "suspended" else "subaccount.reactivate"
            statuses = ({"status": previous}, {"status": status})
            await audit(session, ctx, action, "org", org_id, before=statuses[0], after=statuses[1])
        prefixes = await ApiKeyRepo(session, org_id).prefixes()
    await invalidate_cached_keys(request, prefixes)
    return updated


@orgs_router.post(
    "/{org_id}/suspend",
    operation_id="orgs_suspend",
    dependencies=_DEPENDENCIES,
    openapi_extra={"x-scope": "org:write", "x-permission": _MANAGE},
)
async def orgs_suspend(org_id: uuid.UUID, request: Request, ctx: WriteOrgs) -> SubAccountResult:
    """Suspend a sub-account: its keys and members may only read until it is reactivated."""
    return SubAccountResult(org=sub_account_item(await _set_status(request, ctx, org_id, "suspended")))


@orgs_router.post(
    "/{org_id}/reactivate",
    operation_id="orgs_reactivate",
    dependencies=_DEPENDENCIES,
    openapi_extra={"x-scope": "org:write", "x-permission": _MANAGE},
)
async def orgs_reactivate(org_id: uuid.UUID, request: Request, ctx: WriteOrgs) -> SubAccountResult:
    """Reactivate a suspended sub-account."""
    return SubAccountResult(org=sub_account_item(await _set_status(request, ctx, org_id, "active")))
