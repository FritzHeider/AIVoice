"""``DbWebhookRouter`` over a migrated Postgres: delivery rows, tenant isolation, the transactional outbox."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from call_rows import seed_call
from event_helpers import event
from fakeredis import FakeAsyncRedis
from model_factories import make_org
from sqlalchemy import select
from tenant_rows import OrgFactory

from va_common.ids import new_id
from va_db.models import Batch, Conversation, WebhookDelivery, WebhookEndpoint, Widget
from va_db.session import Database
from va_events import DbWebhookRouter, EventPublisher, EventType, live_body

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

CALL_OBJECT: dict[str, Any] = {
    "call_id": "x",
    "transcripts": [{"id": 1, "user": "user", "text": "My date of birth is"}],
    "concatenated_transcript": "user: My date of birth is\n",
    "pathway_logs": [],
}


class RolledBackError(Exception):
    """Raised inside a transaction to roll it back."""


def _url(name: str) -> str:
    return f"https://{name}-{secrets.token_hex(4)}.hooks.test/in"


async def _endpoint(database: Database, org_id: uuid.UUID, url: str, **values: Any) -> uuid.UUID:
    endpoint = WebhookEndpoint(id=new_id(), org_id=org_id, url=url, **values)
    async with database.tenant_session(org_id) as session:
        session.add(endpoint)
    return endpoint.id


async def _deliveries(
    database: Database, org_id: uuid.UUID, event_id: str | None = None
) -> list[WebhookDelivery]:
    statement = select(WebhookDelivery).where(WebhookDelivery.org_id == org_id)
    if event_id is not None:
        statement = statement.where(WebhookDelivery.event_id == event_id)
    async with database.tenant_session(org_id) as session:
        result = await session.execute(statement.order_by(WebhookDelivery.url))
        return list(result.scalars())


async def test_call_completed_reaches_the_call_webhook_and_matching_endpoints(database: Database) -> None:
    call_url, endpoint_url = _url("call"), _url("org")
    call = await seed_call(database, status="completed", config={"webhook": call_url}, metadata_={"crm": "7"})
    endpoint_id = await _endpoint(database, call.org_id, endpoint_url, event_types=["call.*"])
    await _endpoint(database, call.org_id, _url("disabled"), enabled=False)
    await _endpoint(database, call.org_id, _url("sms"), event_types=["sms.*"])
    other_org = await OrgFactory(database).create()
    await _endpoint(database, other_org, _url("foreign"))
    completed = event(EventType.CALL_COMPLETED, org_id=call.org_id, call_id=call.call_id, data=CALL_OBJECT)

    await DbWebhookRouter(database).route(completed, session=None)

    rows = {row.url: row for row in await _deliveries(database, call.org_id)}
    assert set(rows) == {call_url, endpoint_url}
    bland, envelope = rows[call_url], rows[endpoint_url]
    assert (bland.format, bland.payload, bland.endpoint_id) == ("bland", CALL_OBJECT, None)
    assert (envelope.format, envelope.endpoint_id) == ("envelope", endpoint_id)
    assert envelope.payload["id"] == completed.id
    assert envelope.payload["data"] == CALL_OBJECT
    for row in rows.values():
        assert (row.event_id, row.event_type, row.status, row.attempts) == (
            completed.id,
            "call.completed",
            "pending",
            0,
        )
        assert (row.call_id, row.batch_id, row.conversation_id) == (call.call_id, None, None)
    assert await _deliveries(database, other_org) == []


async def test_live_events_honor_webhook_events_and_carry_call_metadata(database: Database) -> None:
    call_url = _url("call")
    call = await seed_call(
        database, config={"webhook": call_url, "webhook_events": ["latency"]}, metadata_={"crm": "7"}
    )
    latency = event(EventType.CALL_LATENCY, org_id=call.org_id, call_id=call.call_id, message="E2E: 700ms")
    latency = latency.model_copy(update={"seq": 4})
    turn = event(EventType.CALL_TURN, org_id=call.org_id, call_id=call.call_id, data={"text": "hi"})

    await DbWebhookRouter(database).route_many([latency, turn])

    (row,) = await _deliveries(database, call.org_id)
    assert (row.event_id, row.format) == (latency.id, "bland")
    assert row.payload == live_body(latency, {"crm": "7"})


async def test_routing_the_same_event_twice_keeps_one_row_per_url(database: Database) -> None:
    call = await seed_call(database, config={"webhook": _url("call")})
    completed = event(EventType.CALL_COMPLETED, org_id=call.org_id, call_id=call.call_id, data=CALL_OBJECT)
    router = DbWebhookRouter(database)

    await router.route(completed)
    await router.route(completed)

    assert len(await _deliveries(database, call.org_id, completed.id)) == 1


async def test_a_rolled_back_state_change_leaves_no_delivery_row(database: Database) -> None:
    call = await seed_call(database, config={"webhook": _url("call")})
    publisher = EventPublisher(FakeAsyncRedis(), call_events=None, router=DbWebhookRouter(database))
    rolled_back = event(EventType.CALL_COMPLETED, org_id=call.org_id, call_id=call.call_id, data=CALL_OBJECT)
    committed = event(EventType.CALL_COMPLETED, org_id=call.org_id, call_id=call.call_id, data=CALL_OBJECT)

    with pytest.raises(RolledBackError):
        async with database.tenant_session(call.org_id) as session:
            await publisher.publish(rolled_back, session=session)
            assert len((await session.execute(select(WebhookDelivery.id))).all()) == 1
            raise RolledBackError
    async with database.tenant_session(call.org_id) as session:
        await publisher.publish(committed, session=session)

    rows = await _deliveries(database, call.org_id)
    assert [row.event_id for row in rows] == [committed.id]


async def test_batch_conversation_and_widget_webhooks(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    batch_url, conversation_url, widget_url = _url("batch"), _url("conv"), _url("widget")
    batch = Batch(id=new_id(), org_id=org_id, status_webhook=batch_url, calls_total=3, calls_failed=1)
    widget = Widget(id=new_id(), org_id=org_id, name="Site", webhook_url=widget_url, public_key=new_id().hex)
    async with database.tenant_session(org_id) as session:
        session.add_all([batch, widget])
        await session.flush()
        conversation = Conversation(
            id=new_id(), org_id=org_id, channel="widget", runner_type="objective", widget_id=widget.id
        )
        conversation.webhook = conversation_url
        session.add(conversation)
    phase = event(EventType.BATCH_FAILED, org_id=org_id, batch_id=batch.id, data={"code": "no_credits"})
    message = event(
        EventType.CONVERSATION_MESSAGE, org_id=org_id, conversation_id=conversation.id, widget_id=widget.id
    )

    await DbWebhookRouter(database).route_many([phase, message])

    by_event = {(row.event_type, row.url): row for row in await _deliveries(database, org_id)}
    assert set(by_event) == {
        ("batch.failed", batch_url),
        ("conversation.message", conversation_url),
        ("conversation.message", widget_url),
    }
    batch_row = by_event[("batch.failed", batch_url)]
    assert batch_row.format == "bland"
    assert batch_row.batch_id == batch.id
    assert {key: batch_row.payload[key] for key in ("status", "calls_total", "calls_failed", "code")} == {
        "status": "failed",
        "calls_total": 3,
        "calls_failed": 1,
        "code": "no_credits",
    }
    conversation_row = by_event[("conversation.message", conversation_url)]
    assert conversation_row.format == "envelope"
    assert conversation_row.conversation_id == conversation.id


async def test_hipaa_org_strips_transcripts_from_the_call_webhook(database: Database) -> None:
    async with database.system_session() as session:
        org = await make_org(session, settings={"hipaa_mode": True, "baa_signed_at": "2026-10-01T00:00:00Z"})
    phi_url = _url("phi")
    await _endpoint(database, org.id, phi_url, phi_allowed=True)
    call_url = _url("call")
    call = await seed_call(database, org_id=org.id, config={"webhook": call_url})
    completed = event(EventType.CALL_COMPLETED, org_id=org.id, call_id=call.call_id, data=CALL_OBJECT)

    await DbWebhookRouter(database).route(completed)

    rows = {row.url: row for row in await _deliveries(database, org.id)}
    assert rows[call_url].payload["transcripts"] == []
    assert rows[call_url].payload["concatenated_transcript"] == ""
    assert rows[phi_url].payload["data"] == CALL_OBJECT
