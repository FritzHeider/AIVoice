"""Do-not-call lookups against Postgres with RLS (specs/23 §2.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from compliance_rows import add_dnc, new_org, random_e164
from sqlalchemy import select, text

from va_compliance.dnc import DncBlock, find_block
from va_db.models import DncEntry
from va_db.session import Database

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

NO_INHERIT = {"compliance": {"dnc_inherit_from_parent": False}}


def now() -> datetime:
    return datetime.now(UTC)


async def lookup(database: Database, org_id: uuid.UUID, e164: str) -> DncBlock | None:
    async with database.tenant_session(org_id) as session:
        return await find_block(session, org_id, e164, now=now())


async def test_own_entry_blocks(database: Database) -> None:
    org, number = await new_org(database), random_e164()
    await add_dnc(database, org, number, source="opt_out_voice")
    assert await lookup(database, org, number) == DncBlock("org", "opt_out_voice")
    assert await lookup(database, org, random_e164()) is None


async def test_platform_entry_blocks_every_org(database: Database) -> None:
    number = random_e164()
    await add_dnc(database, None, number, source="platform")
    for org in (await new_org(database), await new_org(database)):
        assert await lookup(database, org, number) == DncBlock("platform", "platform")


async def test_platform_entry_wins_the_label_over_an_own_entry(database: Database) -> None:
    org, number = await new_org(database), random_e164()
    await add_dnc(database, org, number)
    await add_dnc(database, None, number, source="platform")
    assert await lookup(database, org, number) == DncBlock("platform", "platform")


async def test_expiry(database: Database) -> None:
    org, expired, future = await new_org(database), random_e164(), random_e164()
    await add_dnc(database, org, expired, expires_at=now() - timedelta(seconds=1))
    await add_dnc(database, org, future, expires_at=now() + timedelta(days=1))
    assert await lookup(database, org, expired) is None
    assert await lookup(database, org, future) == DncBlock("org", "api")


async def test_other_orgs_entries_do_not_block(database: Database) -> None:
    owner, other, number = await new_org(database), await new_org(database), random_e164()
    await add_dnc(database, owner, number)
    assert await lookup(database, other, number) is None


async def test_parent_entry_blocks_child_by_default(database: Database) -> None:
    parent = await new_org(database)
    child, number = await new_org(database, parent=parent), random_e164()
    await add_dnc(database, parent, number, source="import")
    assert await lookup(database, child, number) == DncBlock("parent", "import")


async def test_parent_can_turn_inheritance_off(database: Database) -> None:
    parent = await new_org(database, settings=NO_INHERIT)
    child, number = await new_org(database, parent=parent), random_e164()
    await add_dnc(database, parent, number)
    assert await lookup(database, child, number) is None


async def test_inheritance_follows_the_parents_setting_not_the_childs(database: Database) -> None:
    parent = await new_org(database)
    child, number = await new_org(database, parent=parent, settings=NO_INHERIT), random_e164()
    await add_dnc(database, parent, number)
    assert await lookup(database, child, number) == DncBlock("parent", "api")


async def test_expired_parent_entry_does_not_block(database: Database) -> None:
    parent = await new_org(database)
    child, number = await new_org(database, parent=parent), random_e164()
    await add_dnc(database, parent, number, expires_at=now() - timedelta(minutes=1))
    assert await lookup(database, child, number) is None


async def test_grandparent_entries_are_ignored(database: Database) -> None:
    grandparent = await new_org(database)
    parent = await new_org(database, parent=grandparent)
    child, number = await new_org(database, parent=parent), random_e164()
    await add_dnc(database, grandparent, number)
    assert await lookup(database, parent, number) == DncBlock("parent", "api")
    assert await lookup(database, child, number) is None


async def test_children_never_block_their_parent(database: Database) -> None:
    parent = await new_org(database)
    child, number = await new_org(database, parent=parent), random_e164()
    await add_dnc(database, child, number)
    assert await lookup(database, parent, number) is None


async def test_rls_context_is_restored_after_the_parent_lookup(database: Database) -> None:
    parent = await new_org(database)
    child, other = await new_org(database, parent=parent), random_e164()
    child_number = random_e164()
    await add_dnc(database, parent, other)
    await add_dnc(database, child, child_number)
    async with database.tenant_session(child) as session:
        assert await find_block(session, child, random_e164(), now=now()) is None  # walks to the parent
        assert await session.scalar(text("SELECT current_setting('app.org_id', true)")) == str(child)
        owners = set(
            await session.scalars(
                select(DncEntry.org_id).where(DncEntry.phone_e164.in_([other, child_number]))
            )
        )
    assert owners == {child}
