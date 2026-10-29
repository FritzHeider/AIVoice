"""``POST /internal/auth/switch-org`` (specs/04-auth-tenancy-rbac.md §4.1, §8.3).

The route attaches to :data:`va_api.routers.auth.internal_auth_router`; ``va_api.main`` imports this module so
it exists before the router is included. The session must pass the MFA gate of its current active org; the
target is an org the user is a member of, or a direct sub-account of an org where the user is owner or admin
(the session then acts in the child with the parent role and ``acting_parent_org_id``). The target's own
status is not checked here: principal routes apply it (and a suspended parent's) on every later request. A
closed parent grants no access, so switching into its child is refused with ``ORG_SUSPENDED``.
"""

from __future__ import annotations

from typing import Final

from fastapi import Request, Response

from va_api.auth.session_auth import check_org_status, gated_membership, resolve_switch_target, sessions_of
from va_api.responses import DataResponse
from va_api.routers.auth import PUBLIC_MARKER, CurrentSession, internal_auth_router, session_info
from va_api.routers.auth_schemas import SessionInfo, SwitchOrgRequest
from va_common.errors import AppError

__all__ = ["SWITCH_FORBIDDEN_MESSAGE", "switch_org"]

SWITCH_FORBIDDEN_MESSAGE: Final = "You do not have access to this organization."


@internal_auth_router.post("/switch-org", operation_id="auth_switch_org", openapi_extra=PUBLIC_MARKER)
async def switch_org(
    body: SwitchOrgRequest, request: Request, response: Response, active: CurrentSession
) -> DataResponse[SessionInfo]:
    """Make `org_id` the session's active org (a membership, or a sub-account of an org where the user is
    owner or admin); returns the same body as `GET /internal/auth/session`."""
    active, _ = await gated_membership(request, active)
    target = await resolve_switch_target(request, active.user_id, body.org_id)
    if target is None:
        raise AppError("FORBIDDEN", SWITCH_FORBIDDEN_MESSAGE)
    if target.acting_parent_status is not None:
        check_org_status(target.acting_parent_status, "GET")
    if active.active_org_id != target.org_id:
        active = await sessions_of(request).set_active_org(active, target.org_id)
    return DataResponse(data=await session_info(request, response, active))
