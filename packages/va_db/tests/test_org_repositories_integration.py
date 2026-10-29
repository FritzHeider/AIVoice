"""``OrgRepo`` on the global session and the tenant ``ApiKeyRepo`` (specs/03 §2; specs/04 §8.1)."""

from __future__ import annotations

import secrets
import uuid

import pytest
from identity_db import IdentityDb
from model_factories import flushed, make_org

from va_common.ids import new_id
from va_db.models import ApiKey
from va_db.repositories import ApiKeyRepo, OrgRepo

pytestmark = pytest.mark.integration


async def new_org(identity_db: IdentityDb, **values: object) -> uuid.UUID:
    async with identity_db.database.system_session() as session:
        return (await make_org(session, **values)).id


async def test_overview_joins_the_plan(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db, plan_id="build", limits={"concurrency": 7})
    async with identity_db.database.global_session() as session:
        overview = await OrgRepo(session).overview(org_id)
    assert overview is not None
    assert overview.org.id == org_id
    assert overview.org.limits == {"concurrency": 7}
    assert overview.plan is not None
    assert overview.plan.id == "build"


async def test_overview_without_a_plan_row_and_unknown_org(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db, plan_id="legacy-custom")
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        overview = await repo.overview(org_id)
        assert await repo.overview(new_id()) is None
        assert await repo.get(new_id()) is None
    assert overview is not None
    assert overview.plan is None


async def test_update_sets_only_the_given_columns(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db, retention_days=200, recording_retention_days=30)
    other_id = await new_org(identity_db)
    async with identity_db.database.global_session() as session:
        before = await OrgRepo(session).get(org_id)
    assert before is not None
    async with identity_db.database.global_session() as session:
        updated = await OrgRepo(session).update(org_id, name="Acme Dental West", recording_retention_days=0)
    assert updated is not None
    assert (updated.name, updated.retention_days) == ("Acme Dental West", 200)
    assert updated.recording_retention_days == 0
    assert updated.updated_at >= before.updated_at
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        other = await repo.get(other_id)
        stored = await repo.get(org_id)
    assert other is not None
    assert other.name == "Acme Dental"
    assert stored is not None
    assert stored.name == "Acme Dental West"


async def test_update_with_nothing_returns_the_org(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        unchanged = await repo.update(org_id)
        missing = await repo.update(new_id(), name="Nobody")
    assert unchanged is not None
    assert unchanged.name == "Acme Dental"
    assert missing is None


async def test_api_key_repo_reads_only_its_org(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    other_id = await new_org(identity_db)
    prefix = f"va_live_{secrets.token_hex(2)}"
    async with identity_db.database.system_session() as session:
        key = await flushed(
            session, ApiKey(org_id=org_id, name="server", prefix=prefix, secret_hash="ab" * 32, scopes=["*"])
        )
    async with identity_db.database.tenant_session(org_id) as session:
        found = await ApiKeyRepo(session, org_id).get(key.id)
    assert found is not None
    assert (found.prefix, found.scopes) == (prefix, ["*"])
    async with identity_db.database.tenant_session(other_id) as session:
        assert await ApiKeyRepo(session, other_id).get(key.id) is None
