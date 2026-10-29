"""Audit rows from API routes (specs/04-auth-tenancy-rbac.md §13; specs/28-security.md §10).

Routes call :func:`audit` inside the ``tenant_session`` (or global session) that makes the change, before
it closes, so the row commits with the change and a failed request leaves none. The actor comes from the
request context: an API key is ``api_key`` with the key id, a session user is ``user`` with the user id, and
a platform admin is ``user`` with ``changes.platform_admin = true``. While a parent principal acts on a
sub-account the row lands in the sub-account's log with ``changes.acting_parent_org_id``.

Dashboard auth routes (``/internal/auth/*``) have no request context: :func:`audit_user` writes a user-level
event once in each org where the user has a membership. Every route with a mutating method declares what it
logs in ``openapi_extra``: :func:`audited` names the §13 actions it may write, :func:`audit_exempt` gives the
reason it writes none (``tests/test_audit_markers.py`` enforces one of the two on every route).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Final

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_api.auth.session_auth import client_ip
from va_db.audit import AUDIT_ACTIONS, AuditActor, Changes, ResourceId, record
from va_db.models import AuditLog

__all__ = [
    "AUDIT_EXEMPT_KEY",
    "AUDIT_KEY",
    "actor_of",
    "audit",
    "audit_exempt",
    "audit_user",
    "audited",
    "changed",
    "user_actor",
]

AUDIT_KEY: Final = "x-audit"
AUDIT_EXEMPT_KEY: Final = "x-audit-exempt"


def audited(*actions: str) -> dict[str, list[str]]:
    """The ``openapi_extra`` marker of a route that may write ``actions`` (each one of §13's)."""
    if not actions:
        msg = "audited() needs at least one action"
        raise ValueError(msg)
    unknown = [action for action in actions if action not in AUDIT_ACTIONS]
    if unknown:
        msg = f"unknown audit actions: {unknown!r}"
        raise ValueError(msg)
    return {AUDIT_KEY: list(dict.fromkeys(actions))}


def audit_exempt(reason: str) -> dict[str, str]:
    """The ``openapi_extra`` marker of a mutating route that writes no audit row, with the reason."""
    if not reason.strip():
        msg = "audit_exempt() needs a reason"
        raise ValueError(msg)
    return {AUDIT_EXEMPT_KEY: reason}


def changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The entries of ``before`` and ``after`` whose values differ, by key; two empty dicts when none do."""
    names = [name for name in dict.fromkeys([*before, *after]) if before.get(name) != after.get(name)]
    return {name: before.get(name) for name in names}, {name: after.get(name) for name in names}


def actor_of(ctx: RequestContext) -> AuditActor:
    """The audit actor of ``ctx`` in ``ctx.org_id``; widgets never write audit rows (``ValueError``)."""
    if ctx.principal_type == "widget":
        msg = "widget principals do not write audit rows"
        raise ValueError(msg)
    return AuditActor(
        org_id=ctx.org_id,
        actor_type="api_key" if ctx.principal_type == "api_key" else "user",
        actor_id=ctx.principal_id,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        acting_parent_org_id=ctx.acting_parent_org_id,
        platform_admin=ctx.principal_type == "platform_admin",
    )


async def audit(  # noqa: PLR0913 - the argument list of specs/04 §13 record()
    session: AsyncSession,
    ctx: RequestContext,
    action: str,
    resource_type: str,
    resource_id: ResourceId,
    *,
    before: Changes = None,
    after: Changes = None,
) -> AuditLog:
    """Record ``action`` by the principal of ``ctx`` in ``ctx.org_id``'s log, in ``session``'s transaction.

    The session may run in another org (``/v1/orgs`` routes change a sub-account in its own transaction and
    log in the calling parent org); :func:`va_db.audit.record` sets ``app.org_id`` for the insert.
    """
    return await record(
        session, actor_of(ctx), action, resource_type, resource_id, before=before, after=after
    )


def user_actor(request: Request, org_id: uuid.UUID, user_id: uuid.UUID) -> AuditActor:
    """The dashboard user ``user_id`` acting from ``request``, logged in ``org_id``."""
    return AuditActor(
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


async def audit_user(  # noqa: PLR0913 - record() arguments plus the orgs
    session: AsyncSession,
    request: Request,
    org_ids: Iterable[uuid.UUID],
    user_id: uuid.UUID,
    action: str,
    *,
    before: Changes = None,
    after: Changes = None,
) -> list[AuditLog]:
    """Record the user-level ``action`` of ``user_id`` once in each distinct org of ``org_ids``.

    The resource is the user. No org (a user without memberships) writes nothing.
    """
    return [
        await record(
            session,
            user_actor(request, org_id, user_id),
            action,
            "user",
            user_id,
            before=before,
            after=after,
        )
        for org_id in dict.fromkeys(org_ids)
    ]
