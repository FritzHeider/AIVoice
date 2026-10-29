"""``MembershipRepo`` and the system-scope ``MembershipLookupRepo`` (specs/03-data-model.md §2)."""

from __future__ import annotations

import uuid

import pytest
from identity_db import IdentityDb, unique_email
from model_factories import make_org, make_user
from sqlalchemy import update

from va_common.errors import AppError
from va_db.models import User
from va_db.repositories import MembershipLookupRepo, MembershipRecord, MembershipRepo

pytestmark = pytest.mark.integration


async def test_for_user_lists_memberships_across_orgs_in_order(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        parent = await make_org(session, name="Parent", plan_id="build")
        child = await make_org(session, name="Child", parent_org_id=parent.id, status="suspended")
        user = await make_user(session, unique_email())
        other = await make_user(session, unique_email())
    async with database.tenant_session(parent.id) as session:
        first = await MembershipRepo(session, parent.id).add(user.id, "owner")
        await MembershipRepo(session, parent.id).add(other.id, "viewer")
    async with database.tenant_session(child.id) as session:
        second = await MembershipRepo(session, child.id).add(user.id, "developer")
    async with database.system_session() as session:
        records = await MembershipLookupRepo(session).for_user(user.id)
        assert await MembershipLookupRepo(session).for_user(uuid.uuid4()) == []
    assert records == [
        MembershipRecord(
            membership_id=first.id,
            role="owner",
            org_id=parent.id,
            org_name="Parent",
            org_slug=parent.slug,
            plan_id="build",
            org_status="active",
            parent_org_id=None,
        ),
        MembershipRecord(
            membership_id=second.id,
            role="developer",
            org_id=child.id,
            org_name="Child",
            org_slug=child.slug,
            plan_id="start",
            org_status="suspended",
            parent_org_id=parent.id,
        ),
    ]


async def test_membership_repo_add_duplicate_conflict_and_get(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org, other_org = await make_org(session), await make_org(session)
        user = await make_user(session, unique_email())
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        added = await repo.add(user.id, "admin")
        with pytest.raises(AppError) as info:
            await repo.add(user.id, "viewer")
        assert (info.value.code, info.value.message) == ("CONFLICT", "User is already a member of this org.")
        found = await repo.get_for_user(user.id)
    assert found is not None
    assert (found.id, found.role) == (added.id, "admin")
    async with database.tenant_session(other_org.id) as session:
        assert await MembershipRepo(session, other_org.id).get_for_user(user.id) is None
        assert await MembershipRepo(session, org.id).get_for_user(user.id) is None


@pytest.mark.parametrize(
    ("settings", "limits", "mfa_required", "rate_per_sec"),
    [
        ({"mfa_required": True}, {"rate_per_sec": 40}, True, 40),
        ({"mfa_required": "true"}, {"rate_per_sec": 0}, False, None),
        ({}, {"rate_per_sec": True}, False, None),
    ],
)
async def test_for_user_in_org_reads_mfa_and_rate_override(
    identity_db: IdentityDb,
    settings: dict[str, object],
    limits: dict[str, object],
    mfa_required: bool,
    rate_per_sec: int | None,
) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org = await make_org(session, settings=settings, limits=limits)
        other_org = await make_org(session)
        user = await make_user(session, unique_email())
    async with database.tenant_session(org.id) as session:
        added = await MembershipRepo(session, org.id).add(user.id, "analyst")
    async with database.system_session() as session:
        repo = MembershipLookupRepo(session)
        record = await repo.for_user_in_org(user.id, org.id)
        assert await repo.for_user_in_org(user.id, other_org.id) is None
        assert await repo.for_user_in_org(uuid.uuid4(), org.id) is None
        listed = await repo.for_user(user.id)
    assert record is not None
    assert (record.membership_id, record.role, record.org_id) == (added.id, "analyst", org.id)
    assert (record.mfa_required, record.rate_per_sec) == (mfa_required, rate_per_sec)
    assert listed == [record]


async def test_records_carry_the_member_mfa_flag(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org = await make_org(session)
        enrolled = await make_user(session, unique_email())
        plain = await make_user(session, unique_email())
        await session.execute(update(User).where(User.id == enrolled.id).values(mfa_enabled=True))
    async with database.tenant_session(org.id) as session:
        await MembershipRepo(session, org.id).add(enrolled.id, "admin")
        await MembershipRepo(session, org.id).add(plain.id, "viewer")
    async with database.system_session() as session:
        repo = MembershipLookupRepo(session)
        enrolled_record = await repo.for_user_in_org(enrolled.id, org.id)
        (listed,) = await repo.for_user(enrolled.id)
        (plain_record,) = await repo.for_user(plain.id)
    assert enrolled_record is not None
    assert enrolled_record.user_mfa_enabled is True
    assert listed.user_mfa_enabled is True
    assert plain_record.user_mfa_enabled is False
