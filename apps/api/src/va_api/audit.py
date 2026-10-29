"""Audit rows from API routes (specs/04-auth-tenancy-rbac.md §13; specs/28-security.md §10).

Routes call :func:`audit` inside the ``tenant_session`` (or global session) that makes the change, before
it closes, so the row commits with the change and a failed request leaves none. The actor comes from the
request context: an API key is ``api_key`` with the key id, a session user is ``user`` with the user id, and
a platform admin is ``user`` with ``changes.platform_admin = true``. While a parent principal acts on a
sub-account the row lands in the sub-account's log with ``changes.acting_parent_org_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_db.audit import AuditActor, Changes, ResourceId, record
from va_db.models import AuditLog

__all__ = ["actor_of", "audit", "changed"]


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
