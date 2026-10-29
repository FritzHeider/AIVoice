"""``va_db.audit.record`` and ``AuditLogRepo`` on real Postgres (specs/04 §13; specs/28 §6.3 append-only)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from typing import Final

import pytest
from identity_db import IdentityDb
from model_factories import make_org
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from va_common.errors import AppError
from va_db.audit import REDACTED, AuditActor, record
from va_db.models import AuditLog, Org
from va_db.repositories import AuditLogFilter, AuditLogRepo

pytestmark = pytest.mark.integration

T0: Final = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


async def new_org(identity_db: IdentityDb) -> uuid.UUID:
    async with identity_db.database.system_session() as session:
        return (await make_org(session)).id


def actor(org_id: uuid.UUID) -> AuditActor:
    return AuditActor(
        org_id=org_id,
        actor_type="user",
        actor_id=uuid.UUID("0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e"),
        ip="203.0.113.24",
        user_agent="pytest",
    )


async def stored(identity_db: IdentityDb, org_id: uuid.UUID) -> list[AuditLog]:
    async with identity_db.database.system_session() as session:
        rows = await session.scalars(select(AuditLog).where(AuditLog.org_id == org_id).order_by(AuditLog.id))
        return list(rows)


async def test_record_writes_the_row_in_the_callers_transaction(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        row = await record(
            session,
            actor(org_id),
            "api_key.create",
            "api_key",
            uuid.UUID("0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e0f"),
            after={"name": "server", "secret_hash": "ab" * 32},
        )
    (saved,) = await stored(identity_db, org_id)
    assert saved.id == row.id
    assert (saved.actor_type, saved.action, saved.resource_type) == ("user", "api_key.create", "api_key")
    assert saved.resource_id == "0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e0f"
    assert (saved.ip, saved.user_agent) == (IPv4Address("203.0.113.24"), "pytest")
    assert saved.changes == {"before": None, "after": {"name": "server", "secret_hash": REDACTED}}


async def test_parent_and_platform_markers_and_unparseable_ip(identity_db: IdentityDb) -> None:
    org_id, parent_id = await new_org(identity_db), await new_org(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        await record(
            session,
            replace(actor(org_id), ip="testclient", acting_parent_org_id=parent_id, platform_admin=True),
            "org.update",
            "org",
            org_id,
            before={"name": "A"},
            after={"name": "B"},
        )
    (saved,) = await stored(identity_db, org_id)
    assert saved.ip is None
    assert saved.changes == {
        "before": {"name": "A"},
        "after": {"name": "B"},
        "acting_parent_org_id": str(parent_id),
        "platform_admin": True,
    }


async def test_rollback_of_the_enclosing_transaction_leaves_no_row(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    with pytest.raises(AppError):
        async with identity_db.database.tenant_session(org_id) as session:
            await record(session, actor(org_id), "member.remove", "membership", None)
            raise AppError("CONFLICT", "later step failed")
    assert await stored(identity_db, org_id) == []


async def test_record_writes_another_orgs_log_and_works_on_the_global_session(
    identity_db: IdentityDb,
) -> None:
    child_id, parent_id = await new_org(identity_db), await new_org(identity_db)
    async with identity_db.database.tenant_session(child_id) as session:
        await record(session, actor(parent_id), "subaccount.suspend", "org", child_id)
        assert await session.scalar(text("SELECT current_setting('app.org_id', true)")) == str(child_id)
    async with identity_db.database.global_session() as session:
        await record(session, actor(child_id), "org.update", "org", child_id)
    assert [row.action for row in await stored(identity_db, parent_id)] == ["subaccount.suspend"]
    assert [row.action for row in await stored(identity_db, child_id)] == ["org.update"]


async def test_app_role_may_not_update_or_delete_audit_rows(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        row = await record(session, actor(org_id), "org.update", "org", org_id)
    for statement in (
        update(AuditLog).where(AuditLog.id == row.id).values(action="org.delete"),
        delete(AuditLog).where(AuditLog.id == row.id),
    ):
        with pytest.raises(DBAPIError, match="permission denied for table audit_logs"):
            async with identity_db.database.tenant_session(org_id) as session:
                await session.execute(statement)
    assert [saved.action for saved in await stored(identity_db, org_id)] == ["org.update"]


async def test_deleting_the_org_still_cascades_to_its_audit_rows(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        await record(session, actor(org_id), "org.update", "org", org_id)
    async with identity_db.database.global_session() as session:
        await session.execute(delete(Org).where(Org.id == org_id))
    assert await stored(identity_db, org_id) == []


async def seed(
    identity_db: IdentityDb, org_id: uuid.UUID, specs: list[tuple[str, str, int]]
) -> list[AuditLog]:
    """Rows ``(action, resource_id, minutes after T0)`` with explicit ``created_at`` (as ``va_system``)."""
    rows = [
        AuditLog(
            org_id=org_id,
            actor_type="api_key",
            actor_id=uuid.UUID(int=index + 1),
            action=action,
            resource_type=action.split(".")[0],
            resource_id=resource_id,
            created_at=T0 + timedelta(minutes=minutes),
            updated_at=T0,
        )
        for index, (action, resource_id, minutes) in enumerate(specs)
    ]
    async with identity_db.database.system_session() as session:
        session.add_all(rows)
    return rows


async def page_actions(
    identity_db: IdentityDb, org_id: uuid.UUID, filters: AuditLogFilter, **kwargs: object
) -> tuple[list[str | None], str | None]:
    async with identity_db.database.tenant_session(org_id) as session:
        page = await AuditLogRepo(session, org_id).page(filters, **kwargs)  # type: ignore[arg-type]
    return [row.resource_id for row in page.items], page.next_cursor


async def test_page_filters_window_order_and_isolation(identity_db: IdentityDb) -> None:
    org_id, other_id = await new_org(identity_db), await new_org(identity_db)
    rows = await seed(
        identity_db,
        org_id,
        [
            ("api_key.create", "k1", 0),
            ("api_key.revoke", "k1", 10),
            ("member.invite", "i1", 20),
            ("api_key.create", "k2", 30),
        ],
    )
    await seed(identity_db, other_id, [("api_key.create", "x1", 5)])

    assert (await page_actions(identity_db, org_id, AuditLogFilter()))[0] == ["k2", "i1", "k1", "k1"]
    assert (await page_actions(identity_db, org_id, AuditLogFilter(action="api_key.create")))[0] == [
        "k2",
        "k1",
    ]
    assert (await page_actions(identity_db, org_id, AuditLogFilter(actor_id=rows[2].actor_id)))[0] == ["i1"]
    assert (await page_actions(identity_db, org_id, AuditLogFilter(resource_type="member")))[0] == ["i1"]
    assert (await page_actions(identity_db, org_id, AuditLogFilter(resource_id="k1")))[0] == ["k1", "k1"]
    window = AuditLogFilter(since=T0 + timedelta(minutes=10), until=T0 + timedelta(minutes=30))
    assert (await page_actions(identity_db, org_id, window))[0] == ["i1", "k1"]
    assert (await page_actions(identity_db, other_id, AuditLogFilter()))[0] == ["x1"]


async def test_cursor_pages_have_no_duplicates(identity_db: IdentityDb) -> None:
    org_id = await new_org(identity_db)
    await seed(identity_db, org_id, [("org.update", f"r{minute}", minute) for minute in range(5)])
    seen: list[str | None] = []
    cursor: str | None = None
    while True:
        items, cursor = await page_actions(identity_db, org_id, AuditLogFilter(), limit=2, cursor=cursor)
        seen.extend(items)
        if cursor is None:
            break
    assert seen == ["r4", "r3", "r2", "r1", "r0"]
