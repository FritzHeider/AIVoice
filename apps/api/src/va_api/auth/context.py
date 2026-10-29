"""The resolved request principal (specs/04-auth-tenancy-rbac.md §1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from va_api.auth.scopes import ALL_SCOPES
from va_common.auth import KeyEnvironment
from va_db.models import MemberRole

__all__ = ["PrincipalType", "RequestContext", "Role"]

type Role = MemberRole
type PrincipalType = Literal["api_key", "user", "widget", "platform_admin"]


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is calling, for which org, with which scopes; every repository call receives ``org_id``."""

    request_id: str
    org_id: uuid.UUID
    principal_type: PrincipalType
    principal_id: uuid.UUID  # api_keys.id | users.id | widgets.id
    role: Role | None  # users only
    scopes: frozenset[str]  # api keys: key scopes; users: scopes derived from role (specs/04 §3.3)
    environment: KeyEnvironment
    plan_id: str
    ip: str | None
    user_agent: str | None
    acting_parent_org_id: uuid.UUID | None  # set when a parent-org user/key acts on a sub-account

    def has_scope(self, scope: str) -> bool:
        """Whether ``scope`` is granted, directly or through ``*``."""
        return ALL_SCOPES in self.scopes or scope in self.scopes
