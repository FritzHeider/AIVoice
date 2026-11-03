"""Webhook signing secret under ``/v1/webhooks/signing-secret`` (specs/19-events-and-webhooks.md §10).

The router also carries the endpoint and delivery routes of :mod:`va_api.routers.webhook_endpoints`.

Viewing needs ``webhooks:read`` and rotating ``webhooks:write`` (specs/04 §2.3), and both need the
``webhooks.secret`` permission (§3.2 "Webhook signing secret view last4 / rotate": owners, admins, API keys
with ``*``). ``orgs`` has no RLS, so the row is read in a ``global_session`` with ``ctx.org_id`` only; while a
parent acts on a sub-account the sub-account's secret is used. Rotation keeps the old secret signing for 24 h
(:mod:`va_db.webhook_secrets`); the new secret is in the first response only (``null`` on a replay), and the
audit row records last four characters, never a secret.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Request

from va_api.audit import audit, audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.permissions import require_permission
from va_api.auth.session_auth import clock_of, database_of, envelope_of
from va_api.envelopes import envelope_router
from va_api.idempotency import withhold_on_replay
from va_api.responses import DataResponse
from va_api.routers.webhooks_schemas import (
    RotatedSigningSecret,
    SigningSecretView,
    rotated_signing_secret,
    signing_secret_view,
)
from va_db.repositories import NotFoundError, OrgRepo
from va_db.webhook_secrets import SecretInfo, rotate_signing_secret, secret_info

__all__ = ["webhooks_router", "webhooks_signing_secret_get", "webhooks_signing_secret_rotate"]

_PERMISSION: Final = "webhooks.secret"
_ROTATE_ACTION: Final = "webhook.secret_rotate"

webhooks_router = envelope_router(prefix="/v1/webhooks", tags=["Webhooks"])

ReadWebhooks = Annotated[RequestContext, Depends(require_scope("webhooks:read"))]
WriteWebhooks = Annotated[RequestContext, Depends(require_scope("webhooks:write"))]


def _audited_view(info: SecretInfo) -> dict[str, object]:
    return {
        "secret_last4": info.secret_last4,
        "created_at": info.created_at,
        "previous_secret_last4": info.previous_secret_last4,
        "previous_expires_at": info.previous_expires_at,
    }


@webhooks_router.get(
    "/signing-secret",
    operation_id="webhooks_signing_secret_get",
    dependencies=[Depends(require_permission(_PERMISSION))],
    openapi_extra={"x-scope": "webhooks:read", "x-permission": _PERMISSION},
)
async def webhooks_signing_secret_get(request: Request, ctx: ReadWebhooks) -> DataResponse[SigningSecretView]:
    """The current secret's last four characters and issue time, plus the previous one's while it signs."""
    async with database_of(request).global_session() as session:
        org = await OrgRepo(session).get(ctx.org_id)
        if org is None:
            raise NotFoundError("Org")
        info = await secret_info(envelope_of(request), org, now=clock_of(request).now())
    return DataResponse(data=signing_secret_view(info))


@webhooks_router.post(
    "/signing-secret/rotate",
    operation_id="webhooks_signing_secret_rotate",
    dependencies=[Depends(require_permission(_PERMISSION))],
    openapi_extra={"x-scope": "webhooks:write", "x-permission": _PERMISSION, **audited(_ROTATE_ACTION)},
)
async def webhooks_signing_secret_rotate(
    request: Request, ctx: WriteWebhooks
) -> DataResponse[RotatedSigningSecret]:
    """Issue a new signing secret, returned only here; the old one keeps signing (second `v1=`) for 24 h."""
    withhold_on_replay(request, "data.secret")
    async with database_of(request).global_session() as session:
        rotated = await rotate_signing_secret(
            session, envelope_of(request), ctx.org_id, now=clock_of(request).now()
        )
        await audit(
            session,
            ctx,
            _ROTATE_ACTION,
            "org",
            ctx.org_id,
            before=_audited_view(rotated.before),
            after=_audited_view(rotated.after),
        )
    return DataResponse(data=rotated_signing_secret(rotated))
