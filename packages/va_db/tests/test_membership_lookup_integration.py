"""``MembershipRepo`` and the system-scope ``MembershipLookupRepo`` (specs/03-data-model.md §2)."""

from __future__ import annotations

import uuid

import pytest
from identity_db import IdentityDb, unique_email
from model_factories import make_org, make_user
from sqlalchemy import update

from va_common.errors import AppError
from va_db.models import MemberRole, OrgStatus, User
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


async def test_via_parent_resolves_a_direct_child_through_an_owner_or_admin_membership(
    identity_db: IdentityDb,
) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        parent = await make_org(session, name="Parent", plan_id="start")
        child = await make_org(
            session, name="Austin", parent_org_id=parent.id, plan_id="scale", limits={"rate_per_sec": 30}
        )
        grandchild = await make_org(session, name="Nested", parent_org_id=child.id)
        stranger = await make_org(session, name="Other")
        stranger_child = await make_org(session, name="Other child", parent_org_id=stranger.id)
        owner = await make_user(session, unique_email())
        admin = await make_user(session, unique_email())
    async with database.tenant_session(parent.id) as session:
        owned = await MembershipRepo(session, parent.id).add(owner.id, "owner")
        await MembershipRepo(session, parent.id).add(admin.id, "admin")
    async with database.system_session() as session:
        repo = MembershipLookupRepo(session)
        record = await repo.via_parent(owner.id, child.id)
        as_admin = await repo.via_parent(admin.id, child.id)
        assert await repo.via_parent(owner.id, parent.id) is None
        assert await repo.via_parent(owner.id, grandchild.id) is None
        assert await repo.via_parent(owner.id, stranger.id) is None
        assert await repo.via_parent(owner.id, stranger_child.id) is None
        assert await repo.via_parent(owner.id, uuid.uuid4()) is None
        assert await repo.via_parent(uuid.uuid4(), child.id) is None
    assert record == MembershipRecord(
        membership_id=owned.id,
        role="owner",
        org_id=child.id,
        org_name="Austin",
        org_slug=child.slug,
        plan_id="scale",
        org_status="active",
        parent_org_id=parent.id,
        mfa_required=False,
        rate_per_sec=30,
        user_mfa_enabled=False,
        acting_parent_org_id=parent.id,
        acting_parent_status="active",
    )
    assert as_admin is not None
    assert (as_admin.role, as_admin.org_id, as_admin.acting_parent_org_id) == ("admin", child.id, parent.id)


@pytest.mark.parametrize("role", ["developer", "analyst", "billing", "viewer"])
async def test_via_parent_refuses_other_parent_roles(identity_db: IdentityDb, role: MemberRole) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        parent = await make_org(session)
        child = await make_org(session, parent_org_id=parent.id)
        user = await make_user(session, unique_email())
    async with database.tenant_session(parent.id) as session:
        await MembershipRepo(session, parent.id).add(user.id, role)
    async with database.system_session() as session:
        assert await MembershipLookupRepo(session).via_parent(user.id, child.id) is None


@pytest.mark.parametrize(
    ("parent_settings", "child_settings", "required"),
    [
        ({}, {}, False),
        ({"mfa_required": True}, {}, True),
        ({}, {"mfa_required": True}, True),
        ({"mfa_required": True}, {"mfa_required": True}, True),
    ],
)
async def test_via_parent_requires_mfa_when_the_child_or_the_parent_does(
    identity_db: IdentityDb,
    parent_settings: dict[str, object],
    child_settings: dict[str, object],
    required: bool,
) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        parent = await make_org(session, settings=parent_settings)
        child = await make_org(session, parent_org_id=parent.id, settings=child_settings)
        user = await make_user(session, unique_email())
        await session.execute(update(User).where(User.id == user.id).values(mfa_enabled=True))
    async with database.tenant_session(parent.id) as session:
        await MembershipRepo(session, parent.id).add(user.id, "admin")
    async with database.system_session() as session:
        record = await MembershipLookupRepo(session).via_parent(user.id, child.id)
    assert record is not None
    assert (record.mfa_required, record.user_mfa_enabled, record.rate_per_sec) == (required, True, None)


@pytest.mark.parametrize(
    ("parent_status", "child_status", "gate_status"),
    [
        ("active", "active", "active"),
        ("suspended", "active", "suspended"),
        ("active", "suspended", "suspended"),
        ("closed", "suspended", "closed"),
        ("suspended", "closed", "closed"),
    ],
)
async def test_via_parent_exposes_the_parent_status_and_gates_on_the_stricter(
    identity_db: IdentityDb, parent_status: OrgStatus, child_status: OrgStatus, gate_status: OrgStatus
) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        parent = await make_org(session, status=parent_status)
        child = await make_org(session, parent_org_id=parent.id, status=child_status)
        user = await make_user(session, unique_email())
    async with database.tenant_session(parent.id) as session:
        await MembershipRepo(session, parent.id).add(user.id, "owner")
    async with database.system_session() as session:
        repo = MembershipLookupRepo(session)
        record = await repo.via_parent(user.id, child.id)
        (own,) = await repo.for_user(user.id)
    assert record is not None
    assert (record.org_status, record.acting_parent_status, record.gate_status) == (
        child_status,
        parent_status,
        gate_status,
    )
    assert (own.acting_parent_status, own.gate_status) == (None, parent_status)
