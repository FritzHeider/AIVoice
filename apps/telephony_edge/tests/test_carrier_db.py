"""Carrier events applied end to end through the processor: Postgres calls/legs/call_events, fakeredis carrier
state, carrier channel and event bus, and a recording workflow signaller (specs/06-telephony-telnyx.md §5,
§5.1; §5 Decision P05-T02).

DB tests use the wall clock: ``call_events`` partitions exist only around the migration month.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import pytest
from call_rows import SeededCall, load_call, load_leg, seed_call, seed_leg
from edge_helpers import RecordingSignaller, event_row, make_processor, running, stored_event
from fakeredis import FakeAsyncRedis, FakeServer
from sqlalchemy import select
from structlog.testing import capture_logs

from va_common.metrics import Service, build_metrics
from va_common.time import SystemClock
from va_db.models import CallEvent, TelnyxEvent, WebhookDelivery
from va_db.session import Database
from va_domain.carrier import CarrierEvent, carrier_channel, carrier_state_key, owner_key
from va_edge.carrier import CarrierEventHandler
from va_edge.processor import StoredEvent
from va_events.call_events import DbCallEventWriter, DirectCallEventWriter
from va_events.keys import stream_key
from va_events.model import decode
from va_events.publisher import EventPublisher
from va_events.webhook_router import DbWebhookRouter
from va_telnyx.client_state import ClientState, encode
from va_telnyx.voice_events import CallInitiated
from va_telnyx.webhooks import EventPayload

pytestmark = pytest.mark.integration


@dataclass
class RecordingRouter:
    routed: list[tuple[str, str | None]] = field(default_factory=list)

    async def route(self, event: StoredEvent, payload: CallInitiated) -> None:
        self.routed.append((event.telnyx_event_id, payload.to))


@dataclass
class Rig:
    database: Database
    redis: FakeAsyncRedis
    bus_server: FakeServer
    signaller: RecordingSignaller
    handler: CarrierEventHandler
    metrics: Any

    async def process(self, body: bytes, *, attributed: SeededCall | None = None) -> TelnyxEvent:
        """Store ``body`` (attributed to ``attributed`` like the intake does with a valid client_state),
        run it through a processor, and return the row afterwards."""
        values = {} if attributed is None else {"org_id": attributed.org_id, "call_id": attributed.call_id}
        stored = await stored_event(self.database, body, **values)
        return await self.rerun(stored)

    async def rerun(self, stored: StoredEvent) -> TelnyxEvent:
        processor = make_processor(self.database, self.handler.handlers())
        async with running(processor):
            assert processor.offer(stored)
            await processor.join()
        return await event_row(self.database, stored.id)

    async def bus(self, call: SeededCall) -> list[tuple[str, dict[str, Any]]]:
        """``(type, data)`` of the call's events on ``events:calls`` (in stream order)."""
        entries: Any = await self.redis.xrange(stream_key("calls"))
        events = [decode(fields[b"payload"]) for _, fields in entries]
        return [(event.type.value, dict(event.data)) for event in events if event.call_id == call.call_id]


def make_rig(database: Database, *, inbound: RecordingRouter | None = None) -> Rig:
    bus_server = FakeServer()
    redis = FakeAsyncRedis(server=bus_server)
    publisher = EventPublisher(
        redis,
        call_events=DirectCallEventWriter(DbCallEventWriter(database)),
        router=DbWebhookRouter(database),
    )
    signaller = RecordingSignaller()
    metrics = build_metrics(Service.TELEPHONY_EDGE)
    handler = CarrierEventHandler(
        database, redis, publisher, signaller, clock=SystemClock(), metrics=metrics, inbound=inbound
    )
    return Rig(database, redis, bus_server, signaller, handler, metrics)


@pytest.fixture
def rig(database: Database) -> Rig:
    return make_rig(database)


def ccid() -> str:
    return f"v3:{secrets.token_hex(10)}"


def body(
    event_type: str,
    call_control_id: str,
    *,
    call: SeededCall | None = None,
    leg: str = "primary",
    occurred_at: datetime | None = None,
    **payload: Any,
) -> bytes:
    data: dict[str, Any] = {
        "call_control_id": call_control_id,
        "call_leg_id": f"leg-{call_control_id}",
        **payload,
    }
    if call is not None:
        state = ClientState(call_id=call.call_id, leg=leg, org_id=call.org_id)  # type: ignore[arg-type]
        data["client_state"] = encode(state)
    envelope = {
        "data": {
            "record_type": "event",
            "id": str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
            "payload": data,
        }
    }
    return orjson.dumps(envelope)


async def call_event_types(database: Database, call: SeededCall) -> list[str]:
    async with database.tenant_session(call.org_id) as session:
        rows = await session.scalars(
            select(CallEvent).where(CallEvent.call_id == call.call_id).order_by(CallEvent.seq)
        )
        return [row.message or row.category for row in rows]


async def test_outbound_call_lifecycle(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="dialing")
    leg_id = ccid()
    pubsub = rig.redis.pubsub()
    await pubsub.subscribe(carrier_channel(call.call_id))
    await pubsub.get_message(timeout=1.0)

    row = await rig.process(body("call.initiated", leg_id, call=call, direction="outgoing"), attributed=call)
    assert row.process_error is None
    assert (await load_call(database, call)).status == "ringing"
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert (leg.state, leg.role, leg.call_leg_id) == ("initiated", "primary", f"leg-{leg_id}")

    answered_at = datetime.now(UTC).replace(microsecond=0)
    await rig.process(body("call.answered", leg_id, call=call, occurred_at=answered_at), attributed=call)
    stored = await load_call(database, call)
    assert (stored.status, stored.answered_at, stored.started_at) == ("in_progress", answered_at, answered_at)
    state = await rig.redis.hgetall(carrier_state_key(call.call_id))
    assert state == {b"answered": b"1", b"answered_at": answered_at.isoformat().encode()}
    assert 0 < await rig.redis.ttl(carrier_state_key(call.call_id)) <= 7200

    ended_at = answered_at + timedelta(seconds=42, milliseconds=500)
    await rig.process(
        body(
            "call.hangup",
            leg_id,
            call=call,
            occurred_at=ended_at,
            hangup_cause="normal_clearing",
            hangup_source="callee",
        ),
        attributed=call,
    )
    final = await load_call(database, call)
    assert (final.status, final.end_reason, final.ended_by) == ("completed", "user_hangup", "user")
    assert (final.ended_at, final.connected_seconds) == (ended_at, Decimal("42.500"))
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert (leg.state, leg.hangup_cause, leg.hangup_source) == ("hangup", "normal_clearing", "callee")

    assert await rig.bus(call) == [
        ("call.ringing", {}),
        ("call.answered", {}),
        ("call.ended", {"end_reason": "user_hangup", "ended_by": "user"}),
    ]
    async with database.tenant_session(call.org_id) as session:
        seqs = (await session.scalars(select(CallEvent.seq).where(CallEvent.call_id == call.call_id))).all()
    assert sorted(seqs) == [1, 2, 3]
    channel = []
    while (message := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)) is not None:
        channel.append(CarrierEvent.model_validate_json(message["data"]))
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert [event.type for event in channel] == ["call.initiated", "call.answered", "call.hangup"]
    assert all(
        (event.call_id, event.org_id, event.leg) == (call.call_id, call.org_id, "primary")
        for event in channel
    )
    assert channel[0].payload["call_control_id"] == leg_id
    assert [event.type for event in rig.signaller.signals] == ["call.answered", "call.hangup"]


@pytest.mark.parametrize(("webhook_events", "delivered"), [(["call"], ["call.answered"]), (["queue"], [])])
async def test_carrier_events_honor_the_calls_webhook_events(
    database: Database, rig: Rig, webhook_events: list[str], delivered: list[str]
) -> None:
    url = f"https://calls-{secrets.token_hex(4)}.hooks.test/live"
    call = await seed_call(
        database, status="ringing", config={"webhook": url, "webhook_events": webhook_events}
    )
    await rig.process(body("call.answered", ccid(), call=call), attributed=call)
    async with database.tenant_session(call.org_id) as session:
        rows = (
            await session.execute(
                select(WebhookDelivery.event_type, WebhookDelivery.url, WebhookDelivery.payload).where(
                    WebhookDelivery.call_id == call.call_id
                )
            )
        ).all()
    assert [(event_type, found) for event_type, found, _ in rows] == [(t, url) for t in delivered]
    for _, _, payload in rows:
        assert (payload["call_id"], payload["category"]) == (str(call.call_id), "call")


async def test_busy_before_answer(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="ringing")
    leg_id = ccid()
    await rig.process(
        body("call.hangup", leg_id, call=call, hangup_cause="user_busy", sip_hangup_cause="486"),
        attributed=call,
    )
    stored = await load_call(database, call)
    assert (stored.status, stored.end_reason, stored.ended_by, stored.disposition_tag) == (
        "busy",
        "busy",
        "system",
        "BUSY",
    )
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert (leg.state, leg.sip_hangup_cause) == ("hangup", "486")
    assert await rig.bus(call) == [("call.ended", {"end_reason": "busy", "ended_by": "system"})]


async def test_running_pipeline_owns_the_end_of_an_answered_call(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="in_progress", answered_at=datetime.now(UTC))
    leg_id = ccid()
    await seed_leg(database, call, leg_id, state="answered")
    await rig.redis.set(owner_key(call.call_id), "pod-1")
    await rig.process(body("call.hangup", leg_id, call=call, hangup_cause="normal_clearing"), attributed=call)
    stored = await load_call(database, call)
    assert (stored.status, stored.end_reason) == ("in_progress", None)
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert leg.state == "hangup"
    assert await rig.bus(call) == []
    assert [event.type for event in rig.signaller.signals] == ["call.hangup"]


async def test_event_without_client_state_is_resolved_through_call_legs(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="ringing")
    leg_id = ccid()
    await seed_leg(database, call, leg_id)
    row = await rig.process(body("call.answered", leg_id))
    assert row.process_error is None
    assert (await load_call(database, call)).status == "in_progress"
    assert [event.leg for event in rig.signaller.signals] == ["primary"]


async def test_events_of_unknown_legs_and_calls_are_processed_without_effects(
    database: Database, rig: Rig
) -> None:
    ghost = SeededCall(org_id=(await seed_call(database)).org_id, call_id=uuid.uuid4())
    with capture_logs() as logs:
        unknown_leg = await rig.process(body("call.answered", ccid()))
        unknown_call = await rig.process(body("call.answered", ccid(), call=ghost), attributed=ghost)
        no_router = await rig.process(body("call.initiated", ccid(), direction="incoming", to="+14155550111"))
    assert [row.process_error for row in (unknown_leg, unknown_call, no_router)] == [None, None, None]
    assert all(row.processed_at is not None for row in (unknown_leg, unknown_call, no_router))
    events = [entry["event"] for entry in logs]
    assert events.count("telnyx.event_unattributed") == 2
    assert events.count("telnyx.inbound_unrouted") == 1
    assert rig.signaller.signals == []


async def test_incoming_call_goes_to_the_inbound_router(database: Database) -> None:
    router = RecordingRouter()
    rig = make_rig(database, inbound=router)
    payload = body("call.initiated", ccid(), direction="incoming", to="+14155550111")
    row = await rig.process(payload)
    assert router.routed == [(row.telnyx_event_id, "+14155550111")]


async def test_answer_before_initiated_keeps_the_later_status(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="dialing")
    leg_id = ccid()
    await rig.process(body("call.answered", leg_id, call=call), attributed=call)
    await rig.process(body("call.initiated", leg_id, call=call, call_session_id="sess-9"), attributed=call)
    assert (await load_call(database, call)).status == "in_progress"
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert (leg.state, leg.call_session_id) == ("answered", "sess-9")
    assert [name for name, _ in await rig.bus(call)] == ["call.answered"]


async def test_a_late_primary_answer_never_reverts_a_transfer(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="transferring", answered_at=datetime.now(UTC))
    await rig.process(body("call.answered", ccid(), call=call), attributed=call)
    assert (await load_call(database, call)).status == "transferring"
    assert await rig.bus(call) == []


async def test_reprocessing_an_applied_event_emits_nothing_new(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="ringing")
    stored = await stored_event(
        database, body("call.answered", ccid(), call=call), org_id=call.org_id, call_id=call.call_id
    )
    await rig.rerun(stored)
    await rig.rerun(stored)
    assert [name for name, _ in await rig.bus(call)] == ["call.answered"]
    assert len(rig.signaller.signals) == 2


async def test_publish_failure_is_retried_and_emits_once(database: Database) -> None:
    rig = make_rig(database)
    bus_down = FakeServer()
    bus_down.connected = False
    failing = EventPublisher(FakeAsyncRedis(server=bus_down), call_events=None)
    healthy = rig.handler
    rig.handler = CarrierEventHandler(
        database, rig.redis, failing, rig.signaller, clock=SystemClock(), metrics=rig.metrics
    )
    call = await seed_call(database, status="ringing")
    first = await rig.process(body("call.answered", ccid(), call=call), attributed=call)
    assert first.process_error is not None
    assert first.process_error.startswith("EventPublishError")
    assert (await load_call(database, call)).status == "in_progress"
    assert await rig.bus(call) == []

    rig.handler = healthy
    retried = await rig.rerun(StoredEvent.from_row(first))
    assert retried.process_error is None
    assert retried.process_attempts == 2
    assert [name for name, _ in await rig.bus(call)] == ["call.answered"]


async def test_signal_failure_marks_the_event_for_retry(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="ringing")
    rig.signaller.error = ConnectionError("temporal down")
    row = await rig.process(body("call.answered", ccid(), call=call), attributed=call)
    assert row.process_error == "ConnectionError: temporal down"
    assert (await load_call(database, call)).status == "in_progress"
    assert [name for name, _ in await rig.bus(call)] == ["call.answered"]


async def test_premium_amd_sets_answered_by_and_counts_the_result(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="in_progress", answered_at=datetime.now(UTC))
    leg_id = ccid()
    await seed_leg(database, call, leg_id, state="answered")
    await rig.process(
        body("call.machine.premium.detection.ended", leg_id, call=call, result="fax_detected"),
        attributed=call,
    )
    assert (await load_call(database, call)).answered_by == "fax"
    leg = await load_leg(database, call, leg_id)
    assert leg is not None
    assert leg.amd_result == "fax_detected"
    assert await rig.bus(call) == [("call.amd", {"result": "fax_detected", "answered_by": "fax"})]
    state = await rig.redis.hgetall(carrier_state_key(call.call_id))
    assert state == {b"amd_result": b"fax_detected", b"answered_by": b"fax"}
    sample = rig.metrics.registry.get_sample_value("va_amd_results_total", {"result": "fax_detected"})
    assert sample == 1.0


async def test_cold_transfer_billing_clocks(database: Database, rig: Rig) -> None:
    answered = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=60)
    call = await seed_call(database, status="transferring", answered_at=answered)
    primary, target = ccid(), ccid()
    await seed_leg(database, call, primary, state="answered")
    bridged_at = answered + timedelta(seconds=30)
    await rig.process(
        body(
            "call.bridged",
            target,
            call=call,
            leg="transfer_target",
            occurred_at=bridged_at,
            to="+14155550177",
        ),
        attributed=call,
    )
    stored = await load_call(database, call)
    assert (stored.status, stored.transferred_to) == ("transferred", "+14155550177")
    target_leg = await load_leg(database, call, target)
    assert target_leg is not None
    assert (target_leg.role, target_leg.state) == ("transfer_target", "bridged")

    ended_at = bridged_at + timedelta(seconds=15)
    await rig.process(
        body("call.hangup", primary, call=call, occurred_at=ended_at, hangup_cause="normal_clearing"),
        attributed=call,
    )
    final = await load_call(database, call)
    assert (final.status, final.end_reason) == ("completed", "transferred")
    assert (final.connected_seconds, final.transfer_seconds) == (Decimal("30.000"), Decimal("15.000"))
    assert [name for name, _ in await rig.bus(call)] == ["call.transferred", "call.ended"]


async def test_handler_ignores_non_voice_payloads(database: Database, rig: Rig) -> None:
    stored = await stored_event(database)
    unknown: Any = object()
    payload: EventPayload = unknown
    await rig.handler.handle(stored, payload)
    assert rig.signaller.signals == []


async def test_call_events_rows_are_written(database: Database, rig: Rig) -> None:
    call = await seed_call(database, status="ringing")
    await rig.process(body("call.answered", ccid(), call=call), attributed=call)
    assert len(await call_event_types(database, call)) == 1
