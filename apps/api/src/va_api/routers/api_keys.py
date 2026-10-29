"""API key management under ``/v1/api_keys`` (specs/04-auth-tenancy-rbac.md §9, §3.2 "API keys" rows).

Every route needs ``api_keys:write`` (§2.3 has no read scope; the §3.2 list row is held by exactly the roles
holding it). A caller grants only scopes it holds, a ``test`` principal manages only ``test`` keys, and a
developer manages only keys it created (:mod:`va_api.auth.api_keys`). While acting on a sub-account the keys
are the sub-account's. The plaintext of a created or rolled key is in the first response only; an
``Idempotency-Key`` replay carries ``"key": null``. Revoking or rolling drops the old key's cached lookup
after the transaction commits, so the change applies to the very next request.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.api_keys import (
    check_environment,
    check_expires_at,
    check_grantable,
    check_manages,
    creating_user_id,
    invalidate_cached_keys,
    mint_api_key,
    normalize_scopes,
)
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.session_auth import api_key_hasher_of, clock_of, database_of
from va_api.envelopes import envelope_router
from va_api.idempotency import withhold_on_replay
from va_api.responses import DataResponse
from va_api.routers.api_keys_schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyPatch,
    ApiKeyRoll,
    ApiKeyView,
    api_key_created,
    api_key_view,
)
from va_common.errors import AppError
from va_db.models import ApiKey
from va_db.repositories import ApiKeyRepo

__all__ = [
    "API_KEY_NOT_FOUND_MESSAGE",
    "NOT_ROLLABLE_MESSAGE",
    "api_keys_create",
    "api_keys_list",
    "api_keys_revoke",
    "api_keys_roll",
    "api_keys_router",
    "api_keys_update",
]

API_KEY_NOT_FOUND_MESSAGE: Final = "API key not found."
NOT_ROLLABLE_MESSAGE: Final = "A revoked or expired API key cannot be rolled."
_SCOPE: Final = "api_keys:write"
_EXTRA: Final = {"x-scope": _SCOPE}

api_keys_router = envelope_router(prefix="/v1/api_keys", tags=["API Keys"])

ManageKeys = Annotated[RequestContext, Depends(require_scope(_SCOPE))]


async def _managed_key(session: AsyncSession, ctx: RequestContext, key_id: uuid.UUID) -> ApiKey:
    """The org's key ``key_id`` when ``ctx`` may manage it; ``NOT_FOUND`` or ``FORBIDDEN`` otherwise."""
    key = await ApiKeyRepo(session, ctx.org_id).get(key_id)
    if key is None:
        raise AppError("NOT_FOUND", API_KEY_NOT_FOUND_MESSAGE)
    check_manages(ctx, key)
    return key


@api_keys_router.get("", operation_id="api_keys_list", openapi_extra=_EXTRA)
async def api_keys_list(request: Request, ctx: ManageKeys) -> DataResponse[list[ApiKeyView]]:
    """Every API key of the org by prefix, revoked and expired ones included, oldest first."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        views = [api_key_view(row) for row in await ApiKeyRepo(session, ctx.org_id).list_keys()]
    return DataResponse(data=views)


@api_keys_router.post("", operation_id="api_keys_create", status_code=201, openapi_extra=_EXTRA)
async def api_keys_create(
    body: ApiKeyCreate, request: Request, ctx: ManageKeys
) -> DataResponse[ApiKeyCreated]:
    """Create a key with scopes the caller holds; its plaintext ``key`` is returned only in this response."""
    withhold_on_replay(request, "data.key")
    scopes = normalize_scopes(body.scopes)
    check_grantable(ctx, scopes)
    environment = body.environment or ctx.environment
    check_environment(ctx, environment)
    check_expires_at(body.expires_at, clock_of(request).now())
    minted = mint_api_key(api_key_hasher_of(request), environment)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        row = await ApiKeyRepo(session, ctx.org_id).add(
            name=body.name,
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=scopes,
            environment=environment,
            created_by=await creating_user_id(ctx, session),
            expires_at=body.expires_at,
        )
        created = api_key_created(row, minted.plaintext)
    return DataResponse(data=created)


@api_keys_router.patch("/{key_id}", operation_id="api_keys_update", openapi_extra=_EXTRA)
async def api_keys_update(
    key_id: uuid.UUID, body: ApiKeyPatch, request: Request, ctx: ManageKeys
) -> DataResponse[ApiKeyView]:
    """Rename a key (revoked keys too); an empty body returns the key unchanged."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        row = await _managed_key(session, ctx, key_id)
        if body.name is not None:
            row = await ApiKeyRepo(session, ctx.org_id).rename(key_id, body.name) or row
        view = api_key_view(row)
    return DataResponse(data=view)


@api_keys_router.post("/{key_id}/revoke", operation_id="api_keys_revoke", openapi_extra=_EXTRA)
async def api_keys_revoke(key_id: uuid.UUID, request: Request, ctx: ManageKeys) -> DataResponse[ApiKeyView]:
    """Revoke a key at once; revoking it again returns it with its first ``revoked_at``."""
    now = clock_of(request).now()
    async with database_of(request).tenant_session(ctx.org_id) as session:
        row = await _managed_key(session, ctx, key_id)
        row = await ApiKeyRepo(session, ctx.org_id).revoke(key_id, now) or row
        view = api_key_view(row)
    await invalidate_cached_keys(request, [view.prefix])
    return DataResponse(data=view)


@api_keys_router.post("/{key_id}/roll", operation_id="api_keys_roll", openapi_extra=_EXTRA)
async def api_keys_roll(
    key_id: uuid.UUID, body: ApiKeyRoll, request: Request, ctx: ManageKeys
) -> DataResponse[ApiKeyCreated]:
    """Replace a key: a new key with the same name, scopes, and environment (plaintext only in this response);
    the old key expires after ``expire_old_in_hours`` unless it already expires sooner."""
    withhold_on_replay(request, "data.key")
    now = clock_of(request).now()
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = ApiKeyRepo(session, ctx.org_id)
        old = await _managed_key(session, ctx, key_id)
        if old.revoked_at is not None or (old.expires_at is not None and old.expires_at <= now):
            raise AppError("CONFLICT", NOT_ROLLABLE_MESSAGE)
        check_grantable(ctx, old.scopes)
        check_environment(ctx, old.environment)
        old_prefix = old.prefix
        old_expires_at = now + timedelta(hours=body.expire_old_in_hours)
        if old.expires_at is not None:
            old_expires_at = min(old.expires_at, old_expires_at)
        minted = mint_api_key(api_key_hasher_of(request), old.environment)
        row = await repo.add(
            name=old.name,
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=list(old.scopes),
            environment=old.environment,
            created_by=await creating_user_id(ctx, session),
        )
        await repo.expire(key_id, old_expires_at)
        created = api_key_created(row, minted.plaintext)
    await invalidate_cached_keys(request, [old_prefix])
    return DataResponse(data=created)
