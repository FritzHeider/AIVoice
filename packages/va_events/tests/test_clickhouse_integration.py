"""The packaged migrations against a real ClickHouse (``clickhouse/clickhouse-server:24.8``, as compose).

One test on purpose: under ``pytest -n auto`` every xdist worker that runs a test of this module would boot
its own container.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Final

import pytest
from ch_support import spec_columns
from docker_probe import docker_available

from va_common.time import FrozenClock
from va_events.clickhouse import (
    ClickHouseSchemaDriftError,
    apply_migrations,
    load_migrations,
    open_clickhouse,
)
from va_events.clickhouse.schema import ClickHouseCommands, Migration

# Container creation alone outran the 60 s default while the full suite booted one Postgres per xdist worker.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

CLICKHOUSE_IMAGE: Final = "clickhouse/clickhouse-server:24.8"
EXPECTED_ENGINES: Final = {
    "call_events": "MergeTree",
    "call_turns": "MergeTree",
    "calls_fact": "ReplacingMergeTree",
    "usage_events_raw": "MergeTree",
    "eval_metrics": "MergeTree",
    "schema_migrations": "ReplacingMergeTree",
}


@pytest.fixture(scope="module")
def clickhouse_url() -> Iterator[str]:
    """A disposable server whose own database is not ``va`` (the applier must create it)."""
    if not docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.community.clickhouse import (  # noqa: PLC0415 - imported only when Docker exists
        ClickHouseContainer,
    )

    password = secrets.token_hex(16)
    with ClickHouseContainer(CLICKHOUSE_IMAGE, username="sink", password=password, dbname="sink") as server:
        host, port = server.get_container_host_ip(), server.get_exposed_port(8123)
        yield f"http://sink:{password}@{host}:{port}/sink"


async def columns_by_table(client: ClickHouseCommands) -> dict[str, list[tuple[str, str]]]:
    result = await client.query(
        "SELECT table, name, type FROM system.columns WHERE database = 'va' AND table != 'schema_migrations' "
        "ORDER BY table, position"
    )
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, name, column_type in result.result_rows:
        tables.setdefault(table, []).append((name, column_type))
    return tables


async def engines(client: ClickHouseCommands) -> dict[str, str]:
    result = await client.query("SELECT name, engine FROM system.tables WHERE database = 'va'")
    return {str(name): str(engine) for name, engine in result.result_rows}


async def assert_schema_matches_spec(
    client: ClickHouseCommands, initial: Migration, clock: FrozenClock
) -> None:
    assert await columns_by_table(client) == spec_columns()
    assert await engines(client) == EXPECTED_ENGINES
    recorded = await client.query("SELECT version, name, checksum, applied_at FROM va.schema_migrations")
    ((version, name, checksum, applied_at),) = recorded.result_rows
    assert (version, name, checksum) == ("0001", "init", initial.checksum)
    assert applied_at.replace(tzinfo=None) == clock.now().replace(tzinfo=None)


async def test_migrations_apply_once_and_match_the_spec(clickhouse_url: str) -> None:
    (initial,) = load_migrations()
    clock = FrozenClock()
    async with open_clickhouse(clickhouse_url) as client:
        assert await apply_migrations(client, clock=clock) == ["0001"]
        assert await apply_migrations(client, clock=clock) == []
        await assert_schema_matches_spec(client, initial, clock)

        await client.command(
            "INSERT INTO va.schema_migrations (version, name, checksum, applied_at) "
            "SELECT '0001', 'init', 'tampered', now64(3, 'UTC') + 1"
        )
        with pytest.raises(ClickHouseSchemaDriftError, match="0001"):
            await apply_migrations(client, clock=clock)
