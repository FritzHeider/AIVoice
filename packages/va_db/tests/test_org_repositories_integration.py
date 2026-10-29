"""``OrgRepo`` on the global session and the tenant ``ApiKeyRepo`` (specs/03 §2; specs/04 §8.1, §8.2)."""

from __future__ import annotations

import secrets
import uuid

import pytest
from identity_db import IdentityDb
from model_factories import flushed, make_org, make_user

from va_common.ids import new_id
from va_db.models import ApiKey
from va_db.repositories import ApiKeyRepo, OrgRepo

pytestmark = pytest.mark.integration


async def new_org(identity_db: IdentityDb, **values: object) -> uuid.UUID:
    async with identity_db.database.system_session() as session:
        return (await make_org(session, **values)).id


async def new_user(identity_db: IdentityDb) -> uuid.UUID:
    async with identity_db.database.system_session() as session:
        return (await make_user(session)).id


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


async def test_children_lists_only_direct_sub_accounts_oldest_first(identity_db: IdentityDb) -> None:
    parent_id = await new_org(identity_db)
    first = await new_org(identity_db, parent_org_id=parent_id, name="Austin")
    second = await new_org(identity_db, parent_org_id=parent_id, name="Dallas")
    await new_org(identity_db, parent_org_id=first, name="Grandchild")
    other_parent = await new_org(identity_db)
    await new_org(identity_db, parent_org_id=other_parent)
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        children = await repo.children(parent_id)
        assert await repo.children(second) == []
    assert [org.id for org in children] == [first, second]


async def test_child_finds_only_a_direct_sub_account(identity_db: IdentityDb) -> None:
    parent_id = await new_org(identity_db)
    child_id = await new_org(identity_db, parent_org_id=parent_id)
    sibling_parent = await new_org(identity_db)
    stranger = await new_org(identity_db, parent_org_id=sibling_parent)
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        child = await repo.child(parent_id, child_id)
        assert child is not None
        assert child.id == child_id
        for other in (parent_id, stranger, sibling_parent, new_id()):
            assert await repo.child(parent_id, other) is None
        assert await repo.child(child_id, parent_id) is None


async def test_set_status(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    async with identity_db.database.global_session() as session:
        repo = OrgRepo(session)
        suspended = await repo.set_status(org_id, "suspended")
        assert await repo.set_status(new_id(), "suspended") is None
    assert suspended is not None
    assert suspended.status == "suspended"
    async with identity_db.database.global_session() as session:
        stored = await OrgRepo(session).get(org_id)
    assert stored is not None
    assert stored.status == "suspended"


async def test_api_key_repo_add_and_prefixes(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    other_id = await new_org(identity_db)
    user_id = await new_user(identity_db)
    prefixes = sorted(f"va_test_{secrets.token_hex(2)}" for _ in range(2))
    async with identity_db.database.tenant_session(org_id) as session:
        repo = ApiKeyRepo(session, org_id)
        added = [
            await repo.add(
                name="Sub-account key",
                prefix=prefix,
                secret_hash="cd" * 32,
                pepper_version=2,
                scopes=["*"],
                environment="test",
                created_by=user_id,
            )
            for prefix in prefixes
        ]
    assert [key.org_id for key in added] == [org_id, org_id]
    assert (added[0].pepper_version, added[0].environment, added[0].created_by) == (2, "test", user_id)
    assert added[0].revoked_at is None
    assert added[0].created_at is not None
    async with identity_db.database.tenant_session(org_id) as session:
        assert await ApiKeyRepo(session, org_id).prefixes() == prefixes
    async with identity_db.database.tenant_session(other_id) as session:
        assert await ApiKeyRepo(session, other_id).prefixes() == []
