"""0002_seed_plans rendered offline, and the specs/24-billing-and-limits.md §2 catalogue it seeds."""

from __future__ import annotations

import re
from types import ModuleType
from typing import Final

from migration_helpers import load_revision, offline_sql
from plan_catalog import CAP_COLUMNS, paragraph_values, parse_spec_plans, table_cells, table_prices

SEED: Final[ModuleType] = load_revision("0002_seed_plans.py")


def test_spec_lists_the_four_plans_in_seed_order() -> None:
    assert list(parse_spec_plans()) == ["start", "build", "scale", "enterprise"]
    assert tuple(parse_spec_plans()) == SEED.PLAN_IDS


def _table_cells_by_kind(*, custom: bool) -> dict[tuple[str, str], int | None]:
    """(plan id, price column) -> table micro-dollars, for the "custom" cells or for the stated ones."""
    return {
        (plan_id, column): micros
        for plan_id, prices in table_prices().items()
        for column, micros in prices.items()
        if (micros is None) is custom
    }


def test_table_dollar_cells_agree_with_the_micro_dollar_seed_values() -> None:
    seeds = paragraph_values()
    stated = _table_cells_by_kind(custom=False)
    assert len(stated) == 19
    assert {(plan_id, column): seeds[plan_id][column] for plan_id, column in stated} == stated


def test_only_enterprise_prices_are_custom() -> None:
    assert set(_table_cells_by_kind(custom=True)) == {
        ("enterprise", "monthly_fee_micros"),
        ("enterprise", "connected_rate_micros_per_min"),
        ("enterprise", "transfer_rate_micros_per_min"),
        ("enterprise", "sms_rate_micros"),
        ("enterprise", "widget_message_micros"),
    }


def test_enterprise_resolves_custom_cells_and_unlimited_caps() -> None:
    enterprise = parse_spec_plans()["enterprise"]
    prices = (
        enterprise.monthly_fee_micros,
        enterprise.connected_rate_micros_per_min,
        enterprise.transfer_rate_micros_per_min,
        enterprise.sms_rate_micros,
        enterprise.widget_message_micros,
    )
    assert prices == (0, 80_000, 25_000, 20_000, 10_000)
    assert [getattr(enterprise, column) for column in CAP_COLUMNS] == [None] * len(CAP_COLUMNS)
    assert (len(enterprise.features), enterprise.features[-1]) == (12, "custom_llm")


def test_every_plan_carries_the_tts_standalone_price() -> None:
    assert {plan.tts_rate_micros_per_1k_chars for plan in parse_spec_plans().values()} == {120_000}


def test_spec_table_rows_have_every_column() -> None:
    assert {len(cells) for cells in table_cells()} == {14}
    assert parse_spec_plans()["start"].features == ()
    assert parse_spec_plans()["build"].daily_call_cap == 2_000


def test_offline_upgrade_inserts_the_plans_after_the_schema() -> None:
    sql = offline_sql("upgrade", "0001:0002")
    assert sql.count("INSERT INTO plans (") == 1
    assert "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001'" in sql
    assert "CREATE TABLE" not in sql
    rows = re.findall(r"^\s*\('(\w+)', '(\w+)',", sql, re.MULTILINE)
    assert rows == [(plan.id, plan.name) for plan in parse_spec_plans().values()]


def test_offline_downgrade_deletes_exactly_the_seeded_plans() -> None:
    sql = offline_sql("downgrade", "0002:0001")
    assert "DELETE FROM plans WHERE id IN ('start', 'build', 'scale', 'enterprise')" in sql
    assert "DROP" not in sql
    assert "UPDATE alembic_version SET version_num='0001' WHERE alembic_version.version_num = '0002'" in sql


def test_revision_chain() -> None:
    assert (SEED.revision, SEED.down_revision) == ("0002", "0001")
