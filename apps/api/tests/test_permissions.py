"""Permission names and role grants (specs/04-auth-tenancy-rbac.md §3.3)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from va_api.auth.context import PrincipalType, RequestContext
from va_api.auth.permissions import (
    PERMISSIONS,
    REQUIRED_PERMISSION_ATTR,
    ROLE_PERMISSIONS,
    SESSION_ONLY_PERMISSIONS,
    holds_permission,
    require_permission,
)
from va_common.errors import AppError
from va_db.models import MemberRole

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"


def _context(
    principal_type: PrincipalType, *, role: MemberRole | None = None, scopes: frozenset[str] = frozenset()
) -> RequestContext:
    return RequestContext(
        request_id="req_1",
        org_id=uuid.uuid4(),
        principal_type=principal_type,
        principal_id=uuid.uuid4(),
        role=role,
        scopes=scopes,
        environment="live",
        plan_id="build",
        ip=None,
        user_agent=None,
        acting_parent_org_id=None,
    )


def _spec_permissions() -> set[str]:
    text = " ".join(SPEC_04.read_text(encoding="utf-8").split())
    match = re.search(r"permission names: (.*?) API keys with", text)
    assert match is not None
    return set(re.findall(r"`([a-z_0-9]+\.[a-z_]+)`", match.group(1)))


def test_permissions_match_the_spec_list() -> None:
    assert len(PERMISSIONS) == 20
    assert _spec_permissions() == PERMISSIONS
    assert {"org.delete", "org.owner_transfer", "sso.manage"} == SESSION_ONLY_PERMISSIONS


def test_role_permissions_are_exact() -> None:
    assert ROLE_PERMISSIONS["owner"] == PERMISSIONS
    assert ROLE_PERMISSIONS["admin"] == PERMISSIONS - {
        "org.delete",
        "org.owner_transfer",
        "billing.plan_change",
    }
    assert ROLE_PERMISSIONS["developer"] == {
        "pathways.publish",
        "voices.clone",
        "experiments.operate",
        "calls.operate",
    }
    assert ROLE_PERMISSIONS["analyst"] == frozenset()
    assert ROLE_PERMISSIONS["billing"] == {
        "billing.plan_change",
        "billing.payment",
        "billing.credit_transfer",
    }
    assert ROLE_PERMISSIONS["viewer"] == frozenset()
    assert all(granted <= PERMISSIONS for granted in ROLE_PERMISSIONS.values())


@pytest.mark.parametrize("permission", sorted(PERMISSIONS))
def test_wildcard_api_key_holds_everything_but_session_only(permission: str) -> None:
    ctx = _context("api_key", scopes=frozenset({"*"}))
    assert holds_permission(ctx, permission) is (permission not in SESSION_ONLY_PERMISSIONS)


def test_scoped_api_key_and_widget_hold_nothing() -> None:
    scoped = _context("api_key", scopes=frozenset({"calls:write", "org:write"}))
    widget = _context("widget", scopes=frozenset({"*"}))
    assert not any(holds_permission(scoped, name) for name in PERMISSIONS)
    assert not any(holds_permission(widget, name) for name in PERMISSIONS)


def test_users_hold_their_role_permissions() -> None:
    assert holds_permission(_context("user", role="admin"), "sso.manage")
    assert not holds_permission(_context("user", role="admin"), "org.delete")
    assert holds_permission(_context("platform_admin", role="owner"), "org.delete")
    assert not holds_permission(_context("user", role=None), "calls.operate")


def test_unknown_permission_is_a_definition_error() -> None:
    with pytest.raises(ValueError, match=r"unknown permission: 'sso\.write'"):
        require_permission("sso.write")


async def test_dependency_checks_and_carries_the_permission() -> None:
    check = require_permission("sso.manage")
    assert getattr(check, REQUIRED_PERMISSION_ATTR) == "sso.manage"
    owner = _context("user", role="owner")
    assert await check(ctx=owner) is owner
    with pytest.raises(AppError) as caught:
        await check(ctx=_context("api_key", scopes=frozenset({"*"})))
    assert caught.value.code == "FORBIDDEN"
    assert caught.value.message == "Missing required permission 'sso.manage'."
