"""Webhook endpoint and delivery repositories (specs/19-events-and-webhooks.md §10 Decision P06-T04b).

Each test uses fresh orgs on the module database, so lists are naturally scoped; RLS keeps other orgs' rows
invisible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tenant_rows import OrgFactory

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.repositories import (
    ALREADY_QUEUED_MESSAGE,
    ENDPOINT_DELETED_ERROR,
    ENDPOINT_DELETED_MESSAGE,
    ENDPOINT_DISABLED_MESSAGE,
    NotFoundError,
    WebhookDeliveryFilter,
    WebhookDeliveryRepo,
    WebhookEndpointRepo,
)
from va_db.session import Database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tenant_db: Database) -> Database:
    return tenant_db


@pytest.fixture
def org(fresh_org: uuid.UUID) -> uuid.UUID:
    return fresh_org


async def endpoint(
    database: Database, org_id: uuid.UUID, *, enabled: bool = True, age_s: int = 0
) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        row = WebhookEndpoint(
            org_id=org_id,
            url="https://hooks.acme.test/a",
            enabled=enabled,
            created_at=NOW - timedelta(seconds=age_s),
        )
        session.add(row)
        await session.flush()
        return row.id


async def delivery(database: Database, org_id: uuid.UUID, age_s: int = 0, **values: Any) -> uuid.UUID:
    columns: dict[str, Any] = {
        "event_id": f"evt_{new_id().hex}",
        "event_type": "call.completed",
        "url": "https://hooks.acme.test/a",
        "payload": {"status": "completed"},
        "status": "dead",
        "attempts": 9,
        "last_error": "http_status",
        "last_status_code": 500,
        "last_response_ms": 40,
        "created_at": NOW - timedelta(seconds=age_s),
        **values,
    }
    async with database.tenant_session(org_id) as session:
        row = WebhookDelivery(org_id=org_id, **columns)
        session.add(row)
        await session.flush()
        return row.id


async def load(database: Database, delivery_id: uuid.UUID) -> WebhookDelivery:
    async with database.system_session() as session:
        row = await session.get(WebhookDelivery, delivery_id)
        assert row is not None
        return row


async def test_endpoints_are_listed_oldest_first_and_counted(database: Database, org: uuid.UUID) -> None:
    newer = await endpoint(database, org, age_s=1)
    older = await endpoint(database, org, age_s=5)
    other = await OrgFactory(database).create()
    await endpoint(database, other)
    async with database.tenant_session(org) as session:
        repo = WebhookEndpointRepo(session, org)
        assert [row.id for row in await repo.all()] == [older, newer]
        assert await repo.count() == 2


async def test_delete_cancels_queued_deliveries_of_that_endpoint_only(
    database: Database, org: uuid.UUID
) -> None:
    doomed, kept = await endpoint(database, org), await endpoint(database, org)
    pending = await delivery(database, org, status="pending", endpoint_id=doomed)
    delivering = await delivery(database, org, status="delivering", endpoint_id=doomed)
    succeeded = await delivery(database, org, status="succeeded", endpoint_id=doomed, last_error=None)
    untouched = await delivery(database, org, status="pending", endpoint_id=kept)
    async with database.tenant_session(org) as session:
        assert await WebhookEndpointRepo(session, org).delete(doomed) == 2
    for cancelled in (pending, delivering):
        row = await load(database, cancelled)
        assert (row.status, row.last_error, row.endpoint_id) == ("dead", ENDPOINT_DELETED_ERROR, None)
    row = await load(database, succeeded)
    assert (row.status, row.last_error, row.endpoint_id) == ("succeeded", None, None)
    assert (await load(database, untouched)).endpoint_id == kept
    async with database.tenant_session(org) as session:
        with pytest.raises(NotFoundError):
            await WebhookEndpointRepo(session, org).delete(doomed)


async def test_another_orgs_endpoint_cannot_be_deleted(database: Database, org: uuid.UUID) -> None:
    other = await OrgFactory(database).create()
    theirs = await endpoint(database, other)
    async with database.tenant_session(org) as session:
        with pytest.raises(NotFoundError):
            await WebhookEndpointRepo(session, org).delete(theirs)
    async with database.tenant_session(other) as session:
        assert await WebhookEndpointRepo(session, other).count() == 1


async def test_deliveries_page_filters_and_hides_other_orgs(database: Database, org: uuid.UUID) -> None:
    hook = await endpoint(database, org)
    call_id = new_id()
    first = await delivery(database, org, 30, call_id=call_id)
    second = await delivery(database, org, 20, status="pending", event_type="sms.received", endpoint_id=hook)
    third = await delivery(database, org, 10, status="succeeded", call_id=call_id)
    other = await OrgFactory(database).create()
    await delivery(database, other)

    async def ids(filters: WebhookDeliveryFilter, **kwargs: Any) -> list[uuid.UUID]:
        async with database.tenant_session(org) as session:
            page = await WebhookDeliveryRepo(session, org).page(filters, **kwargs)
            return [row.id for row in page.items]

    assert await ids(WebhookDeliveryFilter()) == [third, second, first]
    assert await ids(WebhookDeliveryFilter(status="dead")) == [first]
    assert await ids(WebhookDeliveryFilter(event_type="sms.received")) == [second]
    assert await ids(WebhookDeliveryFilter(call_id=call_id)) == [third, first]
    assert await ids(WebhookDeliveryFilter(endpoint_id=hook)) == [second]
    async with database.tenant_session(org) as session:
        page = await WebhookDeliveryRepo(session, org).page(WebhookDeliveryFilter(), limit=2)
    assert page.has_more
    assert await ids(WebhookDeliveryFilter(), limit=2, cursor=page.next_cursor) == [first]


@pytest.mark.parametrize("status", ["succeeded", "failed", "dead"])
async def test_replay_resets_the_attempt_series_of_the_same_row(
    database: Database, org: uuid.UUID, status: str
) -> None:
    delivery_id = await delivery(database, org, status=status, delivered_at=NOW - timedelta(hours=1))
    before = await load(database, delivery_id)
    async with database.tenant_session(org) as session:
        replayed = await WebhookDeliveryRepo(session, org).replay(delivery_id, now=NOW)
        assert (replayed.status, replayed.attempts, replayed.next_attempt_at) == ("pending", 0, NOW)
    after = await load(database, delivery_id)
    assert (after.event_id, after.url, after.payload) == (before.event_id, before.url, before.payload)
    assert (after.last_status_code, after.last_error, after.last_response_ms, after.delivered_at) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("status", ["pending", "delivering"])
async def test_queued_deliveries_cannot_be_replayed(database: Database, org: uuid.UUID, status: str) -> None:
    delivery_id = await delivery(database, org, status=status)
    async with database.tenant_session(org) as session:
        with pytest.raises(AppError) as caught:
            await WebhookDeliveryRepo(session, org).replay(delivery_id, now=NOW)
    assert (caught.value.code, caught.value.message) == ("CONFLICT", ALREADY_QUEUED_MESSAGE)


async def test_cancelled_disabled_and_foreign_deliveries_cannot_be_replayed(
    database: Database, org: uuid.UUID
) -> None:
    cancelled = await delivery(database, org, last_error=ENDPOINT_DELETED_ERROR)
    disabled = await delivery(database, org, endpoint_id=await endpoint(database, org, enabled=False))
    enabled = await delivery(database, org, endpoint_id=await endpoint(database, org))
    other = await OrgFactory(database).create()
    foreign = await delivery(database, other)
    async with database.tenant_session(org) as session:
        repo = WebhookDeliveryRepo(session, org)
        for delivery_id, message in (
            (cancelled, ENDPOINT_DELETED_MESSAGE),
            (disabled, ENDPOINT_DISABLED_MESSAGE),
        ):
            with pytest.raises(AppError) as caught:
                await repo.replay(delivery_id, now=NOW)
            assert (caught.value.code, caught.value.message) == ("CONFLICT", message)
        with pytest.raises(NotFoundError):
            await repo.replay(foreign, now=NOW)
        assert (await repo.replay(enabled, now=NOW)).status == "pending"
    assert (await load(database, foreign)).status == "dead"
