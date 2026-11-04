"""``va_db.pathway_publish.apply_publish`` on a migrated database (specs/12 §4 step 3; Decisions P07-T09b and
P07-T11e1): freeze, pin, fork the draft, and the ``pathway.publish`` audit row."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from pathway_rows import seed_orders_pathway
from sqlalchemy import select
from tenant_rows import OrgFactory, RowFactory, open_database

from va_db.audit import AuditActor
from va_db.base import Base
from va_db.models import AuditLog
from va_db.pathway_publish import apply_publish, pinned_number
from va_db.repositories import PathwayRepo
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def new_user(database: Database) -> uuid.UUID:
    async with database.system_session() as session:
        key = await RowFactory().insert(session, Base.metadata.tables["users"], None)
        user_id: uuid.UUID = key["id"]
        return user_id


def actor(org_id: uuid.UUID) -> AuditActor:
    return AuditActor(org_id=org_id, actor_type="api_key", actor_id=uuid.uuid4())


async def publish_rows(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID) -> list[AuditLog]:
    async with database.system_session() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.org_id == org_id, AuditLog.resource_id == str(pathway_id))
            .where(AuditLog.action == "pathway.publish")
            .order_by(AuditLog.created_at)
        )
        return list(rows)


async def test_publishing_the_draft_pins_it_forks_a_draft_and_audits(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await seed_orders_pathway(database, org_id)
    user = await new_user(database)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id, lock=True)
        draft = await repo.draft(pathway)
        result = await apply_publish(
            session, repo, pathway, draft, environment="production", published_by=user, actor=actor(org_id)
        )
        assert (result.published_version, result.environment, result.draft_version, result.previous) == (
            1,
            "production",
            2,
            None,
        )
        assert (draft.status, draft.published_by) == ("published", user)
        assert pinned_number(pathway, "production") == 1
        assert pinned_number(pathway, "staging") is None
        new_draft = await repo.draft(pathway)
        assert (new_draft.version_number, new_draft.graph_hash) == (2, draft.graph_hash)
    rows = await publish_rows(database, org_id, pathway_id)
    assert [row.changes for row in rows] == [
        {
            "before": {"environment": "production", "version": None},
            "after": {"environment": "production", "version": 1},
        }
    ]


async def test_publishing_a_saved_version_opens_no_draft_and_keeps_first_publisher(
    database: Database,
) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await seed_orders_pathway(database, org_id)
    first, second = await new_user(database), await new_user(database)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id, lock=True)
        v1 = await repo.draft(pathway)
        await apply_publish(
            session, repo, pathway, v1, environment="staging", published_by=first, actor=actor(org_id)
        )
        published_at = v1.published_at
        result = await apply_publish(
            session, repo, pathway, v1, environment="production", published_by=second, actor=actor(org_id)
        )
        assert (result.published_version, result.draft_version, result.previous) == (1, 2, None)
        assert pathway.latest_version == 2
        assert (v1.published_by, v1.published_at) == (first, published_at)
        assert (pathway.staging_version, pathway.production_version) == (1, 1)
    rows = await publish_rows(database, org_id, pathway_id)
    # One transaction: both rows share now(), so compare without order.
    assert sorted(row.changes["after"]["environment"] for row in rows) == ["production", "staging"]
