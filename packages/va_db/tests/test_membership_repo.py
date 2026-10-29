"""``MembershipRepo`` member management and the last-owner rule (specs/04-auth-tenancy-rbac.md §6)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from identity_db import IdentityDb, unique_email
from model_factories import make_org, make_user
from sqlalchemy import func, select, text, update
from tenant_rows import open_database

from va_common.errors import AppError
from va_db.models import MemberRole, Membership, Org, User
from va_db.repositories import LAST_OWNER_MESSAGE, NOT_OWNER_MESSAGE, MemberRecord, MembershipRepo
from va_db.session import Database

pytestmark = pytest.mark.integration

LOGIN = datetime(2026, 10, 29, 9, 12, 40, tzinfo=UTC)
WAITING_LOCKS = text("SELECT count(*) FROM pg_locks WHERE NOT granted")


async def org_with(database: Database, *roles: MemberRole) -> tuple[Org, list[User]]:
    """A fresh org with one new user per role, added in order."""
    async with database.system_session() as session:
        org = await make_org(session)
        users = [await make_user(session, unique_email()) for _ in roles]
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        for user, role in zip(users, roles, strict=True):
            await repo.add(user.id, role)
    return org, users


async def roles_of(database: Database, org_id: uuid.UUID) -> dict[uuid.UUID, MemberRole]:
    async with database.tenant_session(org_id) as session:
        return {
            record.user_id: record.role for record in await MembershipRepo(session, org_id).list_members()
        }


async def test_list_members_in_join_order_with_user_fields(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (owner, viewer) = await org_with(database, "owner", "viewer")
    async with database.system_session() as session:
        await session.execute(
            update(User)
            .where(User.id == owner.id)
            .values(name="Jane Park", mfa_enabled=True, last_login_at=LOGIN)
        )
        joined = {
            membership.user_id: membership.created_at
            for membership in (
                await session.scalars(select(Membership).where(Membership.org_id == org.id))
            ).all()
        }
    async with database.tenant_session(org.id) as session:
        members = await MembershipRepo(session, org.id).list_members()
    assert members == [
        MemberRecord(owner.id, owner.email, "Jane Park", "owner", True, LOGIN, joined[owner.id]),
        MemberRecord(viewer.id, viewer.email, "", "viewer", False, None, joined[viewer.id]),
    ]


async def test_member_is_none_outside_the_org(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (owner,) = await org_with(database, "owner")
    other_org, (stranger,) = await org_with(database, "owner")
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        found = await repo.member(owner.id)
        assert await repo.member(stranger.id) is None
        assert await repo.member(uuid.uuid4()) is None
        assert await repo.change_role(stranger.id, "viewer") is None
        assert await repo.remove_member(stranger.id) is False
        assert await repo.transfer_ownership(owner.id, stranger.id) is None
    assert found is not None
    assert found.role == "owner"
    assert await roles_of(database, other_org.id) == {stranger.id: "owner"}


async def test_change_role_keeps_an_owner(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (first, second, developer) = await org_with(database, "owner", "owner", "developer")
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        promoted = await repo.change_role(developer.id, "analyst")
        unchanged = await repo.change_role(developer.id, "analyst")
        demoted = await repo.change_role(first.id, "admin")
        assert await repo.owner_count() == 1
        with pytest.raises(AppError) as info:
            await repo.change_role(second.id, "viewer")
        assert (info.value.code, info.value.message) == ("CONFLICT", LAST_OWNER_MESSAGE)
    assert promoted is not None
    assert promoted.role == "analyst"
    assert unchanged == promoted
    assert demoted is not None
    assert demoted.role == "admin"
    assert await roles_of(database, org.id) == {
        first.id: "admin",
        second.id: "owner",
        developer.id: "analyst",
    }


async def test_owner_keeps_the_role_when_set_to_owner(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (owner,) = await org_with(database, "owner")
    async with database.tenant_session(org.id) as session:
        record = await MembershipRepo(session, org.id).change_role(owner.id, "owner")
    assert record is not None
    assert record.role == "owner"


async def test_remove_member_keeps_an_owner(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (first, second, viewer) = await org_with(database, "owner", "owner", "viewer")
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        assert await repo.remove_member(viewer.id) is True
        assert await repo.remove_member(first.id) is True
        with pytest.raises(AppError) as info:
            await repo.remove_member(second.id)
        assert (info.value.code, info.value.message) == ("CONFLICT", LAST_OWNER_MESSAGE)
        assert await repo.remove(viewer.id) is False
        assert await repo.set_role(viewer.id, "admin") is False
    assert await roles_of(database, org.id) == {second.id: "owner"}


async def test_transfer_ownership_swaps_roles(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, (owner, co_owner, developer) = await org_with(database, "owner", "owner", "developer")
    async with database.tenant_session(org.id) as session:
        result = await MembershipRepo(session, org.id).transfer_ownership(owner.id, developer.id)
    assert result is not None
    previous, new_owner = result
    assert (previous.user_id, previous.role) == (owner.id, "admin")
    assert (new_owner.user_id, new_owner.role) == (developer.id, "owner")
    assert await roles_of(database, org.id) == {
        owner.id: "admin",
        co_owner.id: "owner",
        developer.id: "owner",
    }
    async with database.tenant_session(org.id) as session:
        with pytest.raises(AppError) as info:
            await MembershipRepo(session, org.id).transfer_ownership(owner.id, co_owner.id)
    assert (info.value.code, info.value.message) == ("FORBIDDEN", NOT_OWNER_MESSAGE)


async def demote(database: Database, org_id: uuid.UUID, user_id: uuid.UUID) -> MemberRecord | None:
    async with database.tenant_session(org_id) as session:
        return await MembershipRepo(session, org_id).change_role(user_id, "admin")


async def wait_for_blocked_lock(observer: Database) -> None:
    """Return once some backend waits for a lock; bounded by round trips, never by sleeping."""
    for _ in range(2000):
        async with observer.system_session() as session:
            if (await session.execute(WAITING_LOCKS)).scalar_one() > 0:
                return
    pytest.fail("the second demotion never waited for the org-row lock")


async def test_org_row_lock_serialises_concurrent_demotions(
    identity_db: IdentityDb, migrated_url: str
) -> None:
    database = identity_db.database
    org, (first, second) = await org_with(database, "owner", "owner")
    locked, release = asyncio.Event(), asyncio.Event()

    async def hold_first_demotion() -> MemberRecord | None:
        async with database.tenant_session(org.id) as session:
            record = await MembershipRepo(session, org.id).change_role(first.id, "admin")
            locked.set()
            await release.wait()
        return record

    async with open_database(migrated_url) as other, open_database(migrated_url) as observer:
        holder = asyncio.create_task(hold_first_demotion())
        await locked.wait()
        waiter = asyncio.create_task(demote(other, org.id, second.id))
        await wait_for_blocked_lock(observer)
        release.set()
        held = await holder
        with pytest.raises(AppError) as info:
            await waiter
    assert held is not None
    assert held.role == "admin"
    assert (info.value.code, info.value.message) == ("CONFLICT", LAST_OWNER_MESSAGE)
    assert await roles_of(database, org.id) == {first.id: "admin", second.id: "owner"}


async def test_simultaneous_demotions_leave_exactly_one_owner(
    identity_db: IdentityDb, migrated_url: str
) -> None:
    database = identity_db.database
    org, (first, second) = await org_with(database, "owner", "owner")
    async with open_database(migrated_url) as other:
        results = await asyncio.gather(
            demote(database, org.id, first.id), demote(other, org.id, second.id), return_exceptions=True
        )
    conflicts = [result for result in results if isinstance(result, AppError)]
    assert len(conflicts) == 1
    assert conflicts[0].message == LAST_OWNER_MESSAGE
    async with database.system_session() as session:
        owners = await session.scalar(
            select(func.count()).where(Membership.org_id == org.id, Membership.role == "owner")
        )
    assert owners == 1
