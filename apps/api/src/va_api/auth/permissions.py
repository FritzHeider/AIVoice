"""Fine-grained permissions above read/write scopes (specs/04-auth-tenancy-rbac.md §3.3).

Routers declare ``dependencies=[Depends(require_permission("<name>"))]`` next to their ``require_scope``; the
dependency it returns carries ``required_permission`` so tooling can find it on ``route.dependant``. Session
principals hold the permissions of their role; API keys hold every permission except the session-only ones
when they carry ``*``, and none otherwise; widgets hold none. The names and the role grants come from the §3.2
matrix in :mod:`va_api.auth.rbac_matrix`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Final

from fastapi import Depends

from va_api.auth.context import RequestContext
from va_api.auth.dependencies import get_principal
from va_api.auth.rbac_matrix import PERMISSIONS, ROLES, SESSION_ONLY_PERMISSIONS, role_permissions
from va_api.auth.scopes import ALL_SCOPES
from va_common.errors import AppError
from va_db.models import MemberRole

__all__ = [
    "PERMISSIONS",
    "REQUIRED_PERMISSION_ATTR",
    "ROLE_PERMISSIONS",
    "SESSION_ONLY_PERMISSIONS",
    "holds_permission",
    "require_permission",
]

REQUIRED_PERMISSION_ATTR: Final = "required_permission"

ROLE_PERMISSIONS: Final[Mapping[MemberRole, frozenset[str]]] = MappingProxyType(
    {role: role_permissions(role) for role in ROLES}
)


def holds_permission(ctx: RequestContext, permission: str) -> bool:
    """Whether the principal of ``ctx`` holds ``permission``."""
    if ctx.principal_type in {"user", "platform_admin"}:
        return ctx.role is not None and permission in ROLE_PERMISSIONS[ctx.role]
    if ctx.principal_type == "api_key":
        return ALL_SCOPES in ctx.scopes and permission not in SESSION_ONLY_PERMISSIONS
    return False


def require_permission(permission: str) -> Callable[..., Awaitable[RequestContext]]:
    """A dependency granting access only to principals holding ``permission``; else ``FORBIDDEN``.

    ``permission`` must be one of specs/04 §3.3, checked when the router is defined.
    """
    if permission not in PERMISSIONS:
        msg = f"unknown permission: {permission!r}"
        raise ValueError(msg)

    async def check_permission(ctx: Annotated[RequestContext, Depends(get_principal)]) -> RequestContext:
        if not holds_permission(ctx, permission):
            raise AppError("FORBIDDEN", f"Missing required permission '{permission}'.")
        return ctx

    setattr(check_permission, REQUIRED_PERMISSION_ATTR, permission)
    return check_permission
