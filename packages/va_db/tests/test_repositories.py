"""Repository guards that fire before any SQL, and the system-scope list of specs/03-data-model.md §2."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from model_introspection import SPEC_03
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import va_db.repositories
from va_common.ids import new_id
from va_db.base import Base
from va_db.models import Call, IdempotencyKey, NumberPoolMember, Org, Plan, Prompt, StripeEvent, VoiceRating
from va_db.repositories import (
    IMMUTABLE_COLUMNS,
    PURGE_RULES,
    ApiKeyRecord,
    BillingReconcileRepo,
    NotFoundError,
    RetentionRepo,
    SchedulerRepo,
    SystemRepository,
    TenantRepository,
    is_unique_violation,
    require_positive,
)
from va_db.repositories.api_keys import rate_per_sec_override


@pytest.mark.parametrize("model", [IdempotencyKey, VoiceRating, Org, Plan, NumberPoolMember, StripeEvent])
def test_tenant_repository_rejects_tables_without_id_org_id_created_at(model: type[Base]) -> None:
    with pytest.raises(TypeError, match=f"{model.__tablename__} is not a tenant resource table"):
        TenantRepository(model, AsyncSession(), new_id())


async def test_soft_delete_requires_deleted_at() -> None:
    repo = TenantRepository(Call, AsyncSession(), new_id())
    with pytest.raises(TypeError, match="calls has no deleted_at column"):
        await repo.soft_delete(new_id())


@pytest.mark.parametrize(
    ("values", "rejected"),
    [
        ({"org_id": new_id()}, "org_id"),
        ({"created_at": None}, "created_at"),
        ({"updated_at": None}, "updated_at"),
        ({"deleted_at": None}, "deleted_at"),
        ({"nickname": "x", "name": "y"}, "nickname"),
    ],
)
async def test_create_rejects_immutable_and_unknown_keys(values: dict[str, Any], rejected: str) -> None:
    repo = TenantRepository(Prompt, AsyncSession(), new_id())
    with pytest.raises(ValueError, match=f"prompts does not accept {rejected}"):
        await repo.create(values)


@pytest.mark.parametrize("key", sorted(IMMUTABLE_COLUMNS))
async def test_update_rejects_immutable_keys(key: str) -> None:
    repo = TenantRepository(Prompt, AsyncSession(), new_id())
    with pytest.raises(ValueError, match=f"prompts does not accept {key}"):
        await repo.update(new_id(), {key: None})


def test_not_found_error_never_names_the_row() -> None:
    error = NotFoundError("prompts")
    assert (error.code, error.http_status, error.message, error.resource) == (
        "NOT_FOUND",
        404,
        "prompts not found.",
        "prompts",
    )
    assert error.to_error_item() == {"error": "NOT_FOUND", "message": "prompts not found."}


class _DriverError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(("sqlstate", "expected"), [("23505", True), ("23514", False), (None, False)])
def test_is_unique_violation_reads_the_sqlstate(sqlstate: str | None, *, expected: bool) -> None:
    assert is_unique_violation(IntegrityError("INSERT", {}, _DriverError(sqlstate))) is expected


def test_require_positive() -> None:
    assert require_positive("limit", 1) == 1
    with pytest.raises(ValueError, match="limit must be at least 1, got 0"):
        require_positive("limit", 0)


def _spec_system_repos() -> set[str]:
    """Backticked ``...Repo`` names of the specs/03 §2 ``va_system`` bullet."""
    text = SPEC_03.read_text(encoding="utf-8")
    bullet = re.search(r"^- `va_system`.*?(?=^\S|^$)", text, flags=re.MULTILINE | re.DOTALL)
    assert bullet is not None
    return set(re.findall(r"`(\w+Repo)`", bullet.group(0)))


def test_system_repositories_are_exactly_the_spec_list() -> None:
    subclasses = SystemRepository.__subclasses__()
    assert {cls.__name__ for cls in subclasses} == _spec_system_repos()
    assert len(_spec_system_repos()) == 7
    assert all(cls.system_scope for cls in subclasses)
    assert all(name in va_db.repositories.__all__ for name in _spec_system_repos())


def test_purge_rules_name_real_columns() -> None:
    assert [rule.table for rule in PURGE_RULES] == [
        "idempotency_keys",
        "telnyx_events",
        "stripe_events",
        "webhook_deliveries",
        "sessions",
        "email_tokens",
        "invitations",
    ]
    for rule in PURGE_RULES:
        assert rule.column in Base.metadata.tables[rule.table].c


async def test_system_repositories_reject_non_positive_sizes() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        await RetentionRepo(AsyncSession()).purge_expired(now, batch_size=0)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await SchedulerRepo(AsyncSession()).due_scheduled_calls(now, limit=0)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await BillingReconcileRepo(AsyncSession()).calls_ended_between(now, now, limit=0)


@pytest.mark.parametrize(("expires", "rate_per_sec"), [(True, None), (False, None), (False, 450)])
def test_api_key_record_json_round_trip(expires: bool, rate_per_sec: int | None) -> None:
    record = ApiKeyRecord(
        id=new_id(),
        org_id=new_id(),
        secret_hash="cd" * 32,
        scopes=("*",),
        environment="live",
        pepper_version=1,
        revoked=False,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=5) if expires else None,
        org_status="active",
        plan_id="start",
        rate_per_sec=rate_per_sec,
    )
    data = record.to_json()
    assert ApiKeyRecord.from_json(data) == record
    assert isinstance(data["id"], str)
    assert data["expires_at"] == ("2026-01-01T05:00:00+00:00" if expires else None)
    assert data["rate_per_sec"] == rate_per_sec


def test_api_key_record_from_cache_entry_without_rate_per_sec() -> None:
    record = ApiKeyRecord(
        id=new_id(),
        org_id=new_id(),
        secret_hash="ef" * 32,
        scopes=("calls:read",),
        environment="test",
        pepper_version=1,
        revoked=True,
        expires_at=None,
        org_status="suspended",
        plan_id="enterprise",
        rate_per_sec=9,
    )
    data = record.to_json()
    del data["rate_per_sec"]
    assert ApiKeyRecord.from_json(data).rate_per_sec is None


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"rate_per_sec": 450}, 450),
        ({"rate_per_sec": 1, "daily_cap": 5}, 1),
        ({}, None),
        ({"rate_per_sec": 0}, None),
        ({"rate_per_sec": -3}, None),
        ({"rate_per_sec": "5"}, None),
        ({"rate_per_sec": 5.0}, None),
        ({"rate_per_sec": True}, None),
        ({"rate_per_sec": None}, None),
        ([], None),
        (None, None),
    ],
)
def test_rate_per_sec_override(limits: object, expected: int | None) -> None:
    assert rate_per_sec_override(limits) == expected
