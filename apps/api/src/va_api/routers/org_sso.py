"""The org's OIDC SSO connection: ``PUT``/``GET``/``DELETE /v1/org/sso`` (specs/04-auth-tenancy-rbac.md §5).

Every route needs the ``org:*`` scope and the session-only ``sso.manage`` permission, so API keys never
configure SSO. While ``OIDC_ENABLED`` is false the routes answer ``FORBIDDEN`` after authentication. A ``PUT``
first validates the issuer's discovery document and checks that no other org claims one of the domains,
then encrypts the client secret under the org's data key (column AAD) and upserts the org's single row. The
pool holds one connection per role, so no two database sessions are ever open at once.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, Request

from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.oidc_discovery import DiscoveryError
from va_api.auth.permissions import require_permission
from va_api.auth.session_auth import database_of, envelope_of, oidc_discovery_of, settings_of
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.org_sso_schemas import SsoConnectionDeleted, SsoConnectionInput, SsoConnectionView
from va_common.crypto import Envelope, column_aad
from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import SsoConnection
from va_db.repositories import SsoConnectionLookupRepo, SsoConnectionRepo

__all__ = [
    "NOT_ENABLED_MESSAGE",
    "NOT_FOUND_MESSAGE",
    "SECRET_REQUIRED_MESSAGE",
    "decrypt_client_secret",
    "org_router",
    "org_sso_delete",
    "org_sso_get",
    "org_sso_put",
]

NOT_ENABLED_MESSAGE: Final = "SSO is not enabled on this deployment."
NOT_FOUND_MESSAGE: Final = "SSO connection not found."
SECRET_REQUIRED_MESSAGE: Final = "client_secret is required when creating an SSO connection."  # noqa: S105
_PERMISSION: Final = "sso.manage"

# T08 attaches the remaining /v1/org routes to this router.
org_router = envelope_router(prefix="/v1/org", tags=["Org"])

ReadOrg = Annotated[RequestContext, Depends(require_scope("org:read"))]
WriteOrg = Annotated[RequestContext, Depends(require_scope("org:write"))]
_MANAGE = [Depends(require_permission(_PERMISSION))]


def _secret_aad(connection_id: uuid.UUID) -> str:
    return column_aad("sso_connections", "client_secret_enc", connection_id)


async def decrypt_client_secret(envelope: Envelope, row: SsoConnection) -> str:
    """The plaintext client secret of ``row`` (for the authorization-code exchange)."""
    return await envelope.decrypt_str(row.org_id, row.client_secret_enc, _secret_aad(row.id))


def _ensure_enabled(request: Request) -> None:
    if not settings_of(request).oidc_enabled:
        raise AppError("FORBIDDEN", NOT_ENABLED_MESSAGE)


def _view(row: SsoConnection) -> SsoConnectionView:
    return SsoConnectionView(
        id=row.id,
        provider="oidc",
        issuer=row.issuer,
        client_id=row.client_id,
        allowed_domains=list(row.allowed_domains),
        default_role=row.default_role,
        enforce=row.enforce,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _current(request: Request, org_id: uuid.UUID) -> SsoConnection | None:
    async with database_of(request).tenant_session(org_id) as session:
        return await SsoConnectionRepo(session, org_id).get()


async def _validate_issuer(request: Request, issuer: str) -> None:
    try:
        await oidc_discovery_of(request).fetch(issuer)
    except DiscoveryError as exc:
        message = f"Could not validate the OIDC issuer: {exc.reason}."
        raise AppError("INVALID_INPUT", message, field="issuer") from exc


async def _check_domains(request: Request, org_id: uuid.UUID, domains: list[str]) -> None:
    """``CONFLICT`` when another org's connection already lists one of ``domains``."""
    # specs/03-data-model.md §2: SsoConnectionLookupRepo is a system-scope repository.
    async with database_of(request).system_session() as session:
        claims = await SsoConnectionLookupRepo(session).claims(domains)
    for claim in claims:
        if claim.org_id != org_id:
            message = f"Domain {claim.domain} is already claimed by another organization's SSO connection."
            raise AppError("CONFLICT", message, field="allowed_domains")


@org_router.put(
    "/sso",
    operation_id="org_sso_put",
    dependencies=_MANAGE,
    openapi_extra={"x-scope": "org:write", "x-permission": _PERMISSION},
)
async def org_sso_put(
    body: SsoConnectionInput, request: Request, ctx: WriteOrg
) -> DataResponse[SsoConnectionView]:
    """Create or replace the org's OIDC connection after validating the issuer's discovery document."""
    _ensure_enabled(request)
    existing = await _current(request, ctx.org_id)
    if existing is None and body.client_secret is None:
        raise AppError("INVALID_INPUT", SECRET_REQUIRED_MESSAGE, field="client_secret")
    await _validate_issuer(request, body.issuer)
    await _check_domains(request, ctx.org_id, body.allowed_domains)
    connection_id = existing.id if existing is not None else new_id()
    secret_enc: bytes | None = None
    if body.client_secret is not None:
        secret_enc = await envelope_of(request).encrypt_str(
            ctx.org_id, body.client_secret.get_secret_value(), _secret_aad(connection_id)
        )
    async with database_of(request).tenant_session(ctx.org_id) as session:
        row = await SsoConnectionRepo(session, ctx.org_id).upsert(
            connection_id=connection_id,
            issuer=body.issuer,
            client_id=body.client_id,
            client_secret_enc=secret_enc,
            allowed_domains=body.allowed_domains,
            default_role=body.default_role,
            enforce=body.enforce,
        )
        view = _view(row)
    return DataResponse(data=view)


@org_router.get(
    "/sso",
    operation_id="org_sso_get",
    dependencies=_MANAGE,
    openapi_extra={"x-scope": "org:read", "x-permission": _PERMISSION},
)
async def org_sso_get(request: Request, ctx: ReadOrg) -> DataResponse[SsoConnectionView]:
    """The org's OIDC connection (never its client secret)."""
    _ensure_enabled(request)
    row = await _current(request, ctx.org_id)
    if row is None:
        raise AppError("NOT_FOUND", NOT_FOUND_MESSAGE)
    return DataResponse(data=_view(row))


@org_router.delete(
    "/sso",
    operation_id="org_sso_delete",
    dependencies=_MANAGE,
    openapi_extra={"x-scope": "org:write", "x-permission": _PERMISSION},
)
async def org_sso_delete(request: Request, ctx: WriteOrg) -> DataResponse[SsoConnectionDeleted]:
    """Remove the org's OIDC connection."""
    _ensure_enabled(request)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        deleted = await SsoConnectionRepo(session, ctx.org_id).delete()
    if deleted is None:
        raise AppError("NOT_FOUND", NOT_FOUND_MESSAGE)
    return DataResponse(data=SsoConnectionDeleted(id=deleted))
