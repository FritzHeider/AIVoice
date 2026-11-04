"""``va_db.pathway_publish.settle_regression_publish`` on a migrated database (specs/12 §4 step 2; Decision
P07-T11e2): a passed run publishes with the requester as the audit actor, other outcomes publish nothing, and
a settled run is never settled twice."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pathway_rows import locked_run, publish_of, seed_orders_pathway, seed_regression_run
from sqlalchemy import select
from tenant_rows import OrgFactory, open_database

from va_db.models import AuditLog, PathwayTestRun, PathwayTestRunStatus
from va_db.pathway_publish import SettleResult, publish_record, settle_regression_publish
from va_db.repositories import PathwayRepo, PathwayRunRepo
from va_db.session import Database
from va_domain.pathway_tests import PublishActor, RegressionPublish

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def draft_hash(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID) -> str:
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        return (await repo.draft(await repo.get(pathway_id))).graph_hash


def intent(org_id: uuid.UUID, graph_hash: str, *, version: int = 1) -> RegressionPublish:
    return RegressionPublish(
        environment="production",
        version_number=version,
        graph_hash=graph_hash,
        published_by=None,
        actor=PublishActor(
            org_id=org_id, actor_type="api_key", actor_id=uuid.uuid4(), ip="203.0.113.7", user_agent="ua"
        ),
    )


async def settle(database: Database, org_id: uuid.UUID, run_id: uuid.UUID, wanted: RegressionPublish) -> Any:
    async with database.tenant_session(org_id) as session:
        return await settle_regression_publish(session, org_id, run_id, wanted, now=NOW)


async def pins(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID) -> tuple[int | None, int]:
    async with database.tenant_session(org_id) as session:
        pathway = await PathwayRepo(session, org_id).get(pathway_id)
        return pathway.production_version, pathway.draft_version


async def publish_audits(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID) -> list[AuditLog]:
    async with database.system_session() as session:
        rows = await session.scalars(
            select(AuditLog).where(
                AuditLog.org_id == org_id,
                AuditLog.resource_id == str(pathway_id),
                AuditLog.action == "pathway.publish",
            )
        )
        return list(rows)


async def setup(database: Database) -> tuple[uuid.UUID, uuid.UUID, str]:
    org_id = await OrgFactory(database).create()
    pathway_id = await seed_orders_pathway(database, org_id)
    return org_id, pathway_id, await draft_hash(database, org_id, pathway_id)


async def test_a_passed_run_publishes_in_the_requesters_name(database: Database) -> None:
    org_id, pathway_id, graph_hash = await setup(database)
    wanted = intent(org_id, graph_hash)
    run_id = await seed_regression_run(database, org_id, pathway_id, wanted, "passed")
    assert await settle(database, org_id, run_id, wanted) == SettleResult("published", 1, 2)
    assert await pins(database, org_id, pathway_id) == (1, 2)
    assert (await locked_run(database, org_id, run_id)).publish == {
        "block": True,
        "environment": "production",
        "version_number": 1,
        "graph_hash": graph_hash,
        "outcome": "published",
        "published_version": 1,
        "draft_version": 2,
        "settled_at": NOW.isoformat(),
    }
    (audit,) = await publish_audits(database, org_id, pathway_id)
    assert (audit.actor_type, audit.actor_id, audit.ip, audit.user_agent) == (
        "api_key",
        wanted.actor.actor_id,
        ipaddress.ip_address("203.0.113.7"),
        "ua",
    )
    # A retried settle returns the stored outcome and publishes nothing more.
    assert await settle(database, org_id, run_id, wanted) == SettleResult("published", 1, 2)
    assert len(await publish_audits(database, org_id, pathway_id)) == 1
    assert await pins(database, org_id, pathway_id) == (1, 2)


@pytest.mark.parametrize("status", ["failed", "error"])
async def test_a_run_that_did_not_pass_blocks_the_publish(
    database: Database, status: PathwayTestRunStatus
) -> None:
    org_id, pathway_id, graph_hash = await setup(database)
    wanted = intent(org_id, graph_hash)
    run_id = await seed_regression_run(database, org_id, pathway_id, wanted, status)
    assert await settle(database, org_id, run_id, wanted) == SettleResult("blocked")
    assert await pins(database, org_id, pathway_id) == (None, 1)
    assert (await publish_of(database, org_id, run_id))["outcome"] == "blocked"
    assert await publish_audits(database, org_id, pathway_id) == []


async def test_a_graph_edited_during_the_run_is_stale(database: Database) -> None:
    org_id, pathway_id, _ = await setup(database)
    wanted = intent(org_id, "f" * 64)
    run_id = await seed_regression_run(database, org_id, pathway_id, wanted, "passed")
    assert await settle(database, org_id, run_id, wanted) == SettleResult("stale")
    assert await pins(database, org_id, pathway_id) == (None, 1)


async def test_a_missing_version_or_pathway_is_unavailable(database: Database) -> None:
    org_id, pathway_id, graph_hash = await setup(database)
    missing = intent(org_id, graph_hash, version=9)
    run_id = await seed_regression_run(database, org_id, pathway_id, missing, "passed")
    assert await settle(database, org_id, run_id, missing) == SettleResult("version_unavailable")
    wanted = intent(org_id, graph_hash)
    deleted_run = await seed_regression_run(database, org_id, pathway_id, wanted, "passed")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        await repo.soft_delete(await repo.get(pathway_id))
    assert await settle(database, org_id, deleted_run, wanted) == SettleResult("version_unavailable")
    assert await publish_audits(database, org_id, pathway_id) == []


async def test_a_run_in_progress_stays_pending_and_a_gone_run_is_none(database: Database) -> None:
    org_id, pathway_id, graph_hash = await setup(database)
    wanted = intent(org_id, graph_hash)
    run_id = await seed_regression_run(database, org_id, pathway_id, wanted, "running")
    assert await settle(database, org_id, run_id, wanted) == SettleResult("pending")
    assert (await publish_of(database, org_id, run_id))["outcome"] == "pending"
    assert await settle(database, org_id, uuid.uuid4(), wanted) is None
    assert await pins(database, org_id, pathway_id) == (None, 1)


def test_publish_record_omits_unset_fields() -> None:
    assert publish_record(
        block=False, environment="staging", version_number=2, graph_hash="h", outcome="published"
    ) == {
        "block": False,
        "environment": "staging",
        "version_number": 2,
        "graph_hash": "h",
        "outcome": "published",
    }


async def test_a_run_without_publish_stores_sql_null(database: Database) -> None:
    org_id, pathway_id, _ = await setup(database)
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).add(pathway_id, version_number=1, kind="unit")
        stored = await session.scalar(
            select(PathwayTestRun.publish.is_(None)).where(PathwayTestRun.id == run.id)
        )
    assert stored is True
