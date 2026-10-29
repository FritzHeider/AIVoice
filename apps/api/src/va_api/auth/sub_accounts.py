"""Parent principals acting on a sub-account with ``X-VA-Org-Id`` (specs/04-auth-tenancy-rbac.md §8.3).

After authentication, :func:`act_on_sub_account` switches the request context to the child org named by the
header: only API keys holding ``org:write`` and owner/admin session principals may, only for a direct child of
the org they act in, and the child's status (and, for sessions, its MFA policy) gates the request like the
principal's own org would. The new context keeps the principal, scopes, and role and records the parent in
``acting_parent_org_id``; the rate limit then uses the child's plan and ``limits.rate_per_sec``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Final

from fastapi import Request

from va_api.auth.context import RequestContext
from va_api.auth.session_auth import (
    MFA_REQUIRED_MESSAGE,
    check_org_status,
    database_of,
    mfa_due,
)
from va_api.auth.sessions import ActiveSession
from va_common.errors import AppError
from va_db.models import Org
from va_db.repositories import PARENT_ACCESS_ROLES, OrgRepo
from va_db.repositories.api_keys import rate_per_sec_override
from va_db.repositories.memberships import MFA_REQUIRED_KEY

__all__ = [
    "ACT_FORBIDDEN_MESSAGE",
    "INVALID_ORG_HEADER_MESSAGE",
    "NOT_A_CHILD_MESSAGE",
    "ORG_HEADER",
    "act_on_sub_account",
]

ORG_HEADER: Final = "x-va-org-id"
ACT_FORBIDDEN_MESSAGE: Final = (
    "Acting on a sub-account requires the org:write scope or the owner or admin role."
)
NOT_A_CHILD_MESSAGE: Final = "Organization is not a sub-account of the caller."
INVALID_ORG_HEADER_MESSAGE: Final = "X-VA-Org-Id must be an organization id."
_ORG_WRITE: Final = "org:write"


def _target(request: Request) -> uuid.UUID | None:
    """The org id of a non-blank ``X-VA-Org-Id``; ``INVALID_INPUT`` when it is not a UUID."""
    raw = (request.headers.get(ORG_HEADER) or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError("INVALID_INPUT", INVALID_ORG_HEADER_MESSAGE, field="X-VA-Org-Id") from None


def _may_act(ctx: RequestContext) -> bool:
    """API keys need ``org:write`` (or ``*``); other principals need the owner or admin role."""
    if ctx.principal_type == "api_key":
        return ctx.has_scope(_ORG_WRITE)
    return ctx.role in PARENT_ACCESS_ROLES


def _check_child(child: Org, request: Request, ctx: RequestContext) -> None:
    """The child's status gate and, for session principals, its MFA policy."""
    check_org_status(child.status, request.method)
    active = getattr(request.state, "session", None)
    if ctx.principal_type != "user" or not isinstance(active, ActiveSession):
        return
    requires = child.settings.get(MFA_REQUIRED_KEY) is True
    if mfa_due(org_requires=requires, user_enabled=False, passed=active.mfa_passed):
        raise AppError("MFA_REQUIRED", MFA_REQUIRED_MESSAGE)


async def act_on_sub_account(
    request: Request, ctx: RequestContext, rate_per_sec: int | None
) -> tuple[RequestContext, int | None]:
    """The context and ``rate_per_sec`` override for the org named by ``X-VA-Org-Id`` (inputs unchanged
    without the header or when it names the context's own org).

    ``FORBIDDEN`` when the principal may not act on sub-accounts, when the context already acts in a child,
    or when the org is not a direct child of ``ctx.org_id``; ``ORG_SUSPENDED`` / ``MFA_REQUIRED`` from the
    child's gates.
    """
    target = _target(request)
    if target is None or target == ctx.org_id:
        return ctx, rate_per_sec
    if not _may_act(ctx):
        raise AppError("FORBIDDEN", ACT_FORBIDDEN_MESSAGE)
    if ctx.acting_parent_org_id is not None:
        raise AppError("FORBIDDEN", NOT_A_CHILD_MESSAGE)
    async with database_of(request).global_session() as session:
        child = await OrgRepo(session).child(ctx.org_id, target)
    if child is None:
        raise AppError("FORBIDDEN", NOT_A_CHILD_MESSAGE)
    _check_child(child, request, ctx)
    acting = replace(ctx, org_id=child.id, plan_id=child.plan_id, acting_parent_org_id=ctx.org_id)
    return acting, rate_per_sec_override(child.limits)
