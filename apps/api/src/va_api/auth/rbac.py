"""Role → scope derivation for session principals (specs/04-auth-tenancy-rbac.md §3.3).

A role holds ``<resource>:read`` when any §3.2 row of that resource family is readable for it and
``<resource>:write`` when any row is writable or executable; actions finer than read/write stay behind
permissions (added with the RBAC matrix). Sessions never receive ``*`` or ``mcp``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from va_api.auth.scopes import READ_WRITE_RESOURCES
from va_db.models import MemberRole

__all__ = ["ROLE_SCOPES", "scopes_for_role"]


def _scopes(resources: tuple[str, ...], access: str) -> frozenset[str]:
    return frozenset(f"{resource}:{access}" for resource in resources)


_ALL_READ: Final = _scopes(READ_WRITE_RESOURCES, "read")
_ALL_WRITE: Final = _scopes(READ_WRITE_RESOURCES, "write")
_ANALYTICS: Final = frozenset({"analytics:read"})
_MANAGER: Final = _ALL_READ | _ALL_WRITE | _ANALYTICS | {"api_keys:write"}

ROLE_SCOPES: Final[Mapping[MemberRole, frozenset[str]]] = MappingProxyType(
    {
        "owner": _MANAGER,
        "admin": _MANAGER,
        "developer": _MANAGER - {"billing:write", "org:write"},
        "analyst": _ALL_READ | _ANALYTICS | {"calls:write", "pathways:write", "analysis:write"},
        "billing": frozenset({"billing:read", "billing:write", "org:read"}) | _ANALYTICS,
        "viewer": (_ALL_READ - {"billing:read"}) | _ANALYTICS,
    }
)


def scopes_for_role(role: MemberRole) -> frozenset[str]:
    """The scopes a session principal with ``role`` holds."""
    return ROLE_SCOPES[role]
