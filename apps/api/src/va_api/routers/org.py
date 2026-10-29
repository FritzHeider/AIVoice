"""``GET /v1/me``, ``GET /v1/org`` and ``PATCH /v1/org`` (specs/04-auth-tenancy-rbac.md §8.1).

``PATCH /v1/org`` applies its ``settings`` member like ``PATCH /v1/org/settings`` (:mod:`va_api.routers.
org_settings`), in the same transaction and audit row as the column changes.

``orgs`` is a global table without RLS: every read and write goes through ``OrgRepo`` on the global session
with the request context's org id, never an id from the request. The pool holds one connection per role, so
the sessions of one request are opened one after another, never nested.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from fastapi import Request

from va_api.audit import audit, audited, changed
from va_api.auth.api_key_auth import INVALID_API_KEY
from va_api.auth.context import RequestContext
from va_api.auth.permissions import holds_permission
from va_api.auth.session_auth import database_of, org_settings_of
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
from va_api.routers.org_settings import (
    COMPLIANCE_PERMISSION,
    ORG_NOT_FOUND_MESSAGE,
    SettingsPatchResult,
    apply_settings_patch,
    check_settings_permissions,
)
from va_api.routers.org_sso import ReadOrg, WriteOrg, org_router
from va_common.errors import AppError
from va_common.money import micros_to_dollars
from va_db.models import Org
from va_db.repositories import ApiKeyRepo, IdentityRepo, OrgOverview, OrgRepo
from va_domain.org import load_settings

__all__ = ["ORG_NOT_FOUND_MESSAGE", "me_get", "me_router", "org_get", "org_update"]

_SETTINGS_PREFIX: Final = "settings."
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
        settings=load_settings(org.settings),
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


async def _patch_columns(
    repo: OrgRepo, ctx: RequestContext, body: OrgPatch
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update the columns ``body`` sets; the changed values before and after, by column name."""
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
    return changed(before, after)


@org_router.patch(
    "", operation_id="org_update", openapi_extra={"x-scope": "org:write", **audited("org.update")}
)
async def org_update(body: OrgPatch, request: Request, ctx: WriteOrg) -> DataResponse[OrgView]:
    """Rename the org, change its retention, or change settings (as `PATCH /v1/org/settings`);
    `retention_days`, `recording_retention_days`, and compliance settings also need the `org.compliance`
    permission."""
    if any(getattr(body, name) is not None for name in _COMPLIANCE_FIELDS) and not holds_permission(
        ctx, COMPLIANCE_PERMISSION
    ):
        raise AppError("FORBIDDEN", f"Missing required permission '{COMPLIANCE_PERMISSION}'.")
    sent = body.settings.model_dump(mode="json", exclude_unset=True)
    check_settings_permissions(ctx, sent, _SETTINGS_PREFIX)
    settings: SettingsPatchResult | None = None
    async with database_of(request).global_session() as session:
        repo = OrgRepo(session)
        if sent:
            settings = await apply_settings_patch(session, ctx, sent, _SETTINGS_PREFIX)
        old, new = await _patch_columns(repo, ctx, body)
        if settings is not None:
            old, new = old | settings.before, new | settings.after
        if new:
            await audit(session, ctx, "org.update", "org", ctx.org_id, before=old, after=new)
        overview = await repo.overview(ctx.org_id)
    if settings is not None and settings.written:
        await org_settings_of(request).invalidate(ctx.org_id)
    if overview is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    return DataResponse(data=_org_view(overview))
