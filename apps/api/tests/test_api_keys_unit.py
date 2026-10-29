"""Key-management rules of ``/v1/api_keys`` without a database (specs/04-auth-tenancy-rbac.md §9, §3.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from va_api.auth.api_keys import (
    EXPIRES_AT_PAST_MESSAGE,
    NOT_OWN_KEY_MESSAGE,
    TEST_KEYS_ONLY_MESSAGE,
    check_environment,
    check_expires_at,
    check_grantable,
    check_manages,
    grantable_scopes,
    normalize_scopes,
    ungrantable_message,
)
from va_api.auth.context import PrincipalType, RequestContext
from va_api.auth.rbac import ROLE_SCOPES
from va_api.auth.scopes import SCOPES
from va_common.auth import KeyEnvironment
from va_common.errors import AppError
from va_db.models import ApiKey, MemberRole

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def context(
    principal_type: PrincipalType,
    *,
    role: MemberRole | None = None,
    scopes: frozenset[str] = frozenset(),
    environment: KeyEnvironment = "live",
) -> RequestContext:
    if principal_type == "user" and role is not None:
        scopes = ROLE_SCOPES[role]
    return RequestContext(
        request_id="req_1",
        org_id=uuid.uuid4(),
        principal_type=principal_type,
        principal_id=uuid.uuid4(),
        role=role,
        scopes=scopes,
        environment=environment,
        plan_id="build",
        ip=None,
        user_agent=None,
        acting_parent_org_id=None,
    )


def key_by(created_by: uuid.UUID | None) -> ApiKey:
    return ApiKey(
        name="server", prefix="va_live_3kQ9", scopes=["*"], environment="live", created_by=created_by
    )


def error(raised: pytest.ExceptionInfo[AppError]) -> tuple[str, str, str | None]:
    return raised.value.code, raised.value.message, raised.value.field


def test_star_key_grants_every_scope_and_narrow_key_only_its_own() -> None:
    assert grantable_scopes(context("api_key", scopes=frozenset({"*"}))) == SCOPES
    narrow = frozenset({"api_keys:write", "calls:read"})
    assert grantable_scopes(context("api_key", scopes=narrow)) == narrow


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_sessions_grant_every_scope(role: MemberRole) -> None:
    grantable = grantable_scopes(context("user", role=role))
    assert grantable == SCOPES
    assert {"*", "mcp"} <= grantable


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
def test_other_roles_grant_their_role_scopes(role: MemberRole) -> None:
    grantable = grantable_scopes(context("user", role=role))
    assert grantable == ROLE_SCOPES[role]
    assert "*" not in grantable


def test_widgets_grant_nothing() -> None:
    assert grantable_scopes(context("widget", scopes=frozenset({"widgets:read"}))) == frozenset()


def test_normalize_dedupes_sorts_and_collapses_star() -> None:
    assert normalize_scopes(["calls:write", "calls:read", "calls:write"]) == ["calls:read", "calls:write"]
    assert normalize_scopes(["calls:read", "*", "mcp"]) == ["*"]


def test_normalize_rejects_the_first_unknown_scope() -> None:
    with pytest.raises(AppError) as raised:
        normalize_scopes(["calls:read", "zeta:write", "calls:delete"])
    assert error(raised) == ("INVALID_INPUT", "Unknown scope 'calls:delete'.", "scopes")


def test_check_grantable_names_every_missing_scope() -> None:
    ctx = context("api_key", scopes=frozenset({"api_keys:write", "calls:read"}))
    check_grantable(ctx, ["calls:read"])
    with pytest.raises(AppError) as raised:
        check_grantable(ctx, ["calls:write", "*", "calls:read"])
    assert error(raised) == ("FORBIDDEN", ungrantable_message(["calls:write", "*"]), None)
    assert raised.value.message == "Cannot grant scopes you do not hold: *, calls:write."


def test_test_principals_manage_only_test_keys() -> None:
    check_environment(context("api_key", environment="live"), "test")
    check_environment(context("api_key", environment="live"), "live")
    check_environment(context("api_key", environment="test"), "test")
    with pytest.raises(AppError) as raised:
        check_environment(context("api_key", environment="test"), "live")
    assert error(raised) == ("FORBIDDEN", TEST_KEYS_ONLY_MESSAGE, None)


def test_expires_at_must_be_in_the_future() -> None:
    check_expires_at(None, NOW)
    check_expires_at(NOW + timedelta(seconds=1), NOW)
    for past in (NOW, NOW - timedelta(days=1)):
        with pytest.raises(AppError) as raised:
            check_expires_at(past, NOW)
        assert error(raised) == ("INVALID_INPUT", EXPIRES_AT_PAST_MESSAGE, "expires_at")


def test_developers_manage_only_their_own_keys() -> None:
    developer = context("user", role="developer")
    check_manages(developer, key_by(developer.principal_id))
    for created_by in (uuid.uuid4(), None):
        with pytest.raises(AppError) as raised:
            check_manages(developer, key_by(created_by))
        assert error(raised) == ("FORBIDDEN", NOT_OWN_KEY_MESSAGE, None)


def test_managers_and_api_keys_manage_every_key() -> None:
    for ctx in (
        context("user", role="owner"),
        context("user", role="admin"),
        context("api_key", scopes=frozenset({"api_keys:write"})),
    ):
        check_manages(ctx, key_by(uuid.uuid4()))
        check_manages(ctx, key_by(None))
