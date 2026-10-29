"""Role → scope derivation for session principals (specs/04-auth-tenancy-rbac.md §3.3)."""

from __future__ import annotations

from typing import get_args

from va_api.auth.rbac import ROLE_SCOPES, scopes_for_role
from va_api.auth.scopes import READ_WRITE_RESOURCES, SCOPES
from va_db.models import MemberRole

ALL_READ = {f"{resource}:read" for resource in READ_WRITE_RESOURCES}
ALL_WRITE = {f"{resource}:write" for resource in READ_WRITE_RESOURCES}
MANAGER = ALL_READ | ALL_WRITE | {"api_keys:write", "analytics:read"}


def test_every_role_is_mapped() -> None:
    assert set(ROLE_SCOPES) == set(get_args(MemberRole))


def test_exact_scope_sets() -> None:
    assert scopes_for_role("owner") == MANAGER
    assert scopes_for_role("admin") == MANAGER
    assert scopes_for_role("developer") == MANAGER - {"billing:write", "org:write"}
    assert scopes_for_role("analyst") == ALL_READ | {
        "calls:write",
        "pathways:write",
        "analysis:write",
        "analytics:read",
    }
    assert scopes_for_role("billing") == {"billing:read", "billing:write", "org:read", "analytics:read"}
    assert scopes_for_role("viewer") == (ALL_READ - {"billing:read"}) | {"analytics:read"}


def test_scopes_are_known_and_never_wildcard_or_mcp() -> None:
    for scopes in ROLE_SCOPES.values():
        assert scopes <= SCOPES
        assert "*" not in scopes
        assert "mcp" not in scopes
