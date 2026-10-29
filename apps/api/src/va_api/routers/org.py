"""``GET /v1/me``, ``GET /v1/org`` and ``PATCH /v1/org`` (specs/04-auth-tenancy-rbac.md §8.1).

``orgs`` is a global table without RLS: every read and write goes through ``OrgRepo`` on the global session
with the request context's org id, never an id from the request. The pool holds one connection per role, so
the sessions of one request are opened one after another, never nested.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import Request

from va_api.audit import audit, audited, changed
from va_api.auth.api_key_auth import INVALID_API_KEY
from va_api.auth.context import RequestContext
from va_api.auth.permissions import holds_permission
from va_api.auth.session_auth import database_of
from va_api.auth.sessions import SESSION_INVALID
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.org_schemas import (
    MeApiKey,
    MeUser,
    MeView,
    OrgPatch,
    OrgSummary,
    OrgView,
    effective_limits,
)
from va_api.routers.org_sso import ReadOrg, WriteOrg, org_router
from va_common.errors import AppError
from va_common.money import micros_to_dollars
from va_db.models import Org
from va_db.repositories import ApiKeyRepo, IdentityRepo, OrgOverview, OrgRepo

__all__ = ["ORG_NOT_FOUND_MESSAGE", "me_get", "me_router", "org_get", "org_update"]

ORG_NOT_FOUND_MESSAGE: Final = "Organization not found."
_COMPLIANCE: Final = "org.compliance"
_COMPLIANCE_FIELDS: Final = ("retention_days", "recording_retention_days")
_PATCH_FIELDS: Final = ("name", *_COMPLIANCE_FIELDS)

me_router = envelope_router(prefix="/v1", tags=["Org"])


def _patched(org: Org | None, body: OrgPatch) -> dict[str, object]:
    """The current values of the columns ``body`` sets (empty when ``org`` is gone)."""
    if org is None:
        return {}
    return {name: getattr(org, name) for name in _PATCH_FIELDS if getattr(body, name) is not None}


def _summary(org: Org) -> OrgSummary:
    return OrgSummary(id=org.id, name=org.name, slug=org.slug, plan_id=org.plan_id, status=org.status)


def _org_view(overview: OrgOverview) -> OrgView:
    org = overview.org
    return OrgView(
        id=org.id,
        name=org.name,
        slug=org.slug,
        parent_org_id=org.parent_org_id,
        plan_id=org.plan_id,
        kyc_level=org.kyc_level,
        status=org.status,
        data_region=org.data_region,
        retention_days=org.retention_days,
        recording_retention_days=org.recording_retention_days,
        credits_balance=float(micros_to_dollars(org.credits_balance_micros)),
        limits=effective_limits(overview.plan, org.limits),
        settings=dict(org.settings),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


async def _overview(request: Request, org_id: uuid.UUID) -> OrgOverview:
    async with database_of(request).global_session() as session:
        overview = await OrgRepo(session).overview(org_id)
    if overview is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    return overview


async def _me_user(request: Request, ctx: RequestContext) -> MeUser:
    async with database_of(request).global_session() as session:
        user = await IdentityRepo(session).get_user(ctx.principal_id)
    if user is None:
        raise AppError("AUTH_FAILURE", SESSION_INVALID)
    return MeUser(id=user.id, email=user.email, name=user.name, mfa_enabled=user.mfa_enabled)


async def _me_api_key(request: Request, ctx: RequestContext) -> MeApiKey:
    async with database_of(request).tenant_session(ctx.principal_org_id) as session:
        key = await ApiKeyRepo(session, ctx.principal_org_id).get(ctx.principal_id)
    if key is None:
        raise AppError("AUTH_FAILURE", INVALID_API_KEY)
    return MeApiKey(id=key.id, prefix=key.prefix, scopes=sorted(ctx.scopes), environment=ctx.environment)


@me_router.get("/me", operation_id="me_get", openapi_extra={"x-scope": "org:read"})
async def me_get(request: Request, ctx: ReadOrg) -> DataResponse[MeView]:
    """The calling principal: user or API key, the org it acts in, role and scopes; `acting_parent_org_id` is
    the parent org while a parent principal acts on a sub-account."""
    user = await _me_user(request, ctx) if ctx.principal_type in {"user", "platform_admin"} else None
    api_key = await _me_api_key(request, ctx) if ctx.principal_type == "api_key" else None
    async with database_of(request).global_session() as session:
        org = await OrgRepo(session).get(ctx.org_id)
    if org is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    view = MeView(
        principal_type=ctx.principal_type,
        user=user,
        api_key=api_key,
        org=_summary(org),
        role=ctx.role,
        scopes=sorted(ctx.scopes),
        acting_parent_org_id=ctx.acting_parent_org_id,
    )
    return DataResponse(data=view)


@org_router.get("", operation_id="org_get", openapi_extra={"x-scope": "org:read"})
async def org_get(request: Request, ctx: ReadOrg) -> DataResponse[OrgView]:
    """The org with its effective limits (plan values overlaid by overrides) and balance in dollars."""
    return DataResponse(data=_org_view(await _overview(request, ctx.org_id)))


@org_router.patch(
    "", operation_id="org_update", openapi_extra={"x-scope": "org:write", **audited("org.update")}
)
async def org_update(body: OrgPatch, request: Request, ctx: WriteOrg) -> DataResponse[OrgView]:
    """Rename the org or change its retention; `retention_days` and `recording_retention_days` also need the
    `org.compliance` permission."""
    if any(getattr(body, name) is not None for name in _COMPLIANCE_FIELDS) and not holds_permission(
        ctx, _COMPLIANCE
    ):
        raise AppError("FORBIDDEN", f"Missing required permission '{_COMPLIANCE}'.")
    async with database_of(request).global_session() as session:
        repo = OrgRepo(session)
        before = _patched(await repo.get(ctx.org_id), body)
        after = _patched(
            await repo.update(
                ctx.org_id,
                name=body.name,
                retention_days=body.retention_days,
                recording_retention_days=body.recording_retention_days,
            ),
            body,
        )
        old, new = changed(before, after)
        if new:
            await audit(session, ctx, "org.update", "org", ctx.org_id, before=old, after=new)
        overview = await repo.overview(ctx.org_id)
    if overview is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    return DataResponse(data=_org_view(overview))
