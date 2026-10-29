"""``GET /v1/org/settings`` and ``PATCH /v1/org/settings`` (specs/04-auth-tenancy-rbac.md §7, §8.1).

A PATCH body is parsed with :class:`va_domain.org.OrgSettings` and only the keys the client sent are applied:
groups merge key by key, every other field is replaced. ``orgs.settings`` keeps the sparse object, written
under a row lock so concurrent PATCHes never lose each other's keys. ``PATCH /v1/org`` applies its
``settings`` member through the same helpers with the ``settings.`` field prefix. The cached
:class:`va_db.org_settings.OrgSettingsReader` copy is invalidated after the write commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit, audited, changed
from va_api.auth.context import RequestContext
from va_api.auth.ip_allowlist import ip_allowed
from va_api.auth.permissions import holds_permission
from va_api.auth.session_auth import client_ip, database_of, org_settings_of
from va_api.responses import DataResponse
from va_api.routers.org_sso import ReadOrg, WriteOrg, org_router
from va_common.errors import AppError
from va_db.repositories import OrgRepo
from va_domain.org import (
    OrgSettings,
    effective_values,
    field_path,
    load_settings,
    merge_settings,
    needs_compliance,
    sent_paths,
    sparse_settings,
)

__all__ = [
    "BAA_ADMIN_ONLY_MESSAGE",
    "COMPLIANCE_PERMISSION",
    "HIPAA_NEEDS_BAA_MESSAGE",
    "IP_ALLOWLIST_NO_CLIENT_MESSAGE",
    "ORG_NOT_FOUND_MESSAGE",
    "ZERO_RETENTION_PLAN_MESSAGE",
    "SettingsPatchResult",
    "apply_settings_patch",
    "check_settings_permissions",
    "org_settings_get",
    "org_settings_update",
]

COMPLIANCE_PERMISSION: Final = "org.compliance"
ORG_NOT_FOUND_MESSAGE: Final = "Organization not found."
BAA_ADMIN_ONLY_MESSAGE: Final = "Only platform administrators can set 'baa_signed_at'."
HIPAA_NEEDS_BAA_MESSAGE: Final = "hipaa_mode requires a signed BAA ('baa_signed_at')."
ZERO_RETENTION_PLAN_MESSAGE: Final = "Zero retention requires the enterprise plan."
IP_ALLOWLIST_NO_CLIENT_MESSAGE: Final = "ip_allowlist cannot be set from a request without a client address."
_ZERO_RETENTION_PLAN: Final = "enterprise"
_HIPAA_PATHS: Final = frozenset({"hipaa_mode", "baa_signed_at"})
_ZERO_RETENTION_PATHS: Final = frozenset({"privacy", "privacy.zero_retention"})
_AUDIT_PREFIX: Final = "settings."


@dataclass(frozen=True, slots=True)
class SettingsPatchResult:
    """The settings after a patch, whether ``orgs.settings`` changed, and the audit diff.

    ``before``/``after`` hold the effective values that changed, keyed ``settings.<dotted path>``.
    """

    settings: OrgSettings
    written: bool
    before: dict[str, Any]
    after: dict[str, Any]


def check_settings_permissions(ctx: RequestContext, sent: Mapping[str, Any], prefix: str) -> None:
    """``FORBIDDEN`` unless ``ctx`` may set every path of ``sent`` (what the client sent, JSON mode).

    Compliance settings need ``org.compliance``; ``baa_signed_at`` is for platform administrators only.
    ``prefix`` (``""`` or ``"settings."``) is prepended to the error ``field``.
    """
    if needs_compliance(sent) and not holds_permission(ctx, COMPLIANCE_PERMISSION):
        raise AppError("FORBIDDEN", f"Missing required permission '{COMPLIANCE_PERMISSION}'.")
    if "baa_signed_at" in sent and ctx.principal_type != "platform_admin":
        raise AppError("FORBIDDEN", BAA_ADMIN_ONLY_MESSAGE, field=f"{prefix}baa_signed_at")


def _validated(merged: Mapping[str, Any], prefix: str) -> OrgSettings:
    try:
        return OrgSettings.model_validate(dict(merged))
    except ValidationError as exc:
        error = exc.errors()[0]
        raise AppError(
            "INVALID_INPUT", str(error["msg"]), field=f"{prefix}{field_path(tuple(error['loc']))}"
        ) from exc


def _check_cross_field(ctx: RequestContext, settings: OrgSettings, paths: set[str], prefix: str) -> None:
    # Only rules whose paths were sent apply, so an unrelated PATCH never fails on state set elsewhere.
    if paths & _HIPAA_PATHS and settings.hipaa_mode and settings.baa_signed_at is None:
        raise AppError("INVALID_INPUT", HIPAA_NEEDS_BAA_MESSAGE, field=f"{prefix}hipaa_mode")
    if (
        paths & _ZERO_RETENTION_PATHS
        and settings.privacy.zero_retention
        and ctx.plan_id != _ZERO_RETENTION_PLAN
    ):
        raise AppError(
            "FEATURE_NOT_IN_PLAN", ZERO_RETENTION_PLAN_MESSAGE, field=f"{prefix}privacy.zero_retention"
        )


def _check_keeps_caller(settings: OrgSettings, paths: set[str], ip: str | None, prefix: str) -> None:
    """A sent non-empty ``ip_allowlist`` must admit the address of the request setting it (no lockout)."""
    if "ip_allowlist" not in paths or ip_allowed(ip, settings.ip_allowlist):
        return
    message = (
        IP_ALLOWLIST_NO_CLIENT_MESSAGE
        if ip is None
        else f"ip_allowlist must include the address of this request ({ip})."
    )
    raise AppError("INVALID_INPUT", message, field=f"{prefix}ip_allowlist")


def _stored_at(stored: Mapping[str, Any], path: str, fallback: Any) -> Any:
    value: Any = stored
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return fallback
        value = value[part]
    return value


def _previous_values(stored: Mapping[str, Any], paths: list[str]) -> dict[str, Any]:
    """The effective values at ``paths`` before the patch, for the audit diff.

    Stored settings that no longer validate (the patch repairs them) are read raw, defaults elsewhere.
    """
    try:
        return effective_values(load_settings(stored), paths)
    except ValidationError:
        defaults = effective_values(OrgSettings(), paths)
        return {path: _stored_at(stored, path, defaults[path]) for path in paths}


async def apply_settings_patch(
    session: AsyncSession, ctx: RequestContext, sent: Mapping[str, Any], prefix: str, *, ip: str | None
) -> SettingsPatchResult:
    """Lock the org row of ``ctx``, merge ``sent`` into its stored settings, validate, and store the result.

    Runs in the caller's global session; the caller writes the audit row and invalidates the cache after
    commit. ``NOT_FOUND`` when the org is gone, ``INVALID_INPUT`` / ``FEATURE_NOT_IN_PLAN`` on rule failures,
    including a sent ``ip_allowlist`` that would reject ``ip`` (the client address of the request).
    """
    repo = OrgRepo(session)
    org = await repo.lock(ctx.org_id)
    if org is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    stored = dict(org.settings)
    settings = _validated(merge_settings(stored, sent), prefix)
    paths = sent_paths(sent)
    _check_cross_field(ctx, settings, set(paths), prefix)
    _check_keeps_caller(settings, set(paths), ip, prefix)
    sparse = sparse_settings(settings)
    written = sparse != stored
    if written:
        await repo.update(ctx.org_id, settings=sparse)
    before, after = changed(_previous_values(stored, paths), effective_values(settings, paths))
    return SettingsPatchResult(
        settings=settings,
        written=written,
        before={f"{_AUDIT_PREFIX}{path}": value for path, value in before.items()},
        after={f"{_AUDIT_PREFIX}{path}": value for path, value in after.items()},
    )


@org_router.get("/settings", operation_id="org_settings_get", openapi_extra={"x-scope": "org:read"})
async def org_settings_get(request: Request, ctx: ReadOrg) -> DataResponse[OrgSettings]:
    """Every org setting, defaults filled for the ones never set."""
    settings = await org_settings_of(request).get(ctx.org_id)
    if settings is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    return DataResponse(data=settings)


@org_router.patch(
    "/settings",
    operation_id="org_settings_update",
    openapi_extra={"x-scope": "org:write", **audited("org.update")},
)
async def org_settings_update(
    body: OrgSettings, request: Request, ctx: WriteOrg
) -> DataResponse[OrgSettings]:
    """Change org settings: omitted keys stay unchanged, groups merge key by key, other values are replaced.

    Compliance settings need the `org.compliance` permission; `baa_signed_at` is set by platform
    administrators only; `hipaa_mode` needs a signed BAA and `privacy.zero_retention` the enterprise plan. A
    non-empty `ip_allowlist` must include the address of the request that sets it.
    """
    sent = body.model_dump(mode="json", exclude_unset=True)
    check_settings_permissions(ctx, sent, "")
    async with database_of(request).global_session() as session:
        result = await apply_settings_patch(session, ctx, sent, "", ip=client_ip(request))
        if result.after:
            await audit(
                session, ctx, "org.update", "org", ctx.org_id, before=result.before, after=result.after
            )
    if result.written:
        await org_settings_of(request).invalidate(ctx.org_id)
    return DataResponse(data=result.settings)
