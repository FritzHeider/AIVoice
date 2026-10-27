from __future__ import annotations

import re
from typing import Any, get_args

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB
from sqlalchemy.schema import CreateIndex

from va_db.base import Base
from va_db.models import (
    ApiKeyEnvironment,
    AuditActorType,
    DataRegion,
    EmailTokenPurpose,
    MemberRole,
    OrgStatus,
    SsoProvider,
    UserStatus,
)

_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]

_TABLES = (
    "orgs",
    "users",
    "memberships",
    "invitations",
    "sessions",
    "email_tokens",
    "api_keys",
    "sso_connections",
    "audit_logs",
    "idempotency_keys",
)

# Column order and nullability exactly as in specs/03-data-model.md §3 (True = nullable).
_COLUMNS: dict[str, list[tuple[str, bool]]] = {
    "orgs": [
        ("id", False),
        ("parent_org_id", True),
        ("name", False),
        ("slug", False),
        ("plan_id", False),
        ("kyc_level", False),
        ("status", False),
        ("credits_balance_micros", False),
        ("credit_limit_micros", False),
        ("auto_recharge_enabled", False),
        ("auto_recharge_threshold_micros", False),
        ("auto_recharge_amount_micros", False),
        ("stripe_customer_id", True),
        ("limits", False),
        ("settings", False),
        ("data_region", False),
        ("retention_days", False),
        ("recording_retention_days", False),
        ("webhook_signing_secret_enc", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "users": [
        ("id", False),
        ("email", False),
        ("name", False),
        ("password_hash", True),
        ("email_verified_at", True),
        ("mfa_totp_secret_enc", True),
        ("mfa_enabled", False),
        ("mfa_recovery_codes_hash", False),
        ("is_platform_admin", False),
        ("last_login_at", True),
        ("status", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "memberships": [
        ("id", False),
        ("org_id", False),
        ("user_id", False),
        ("role", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "invitations": [
        ("id", False),
        ("org_id", False),
        ("email", False),
        ("role", False),
        ("token_hash", False),
        ("invited_by", False),
        ("expires_at", False),
        ("accepted_at", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "sessions": [
        ("id", False),
        ("user_id", False),
        ("token_hash", False),
        ("active_org_id", True),
        ("ip", True),
        ("user_agent", True),
        ("mfa_passed", False),
        ("expires_at", False),
        ("revoked_at", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "email_tokens": [
        ("id", False),
        ("user_id", False),
        ("purpose", False),
        ("token_hash", False),
        ("expires_at", False),
        ("used_at", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "api_keys": [
        ("id", False),
        ("org_id", False),
        ("name", False),
        ("prefix", False),
        ("secret_hash", False),
        ("scopes", False),
        ("pepper_version", False),
        ("environment", False),
        ("created_by", True),
        ("last_used_at", True),
        ("expires_at", True),
        ("revoked_at", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "sso_connections": [
        ("id", False),
        ("org_id", False),
        ("provider", False),
        ("issuer", False),
        ("client_id", False),
        ("client_secret_enc", False),
        ("allowed_domains", False),
        ("default_role", False),
        ("enforce", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "audit_logs": [
        ("id", False),
        ("org_id", False),
        ("actor_type", False),
        ("actor_id", True),
        ("action", False),
        ("resource_type", False),
        ("resource_id", True),
        ("ip", True),
        ("user_agent", True),
        ("changes", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "idempotency_keys": [
        ("org_id", False),
        ("key", False),
        ("method", False),
        ("path", False),
        ("request_hash", False),
        ("response_status", True),
        ("response_body", True),
        ("locked_until", True),
        ("created_at", False),
        ("updated_at", False),
    ],
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _constraint(table: str, name: str) -> Any:
    matches = [c for c in _table(table).constraints if c.name == name]
    assert len(matches) == 1, f"{table}: expected one constraint named {name}"
    return matches[0]


def test_tables_registered() -> None:
    assert set(_TABLES) <= set(Base.metadata.tables)


@pytest.mark.parametrize("table", _TABLES)
def test_columns_order_and_nullability(table: str) -> None:
    assert [(column.name, column.nullable) for column in _table(table).columns] == _COLUMNS[table]


@pytest.mark.parametrize(
    ("table", "column", "literal", "expected"),
    [
        ("orgs", "status", OrgStatus, ("active", "suspended", "closed")),
        ("orgs", "data_region", DataRegion, ("us", "eu")),
        ("users", "status", UserStatus, ("active", "disabled")),
        ("memberships", "role", MemberRole, ("owner", "admin", "developer", "analyst", "billing", "viewer")),
        ("invitations", "role", MemberRole, ("owner", "admin", "developer", "analyst", "billing", "viewer")),
        ("email_tokens", "purpose", EmailTokenPurpose, ("verify_email", "reset_password")),
        ("api_keys", "environment", ApiKeyEnvironment, ("live", "test")),
        ("sso_connections", "provider", SsoProvider, ("oidc",)),
        ("audit_logs", "actor_type", AuditActorType, ("user", "api_key", "system")),
    ],
)
def test_enum_checks_mirror_literals(
    table: str, column: str, literal: Any, expected: tuple[str, ...]
) -> None:
    constraint = _constraint(table, f"{table}_{column}_check")
    assert isinstance(constraint, CheckConstraint)
    sql = str(constraint.sqltext)
    assert sql.startswith(f"{column} IN (")
    assert tuple(re.findall(r"'([^']*)'", sql)) == get_args(literal) == expected


def test_check_constraints_are_exactly_the_spec_set() -> None:
    names = {
        constraint.name
        for table in _TABLES
        for constraint in _table(table).constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert names == {
        "orgs_kyc_level_check",
        "orgs_status_check",
        "orgs_data_region_check",
        "orgs_retention_days_check",
        "orgs_recording_retention_days_check",
        "users_status_check",
        "memberships_role_check",
        "invitations_role_check",
        "email_tokens_purpose_check",
        "api_keys_environment_check",
        "sso_connections_provider_check",
        "audit_logs_actor_type_check",
    }


@pytest.mark.parametrize(
    ("name", "sql"),
    [
        ("orgs_kyc_level_check", "kyc_level BETWEEN 0 AND 3"),
        ("orgs_retention_days_check", "retention_days BETWEEN 1 AND 3650"),
        ("orgs_recording_retention_days_check", "recording_retention_days BETWEEN 0 AND 3650"),
    ],
)
def test_range_checks(name: str, sql: str) -> None:
    assert str(_constraint("orgs", name).sqltext) == sql


_SERVER_DEFAULTS: dict[tuple[str, str], str] = {
    ("orgs", "plan_id"): "'start'",
    ("orgs", "kyc_level"): "0",
    ("orgs", "status"): "'active'",
    ("orgs", "credits_balance_micros"): "0",
    ("orgs", "credit_limit_micros"): "0",
    ("orgs", "auto_recharge_enabled"): "false",
    ("orgs", "auto_recharge_threshold_micros"): "5000000",
    ("orgs", "auto_recharge_amount_micros"): "25000000",
    ("orgs", "limits"): "'{}'::jsonb",
    ("orgs", "settings"): "'{}'::jsonb",
    ("orgs", "data_region"): "'us'",
    ("orgs", "retention_days"): "365",
    ("orgs", "recording_retention_days"): "90",
    ("users", "name"): "''",
    ("users", "mfa_enabled"): "false",
    ("users", "mfa_recovery_codes_hash"): "'{}'",
    ("users", "is_platform_admin"): "false",
    ("users", "status"): "'active'",
    ("sessions", "mfa_passed"): "false",
    ("api_keys", "scopes"): "'{*}'",
    ("api_keys", "pepper_version"): "1",
    ("api_keys", "environment"): "'live'",
    ("sso_connections", "allowed_domains"): "'{}'",
    ("sso_connections", "default_role"): "'viewer'",
    ("sso_connections", "enforce"): "false",
    ("audit_logs", "changes"): "'{}'::jsonb",
}


def test_server_defaults() -> None:
    actual = {
        (table, column.name): str(column.server_default.arg.compile(dialect=_DIALECT))  # type: ignore[attr-defined]
        for table in _TABLES
        for column in _table(table).columns
        if column.server_default is not None and column.name not in {"created_at", "updated_at"}
    }
    assert actual == _SERVER_DEFAULTS


@pytest.mark.parametrize("table", _TABLES)
def test_timestamps_and_ids(table: str) -> None:
    columns = _table(table).c
    for name in ("created_at", "updated_at"):
        assert str(columns[name].server_default.arg.compile(dialect=_DIALECT)) == "now()"  # type: ignore[union-attr]
    if table != "idempotency_keys":
        assert columns.id.primary_key
        assert columns.id.server_default is None
        assert columns.id.default is not None


def test_unique_and_primary_key_names() -> None:
    found = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for table in _TABLES
        for constraint in _table(table).constraints
        if isinstance(constraint, UniqueConstraint | PrimaryKeyConstraint)
    }
    assert found == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES if table != "idempotency_keys"),
        ("idempotency_keys_pkey", ("org_id", "key")),
        ("orgs_slug_key", ("slug",)),
        ("orgs_stripe_customer_id_key", ("stripe_customer_id",)),
        ("users_email_key", ("email",)),
        ("memberships_org_id_user_id_key", ("org_id", "user_id")),
        ("invitations_token_hash_key", ("token_hash",)),
        ("sessions_token_hash_key", ("token_hash",)),
        ("email_tokens_token_hash_key", ("token_hash",)),
        ("api_keys_prefix_key", ("prefix",)),
        ("sso_connections_org_id_issuer_key", ("org_id", "issuer")),
    }


def test_foreign_keys_ondelete() -> None:
    found = set()
    for table in _TABLES:
        for constraint in _table(table).constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                (element,) = constraint.elements
                found.add(
                    (
                        constraint.name,
                        table,
                        element.parent.name,
                        element.target_fullname,
                        constraint.ondelete,
                    )
                )
    assert found == {
        ("orgs_parent_org_id_fkey", "orgs", "parent_org_id", "orgs.id", "RESTRICT"),
        ("memberships_org_id_fkey", "memberships", "org_id", "orgs.id", "CASCADE"),
        ("memberships_user_id_fkey", "memberships", "user_id", "users.id", "CASCADE"),
        ("invitations_org_id_fkey", "invitations", "org_id", "orgs.id", "CASCADE"),
        ("invitations_invited_by_fkey", "invitations", "invited_by", "users.id", None),
        ("sessions_user_id_fkey", "sessions", "user_id", "users.id", "CASCADE"),
        ("sessions_active_org_id_fkey", "sessions", "active_org_id", "orgs.id", "SET NULL"),
        ("email_tokens_user_id_fkey", "email_tokens", "user_id", "users.id", "CASCADE"),
        ("api_keys_org_id_fkey", "api_keys", "org_id", "orgs.id", "CASCADE"),
        ("api_keys_created_by_fkey", "api_keys", "created_by", "users.id", "SET NULL"),
        ("sso_connections_org_id_fkey", "sso_connections", "org_id", "orgs.id", "CASCADE"),
        ("audit_logs_org_id_fkey", "audit_logs", "org_id", "orgs.id", "CASCADE"),
        ("idempotency_keys_org_id_fkey", "idempotency_keys", "org_id", "orgs.id", "CASCADE"),
    }


def test_indexes() -> None:
    ddl = {
        " ".join(str(CreateIndex(index).compile(dialect=_DIALECT)).split())
        for table in _TABLES
        for index in _table(table).indexes
    }
    assert ddl == {
        "CREATE INDEX orgs_parent_idx ON orgs (parent_org_id)",
        "CREATE INDEX memberships_user_idx ON memberships (user_id)",
        "CREATE INDEX invitations_org_idx ON invitations (org_id)",
        "CREATE INDEX sessions_user_idx ON sessions (user_id)",
        "CREATE INDEX api_keys_org_idx ON api_keys (org_id)",
        "CREATE INDEX audit_logs_org_created_idx ON audit_logs (org_id, created_at DESC)",
    }


@pytest.mark.parametrize(
    ("table", "column", "sql_type"),
    [
        ("orgs", "slug", CITEXT),
        ("users", "email", CITEXT),
        ("invitations", "email", CITEXT),
        ("sessions", "ip", INET),
        ("audit_logs", "ip", INET),
        ("orgs", "limits", JSONB),
        ("orgs", "settings", JSONB),
        ("audit_logs", "changes", JSONB),
        ("idempotency_keys", "response_body", JSONB),
        ("orgs", "webhook_signing_secret_enc", LargeBinary),
        ("users", "mfa_totp_secret_enc", LargeBinary),
        ("sso_connections", "client_secret_enc", LargeBinary),
        ("orgs", "kyc_level", SmallInteger),
        ("api_keys", "pepper_version", SmallInteger),
        ("orgs", "retention_days", Integer),
        ("idempotency_keys", "response_status", Integer),
        ("orgs", "credits_balance_micros", BigInteger),
        ("orgs", "auto_recharge_amount_micros", BigInteger),
        ("orgs", "auto_recharge_enabled", Boolean),
        ("orgs", "parent_org_id", Uuid),
        ("audit_logs", "actor_id", Uuid),
        ("orgs", "status", Text),
        ("sso_connections", "default_role", Text),
        ("sessions", "expires_at", DateTime),
    ],
)
def test_column_types(table: str, column: str, sql_type: type[Any]) -> None:
    column_type = _table(table).c[column].type
    assert type(column_type) is sql_type
    if isinstance(column_type, DateTime):
        assert column_type.timezone is True


@pytest.mark.parametrize(
    ("table", "column"),
    [("users", "mfa_recovery_codes_hash"), ("api_keys", "scopes"), ("sso_connections", "allowed_domains")],
)
def test_text_array_columns(table: str, column: str) -> None:
    column_type = _table(table).c[column].type
    assert isinstance(column_type, ARRAY)
    assert type(column_type.item_type) is Text


def test_every_timestamp_is_timezone_aware() -> None:
    for table in _TABLES:
        for column in _table(table).columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True, f"{table}.{column.name}"
