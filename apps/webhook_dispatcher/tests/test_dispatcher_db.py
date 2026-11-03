"""The dispatcher against a migrated Postgres and a real receiver (specs/19-events-and-webhooks.md §8, §11).

Rows are claimed with ``FOR UPDATE SKIP LOCKED``; each test empties the outbox first (``dispatch_rig``).
Real time is used: ``call_events`` partitions exist only around the migration month.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from dispatch_helpers import DASHBOARD, Rig, StaticOrgSettings, dispatch_rig, short_timeouts
from service_helpers import json_log_stream, log_events
from sqlalchemy import select
from webhook_receiver import Receiver, Reply, signature_ok

from va_common.ids import new_id
from va_common.settings import Environment
from va_db.models import CallEvent, Org
from va_db.repositories import OutboxRepo, WebhookEndpointRepo
from va_domain.org import OrgSettings
from va_events.webhook_schedule import Timeouts
from va_webhooks.dispatcher import DispatchLimits

pytestmark = pytest.mark.integration

LONG = Timeouts(batch_s=10.0, default_s=10.0)


async def call_events(rig: Rig, call_id: uuid.UUID) -> list[CallEvent]:
    async with rig.database.system_session() as session:
        rows = await session.scalars(
            select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.seq)
        )
        return list(rows.all())


async def test_success_signs_records_and_logs_the_attempt(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        call_id = new_id()
        delivery_id = await rig.delivery(org, rig.url("/ok"), call_id=call_id)
        assert await rig.dispatcher.run_once() == 1
        row = await rig.row(delivery_id)
        events = await call_events(rig, call_id)
    (received,) = rig.receiver.requests
    assert signature_ok(received, org.secret)
    assert received.headers["x-va-event-id"] == received.headers["x-event-id"] == row.event_id
    assert received.headers["x-va-event-type"] == "call.completed"
    assert received.headers["user-agent"] == "VoiceAgents-Webhooks/1.0"
    assert received.headers["content-type"] == "application/json"
    assert received.host == "hooks.test"
    assert orjson.loads(received.body) == row.payload
    assert (row.status, row.attempts, row.last_status_code, row.last_error) == ("succeeded", 1, 200, None)
    assert row.delivered_at is not None
    assert row.last_response_ms is not None
    (event,) = events
    assert (event.category, event.level, event.seq) == ("webhook", "info", 1)
    assert event.message.startswith("Webhook delivered to hooks.test: 200 (")
    assert event.data["attempt"] == 1
    assert event.data["delivery_id"] == str(delivery_id)
    assert rig.deliveries("succeeded") == 1
    assert rig.metrics.registry.get_sample_value("va_webhook_delivery_ms_count") == 1


async def test_a_500_schedules_a_jittered_retry_and_warns_in_the_call_log(dispatcher_db_url: str) -> None:
    receiver = Receiver(default=Reply(500))
    async with dispatch_rig(dispatcher_db_url, receiver=receiver) as rig:
        org = await rig.org()
        call_id = new_id()
        delivery_id = await rig.delivery(org, rig.url("/fail"), call_id=call_id)
        before = datetime.now(UTC)
        await rig.dispatcher.run_once()
        row = await rig.row(delivery_id)
        (event,) = await call_events(rig, call_id)
        assert await rig.dispatcher.run_once() == 0
    assert (row.status, row.attempts, row.last_status_code, row.last_error) == (
        "pending",
        1,
        500,
        "http_status",
    )
    assert row.next_attempt_at >= before + timedelta(seconds=9)
    assert row.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=11)
    assert event.level == "warning"
    assert "failed: 500 (attempt 1), retrying in" in event.message
    assert rig.deliveries("failed") == 1


async def test_the_ninth_failed_attempt_is_dead(dispatcher_db_url: str) -> None:
    receiver = Receiver(default=Reply(503))
    async with dispatch_rig(dispatcher_db_url, receiver=receiver) as rig:
        org = await rig.org()
        call_id = new_id()
        delivery_id = await rig.delivery(org, rig.url("/fail"), call_id=call_id, attempts=8)
        await rig.dispatcher.run_once()
        row = await rig.row(delivery_id)
        (event,) = await call_events(rig, call_id)
    assert (row.status, row.attempts, row.last_error) == ("dead", 9, "http_status")
    assert event.level == "error"
    assert event.message == "Webhook to hooks.test failed: 503, giving up"
    assert rig.deliveries("dead") == 1


async def test_timeouts_and_redirects_are_failures(dispatcher_db_url: str) -> None:
    receiver = Receiver()
    receiver.script("/slow", Reply(hold=True))
    receiver.script("/moved", Reply(302, headers={"Location": "/elsewhere"}))
    async with dispatch_rig(dispatcher_db_url, receiver=receiver, timeouts=short_timeouts(0.5)) as rig:
        org = await rig.org()
        slow = await rig.delivery(org, rig.url("/slow"))
        moved = await rig.delivery(org, rig.url("/moved"))
        await rig.dispatcher.run_once()
        slow_row, moved_row = await rig.row(slow), await rig.row(moved)
    assert (slow_row.status, slow_row.last_error, slow_row.last_status_code) == ("pending", "timeout", None)
    assert slow_row.last_response_ms is not None
    assert slow_row.last_response_ms >= 400
    assert (moved_row.status, moved_row.last_error, moved_row.last_status_code) == (
        "pending",
        "redirect",
        302,
    )
    assert "/elsewhere" not in rig.receiver.paths()


async def test_connection_failures_retry(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        refused = await rig.delivery(org, "http://hooks.test:1/closed")
        await rig.dispatcher.run_once()
        row = await rig.row(refused)
    assert (row.status, row.last_error, row.last_status_code) == ("pending", "connection", None)


async def test_410_disables_the_endpoint_and_emails_owners_and_admins_once(dispatcher_db_url: str) -> None:
    receiver = Receiver(default=Reply(410))
    async with dispatch_rig(dispatcher_db_url, receiver=receiver) as rig:
        org = await rig.org(admins=2)
        url = rig.url("/gone")
        endpoint = await rig.endpoint(org, url)
        first = await rig.delivery(org, url, endpoint_id=endpoint)
        await rig.dispatcher.run_once()
        assert not await rig.endpoint_enabled(endpoint)
        later = await rig.delivery(org, url, endpoint_id=endpoint)
        per_call = await rig.delivery(org, rig.url("/gone-too"))
        await rig.dispatcher.run_once()
        rows = [await rig.row(delivery_id) for delivery_id in (first, later, per_call)]
    assert [(row.status, row.last_error, row.last_status_code) for row in rows] == [
        ("dead", "gone", 410),
        ("dead", "endpoint_disabled", None),
        ("dead", "gone", 410),
    ]
    assert rig.receiver.paths() == ["/gone", "/gone-too"]
    assert len(rig.sender.sent) == 3
    assert {message.template for message in rig.sender.sent} == {"webhook_endpoint_disabled"}
    assert len({message.to for message in rig.sender.sent}) == 3
    assert f"{DASHBOARD}/settings/webhooks" in rig.sender.sent[0].text


async def test_private_addresses_are_refused_without_a_request(dispatcher_db_url: str) -> None:
    answers = {"hooks.test": ["127.0.0.1"], "internal.test": ["10.0.0.5"]}
    async with dispatch_rig(dispatcher_db_url, allow_private=False, answers=answers) as rig:
        org = await rig.org()
        private = await rig.delivery(org, rig.url("/x", host="internal.test"))
        loopback = await rig.delivery(org, rig.url("/x"))
        await rig.dispatcher.run_once()
        rows = [await rig.row(private), await rig.row(loopback)]
    assert [(row.status, row.last_error) for row in rows] == [
        ("pending", "blocked_destination:address"),
        ("pending", "blocked_destination:address"),
    ]
    assert rig.receiver.requests == []


@pytest.mark.parametrize(
    ("insecure", "status", "error"),
    [(False, "dead", "blocked_destination:scheme"), (True, "succeeded", None)],
)
async def test_http_outside_dev_needs_allow_insecure_webhooks(
    dispatcher_db_url: str, insecure: bool, status: str, error: str | None
) -> None:
    settings = StaticOrgSettings(OrgSettings(allow_insecure_webhooks=insecure))
    async with dispatch_rig(dispatcher_db_url, va_env=Environment.STAGING, org_settings=settings) as rig:
        org = await rig.org()
        delivery_id = await rig.delivery(org, rig.url("/plain"))
        await rig.dispatcher.run_once()
        row = await rig.row(delivery_id)
    assert (row.status, row.last_error) == (status, error)


async def test_unreadable_secret_retries_without_a_request(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        async with rig.database.system_session() as session:
            stored = await session.get(Org, org.id)
            assert stored is not None
            stored.webhook_signing_secret_enc = b"\x01" + bytes(40)
        delivery_id = await rig.delivery(org, rig.url("/x"))
        await rig.dispatcher.run_once()
        row = await rig.row(delivery_id)
    assert (row.status, row.last_error) == ("pending", "secret_unavailable")
    assert rig.receiver.requests == []


async def test_deliveries_without_a_call_write_no_call_event(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        await rig.delivery(org, rig.url("/x"), event_type="billing.low_balance")
        await rig.dispatcher.run_once()
        async with rig.database.system_session() as session:
            count = len((await session.scalars(select(CallEvent.id).where(CallEvent.org_id == org.id))).all())
    assert count == 0
    assert len(rig.receiver.requests) == 1


async def test_two_pods_deliver_every_row_exactly_once(dispatcher_db_url: str) -> None:
    limits, key = DispatchLimits(claim_batch=40), secrets.token_hex(32)
    async with (
        dispatch_rig(dispatcher_db_url, limits=limits, master_key=key, timeouts=LONG) as rig,
        dispatch_rig(
            dispatcher_db_url, limits=limits, receiver=rig.receiver, master_key=key, timeouts=LONG
        ) as other,
    ):
        org = await rig.org()
        ids = [await rig.delivery(org, rig.url(f"/n/{index}")) for index in range(300)]

        async def drain(pod: Rig) -> None:
            claimed = 1
            while claimed:
                claimed = await pod.dispatcher.run_once()

        await asyncio.gather(drain(rig), drain(other))
        rows = [await rig.row(delivery_id) for delivery_id in ids]
    event_ids = [request.headers["x-va-event-id"] for request in rig.receiver.requests]
    assert len(event_ids) == 300
    assert len(set(event_ids)) == 300
    assert {row.status for row in rows} == {"succeeded"}
    assert rig.deliveries("succeeded") > 0
    assert other.deliveries("succeeded") > 0


async def test_per_host_concurrency_is_capped_while_other_hosts_progress(dispatcher_db_url: str) -> None:
    receiver = Receiver()
    receiver.script("/held", *[Reply(hold=True)] * 6)
    limits = DispatchLimits(per_host=3)
    async with dispatch_rig(dispatcher_db_url, limits=limits, receiver=receiver, timeouts=LONG) as rig:
        org = await rig.org()
        for _ in range(6):
            await rig.delivery(org, rig.url("/held", host="a.hooks.test"))
        for _ in range(4):
            await rig.delivery(org, rig.url("/free", host="b.hooks.test"))
        run = asyncio.ensure_future(rig.dispatcher.run_once())
        await receiver.wait_for(
            lambda: receiver.completed["b.hooks.test"] == 4 and receiver.in_flight["a.hooks.test"] == 3
        )
        assert receiver.max_in_flight["a.hooks.test"] == 3
        receiver.release()
        await run
    assert receiver.max_in_flight["a.hooks.test"] == 3
    assert receiver.completed["a.hooks.test"] == 6
    assert rig.dispatcher.hosts.tracked() == 0


async def test_an_unfinished_claim_is_redelivered_after_the_lease(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        delivery_id = await rig.delivery(org, rig.url("/x"))
        async with rig.database.system_session() as session:
            (claimed,) = await OutboxRepo(session).claim_due(datetime.now(UTC))
        assert claimed.id == delivery_id
        assert await rig.dispatcher.run_once() == 0
        async with rig.database.system_session() as session:
            recovered = await OutboxRepo(session).claim_due(
                datetime.now(UTC) + timedelta(minutes=5, seconds=1)
            )
        assert [row.id for row in recovered] == [delivery_id]


async def test_run_drains_in_flight_deliveries_when_stopped(dispatcher_db_url: str) -> None:
    receiver = Receiver()
    receiver.script("/held", Reply(hold=True))
    async with dispatch_rig(dispatcher_db_url, receiver=receiver, timeouts=LONG) as rig:
        org = await rig.org()
        delivery_id = await rig.delivery(org, rig.url("/held"))
        stop = asyncio.Event()
        run = asyncio.ensure_future(rig.dispatcher.run(stop))
        await receiver.wait_for(lambda: receiver.in_flight["hooks.test"] == 1)
        stop.set()
        await asyncio.sleep(0)
        assert not run.done()
        receiver.release()
        async with asyncio.timeout(10):
            await run
        row = await rig.row(delivery_id)
        backlog = rig.metrics.registry.get_sample_value("va_webhook_backlog")
    assert row.status == "succeeded"
    assert backlog is not None


async def test_backlog_gauges(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        await rig.delivery(org, rig.url("/x"), next_attempt_at=datetime.now(UTC) - timedelta(seconds=90))
        await rig.delivery(org, rig.url("/y"))
        await rig.dispatcher.refresh_backlog()
    registry = rig.metrics.registry
    assert registry.get_sample_value("va_webhook_backlog") == 2
    oldest = registry.get_sample_value("va_webhook_oldest_pending_seconds")
    assert oldest is not None
    assert 89 <= oldest <= 120


async def test_a_delivery_cancelled_mid_attempt_keeps_its_cancelled_state(dispatcher_db_url: str) -> None:
    """Deleting the endpoint while the attempt is in flight wins: no outcome, no call log row, no metric."""
    receiver = Receiver()
    receiver.script("/held", Reply(hold=True))
    stream = json_log_stream()
    async with dispatch_rig(dispatcher_db_url, receiver=receiver, timeouts=LONG) as rig:
        org = await rig.org()
        call_id = new_id()
        endpoint_id = await rig.endpoint(org, rig.url("/held"))
        delivery_id = await rig.delivery(org, rig.url("/held"), call_id=call_id, endpoint_id=endpoint_id)
        run = asyncio.ensure_future(rig.dispatcher.run_once())
        await receiver.wait_for(lambda: receiver.in_flight["hooks.test"] == 1)
        async with rig.database.tenant_session(org.id) as session:
            assert await WebhookEndpointRepo(session, org.id).delete(endpoint_id) == 1
        receiver.release()
        async with asyncio.timeout(10):
            await run
        row = await rig.row(delivery_id)
        events = await call_events(rig, call_id)
    assert (row.status, row.attempts, row.last_error, row.endpoint_id) == (
        "dead",
        0,
        "endpoint_deleted",
        None,
    )
    assert events == []
    assert rig.deliveries("succeeded") == 0
    (cancelled,) = [e for e in log_events(stream) if e["event"] == "webhook.delivery_cancelled"]
    assert cancelled["delivery_id"] == str(delivery_id)
