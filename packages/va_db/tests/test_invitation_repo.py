"""``InvitationRepo`` and the system-scope ``InvitationLookupRepo`` (specs/04 §6; specs/03 §2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from identity_db import IdentityDb, unique_email
from model_factories import make_org, make_user
from sqlalchemy import select, update

from va_common.auth import sha256_hex
from va_db.models import Invitation, Org, User
from va_db.repositories import (
    INVITATION_TTL,
    InvitationLookup,
    InvitationLookupRepo,
    InvitationRepo,
    MembershipRepo,
)
from va_db.session import Database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 29, 9, 12, 40, tzinfo=UTC)


async def org_and_inviter(database: Database, *, name: str = "Jane Park") -> tuple[Org, User]:
    """A fresh org and a user (with ``name``) who sends its invitations."""
    async with database.system_session() as session:
        org = await make_org(session, name="Acme Dental")
        inviter = await make_user(session, unique_email("inviter"))
        await session.execute(update(User).where(User.id == inviter.id).values(name=name))
    return org, inviter


async def invite(
    database: Database, org: Org, inviter: User, email: str, *, now: datetime = NOW
) -> tuple[Invitation, str]:
    async with database.tenant_session(org.id) as session:
        return await InvitationRepo(session, org.id).create(
            email=email, role="developer", invited_by=inviter.id, now=now
        )


async def lookup(database: Database, token: str) -> InvitationLookup | None:
    async with database.system_session() as session:
        return await InvitationLookupRepo(session).by_token(token)


async def test_create_stores_the_hash_and_a_seven_day_expiry(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database)
    email = unique_email("priya")
    row, token = await invite(database, org, inviter, email)
    assert timedelta(days=7) == INVITATION_TTL
    assert row.expires_at == NOW + INVITATION_TTL
    assert row.created_at == NOW
    assert (row.org_id, row.email, row.role, row.invited_by, row.accepted_at) == (
        org.id,
        email,
        "developer",
        inviter.id,
        None,
    )
    assert row.token_hash == sha256_hex(token)
    assert token not in row.token_hash
    assert len(token) == 43


async def test_recreate_replaces_the_open_invitation(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database)
    email = unique_email("priya")
    first, first_token = await invite(database, org, inviter, email)
    async with database.tenant_session(org.id) as session:
        second, second_token = await InvitationRepo(session, org.id).create(
            email=email.upper(), role="admin", invited_by=inviter.id, now=NOW + timedelta(minutes=1)
        )
    async with database.tenant_session(org.id) as session:
        rows = await InvitationRepo(session, org.id).list_open()
    assert [row.id for row in rows] == [second.id]
    assert rows[0].role == "admin"
    assert first.id != second.id
    assert await lookup(database, first_token) is None
    assert await lookup(database, second_token) is not None


async def test_lookup_joins_org_inviter_and_account(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database)
    async with database.system_session() as session:
        existing = await make_user(session, unique_email("omar"))
    new_row, new_token = await invite(database, org, inviter, unique_email("new"))
    _, existing_token = await invite(database, org, inviter, existing.email.upper())
    async with database.system_session() as session:
        repo = InvitationLookupRepo(session)
        found_new = await repo.by_token(new_token)
        found_existing = await repo.by_token(existing_token)
        assert await repo.by_token("A" * 43) is None
    assert found_new is not None
    assert found_existing is not None
    assert (found_new.id, found_new.org_id, found_new.org_name, found_new.org_status) == (
        new_row.id,
        org.id,
        "Acme Dental",
        "active",
    )
    assert (found_new.role, found_new.inviter_name, found_new.account_exists) == (
        "developer",
        "Jane Park",
        False,
    )
    assert found_new.expires_at == NOW + INVITATION_TTL
    assert found_existing.account_exists is True
    assert found_new.usable(NOW)
    assert not found_new.usable(NOW + INVITATION_TTL)


async def test_lookup_falls_back_to_the_inviter_email(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database, name="")
    _, token = await invite(database, org, inviter, unique_email("priya"))
    async with database.system_session() as session:
        found = await InvitationLookupRepo(session).by_token(token)
    assert found is not None
    assert found.inviter_name == inviter.email


async def test_consume_once_and_never_after_expiry(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database)
    row, token = await invite(database, org, inviter, unique_email("priya"))
    expired, _ = await invite(database, org, inviter, unique_email("late"))
    async with database.tenant_session(org.id) as session:
        repo = InvitationRepo(session, org.id)
        assert await repo.consume(expired.id, NOW + INVITATION_TTL) is None
        consumed = await repo.consume(row.id, NOW + timedelta(hours=1))
        assert consumed is not None
        assert consumed.accepted_at == NOW + timedelta(hours=1)
        assert await repo.consume(row.id, NOW + timedelta(hours=2)) is None
        assert await repo.get_open(row.id) is None
        assert await repo.revoke(row.id) is False
        assert [item.id for item in await repo.list_open()] == [expired.id]
    found = await lookup(database, token)
    assert found is not None
    assert not found.usable(NOW)


async def test_list_and_revoke_stay_in_the_org(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, inviter = await org_and_inviter(database)
    other_org, other_inviter = await org_and_inviter(database)
    older, _ = await invite(database, org, inviter, unique_email("a"))
    newer, _ = await invite(database, org, inviter, unique_email("b"), now=NOW + timedelta(seconds=5))
    foreign, _ = await invite(database, other_org, other_inviter, unique_email("c"))
    async with database.tenant_session(org.id) as session:
        repo = InvitationRepo(session, org.id)
        assert [row.id for row in await repo.list_open()] == [newer.id, older.id]
        assert await repo.get_open(foreign.id) is None
        assert await repo.revoke(foreign.id) is False
        assert await repo.revoke(uuid.uuid4()) is False
        assert await repo.revoke(older.id) is True
        assert [row.id for row in await repo.list_open()] == [newer.id]
    async with database.system_session() as session:
        remaining = set((await session.scalars(select(Invitation.id))).all())
    assert foreign.id in remaining
    assert older.id not in remaining


async def test_member_by_email_is_case_insensitive_and_org_scoped(identity_db: IdentityDb) -> None:
    database = identity_db.database
    org, member = await org_and_inviter(database)
    other_org, stranger = await org_and_inviter(database)
    async with database.tenant_session(org.id) as session:
        await MembershipRepo(session, org.id).add(member.id, "analyst")
    async with database.tenant_session(other_org.id) as session:
        await MembershipRepo(session, other_org.id).add(stranger.id, "owner")
    async with database.tenant_session(org.id) as session:
        repo = MembershipRepo(session, org.id)
        found = await repo.member_by_email(f"  {member.email.upper()} ")
        assert await repo.member_by_email(stranger.email) is None
    assert found is not None
    assert (found.user_id, found.role) == (member.id, "analyst")
