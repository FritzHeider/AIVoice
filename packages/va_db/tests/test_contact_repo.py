"""``ContactRepo.upsert_phone`` on a migrated database: insert, reuse, per-org rows, and RLS (specs/03 §6
``contacts``; specs/06 §7 Decision P05-T12c)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from tenant_rows import OrgFactory, open_database

from va_db.models import Contact
from va_db.repositories import ContactRepo
from va_db.session import Database

pytestmark = pytest.mark.integration

PHONE = "+14155550142"


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def _upsert(database: Database, org_id: uuid.UUID, phone: str = PHONE) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        return await ContactRepo(session, org_id).upsert_phone(phone)


async def _contacts(database: Database, org_id: uuid.UUID) -> list[Contact]:
    async with database.tenant_session(org_id) as session:
        return list((await session.scalars(select(Contact).where(Contact.org_id == org_id))).all())


async def test_the_first_call_creates_the_contact_and_later_calls_reuse_it(database: Database) -> None:
    org = await OrgFactory(database).create()
    first = await _upsert(database, org)
    [created] = await _contacts(database, org)
    second = await _upsert(database, org)
    [reused] = await _contacts(database, org)
    assert first == second == created.id
    assert (reused.phone_e164, reused.email, reused.name) == (PHONE, None, None)
    assert reused.updated_at >= created.updated_at


async def test_each_org_has_its_own_contact_for_the_same_phone(database: Database) -> None:
    org_a, org_b = await OrgFactory(database).create(), await OrgFactory(database).create()
    contact_a = await _upsert(database, org_a)
    contact_b = await _upsert(database, org_b)
    assert contact_a != contact_b
    assert [c.id for c in await _contacts(database, org_a)] == [contact_a]
    assert [c.id for c in await _contacts(database, org_b)] == [contact_b]


async def test_a_session_of_another_org_cannot_write_the_contact(database: Database) -> None:
    org_a, org_b = await OrgFactory(database).create(), await OrgFactory(database).create()
    with pytest.raises(Exception, match="row-level security"):
        async with database.tenant_session(org_b) as session:
            await ContactRepo(session, org_a).upsert_phone(PHONE)
    assert await _contacts(database, org_a) == []
