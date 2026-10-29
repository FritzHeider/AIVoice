"""Role → scope derivation for session principals (specs/04-auth-tenancy-rbac.md §3.3).

The scopes are derived from the §3.2 matrix in :mod:`va_api.auth.rbac_matrix`: a role holds
``<resource>:read`` when any row of that resource family is readable for it and ``<resource>:write`` when any
row is writable or executable; actions finer than read/write stay behind permissions. Sessions never receive
``*`` or ``mcp``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from va_api.auth.rbac_matrix import ROLES, role_scopes
from va_db.models import MemberRole

__all__ = ["ROLE_SCOPES", "scopes_for_role"]

ROLE_SCOPES: Final[Mapping[MemberRole, frozenset[str]]] = MappingProxyType(
    {role: role_scopes(role) for role in ROLES}
)


def scopes_for_role(role: MemberRole) -> frozenset[str]:
    """The scopes a session principal with ``role`` holds."""
    return ROLE_SCOPES[role]
