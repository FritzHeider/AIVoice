"""0002_seed_plans on real Postgres: the plans rows equal the specs/24-billing-and-limits.md §2 catalogue."""

from __future__ import annotations

import uuid
from typing import Final

import pytest
from migration_helpers import query
from plan_catalog import SPEC_PLAN_COLUMNS, parse_spec_plans
from sqlalchemy import select
from tenant_rows import open_database

from va_db.models import Plan

pytestmark = pytest.mark.integration

SEEDED_SQL: Final = f"SELECT {', '.join(SPEC_PLAN_COLUMNS)} FROM plans ORDER BY id"  # noqa: S608  (constant column list)


def test_plans_rows_equal_the_spec_catalogue(migrated_url: str) -> None:
    expected = sorted(plan.as_row() for plan in parse_spec_plans().values())
    assert query(migrated_url, SEEDED_SQL) == expected


def test_seeded_plans_are_public_without_stripe_prices(migrated_url: str) -> None:
    assert query(migrated_url, "SELECT DISTINCT is_public, stripe_price_id FROM plans") == [(True, None)]


async def test_roles_read_the_catalogue(migrated_url: str) -> None:
    async with open_database(migrated_url) as database:
        async with database.system_session() as session:
            system_ids = set((await session.scalars(select(Plan.id))).all())
        # plans has no RLS: va_app reads the platform catalogue from any org's session.
        async with database.tenant_session(uuid.uuid4()) as session:
            tenant_ids = set((await session.scalars(select(Plan.id))).all())
    assert system_ids == tenant_ids == set(parse_spec_plans())
