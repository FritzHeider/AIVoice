"""FakeTelnyx webhooks through the edge intake (specs/06-telephony-telnyx.md §4.1; specs/30 FakeTelnyx).

A simulated call's webhooks are captured by a recording transport and replayed against the edge with the
simulator's public key as the platform key: every one verifies and is stored exactly once, including the
redeliveries of ``force_duplicate_webhooks``. With a ``client_state`` naming a seeded call, the carrier
handler drives that call and its leg through the simulated lifecycle (specs/06 §5). Incoming calls to numbers
the platform refuses are rejected or transferred to the fallback number in the simulator (specs/06 §7).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

import pytest
from call_rows import load_call, load_leg, seed_call
from edge_helpers import DbContainer, RecordingSignaller, edge_settings, metric
from fakeredis import FakeAsyncRedis
from routing_helpers import CALLER, FALLBACK, RecordingAcceptor, RoutingRig, random_number, routing_rig
from service_helpers import make_client
from sqlalchemy import func, select
from telnyx_helpers import (
    Received,
    RecordingTransport,
    answered_call,
    at,
    ok,
    settle,
    telnyx_client,
    telnyx_state,
)

from va_common.metrics import Service, build_metrics
from va_common.time import FrozenClock, SystemClock
from va_db.models import TelnyxEvent
from va_db.session import Database
from va_edge.carrier import CarrierEventHandler
from va_edge.main import create_app
from va_edge.routing import InboundAcceptor
from va_events.publisher import EventPublisher
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx.client_state import ClientState, encode

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("duplicates", [False, True])
async def test_simulated_call_webhooks_are_verified_and_stored_once(
    edge_db_url: str, database: Database, duplicates: bool
) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport, seed=secrets.randbits(32))
    state.telnyx.config = state.telnyx.config.model_copy(update={"force_duplicate_webhooks": duplicates})
    async with telnyx_client(state) as sim:
        _, ccid = await answered_call(sim, state)
        await ok(sim, ccid, "hangup")
        await settle(state)
    received = transport.received
    assert {item.event_type for item in received} >= {"call.initiated", "call.answered", "call.hangup"}
    unique = {item.envelope["data"]["id"] for item in received}
    assert (len(received) > len(unique)) is duplicates

    app = create_app(
        edge_settings(state.signing_key.public_key_b64),
        container_factory=DbContainer(edge_db_url),
        init_observability=False,
        clock=FrozenClock(state.clock.now()),
    )
    statuses: list[str] = []
    async with make_client(app) as client:
        for item in received:
            path = "/telnyx/voice" if "/telnyx/voice" in item.url else "/telnyx/messaging"
            response = await client.post(path, content=item.body, headers=dict(item.headers))
            assert response.status_code == 200, response.text
            statuses.append(response.json()["status"])
        await app.state.processor.join()
        assert metric(app, "unverified", "invalid_signature") == 0
    assert statuses.count("ok") == len(unique)
    assert statuses.count("duplicate") == len(received) - len(unique)
    async with database.system_session() as session:
        statement = select(func.count()).where(TelnyxEvent.telnyx_event_id.in_(unique))
        assert await session.scalar(statement) == len(unique)
        rows = (
            await session.scalars(select(TelnyxEvent).where(TelnyxEvent.telnyx_event_id.in_(unique)))
        ).all()
    assert {row.call_control_id for row in rows} == {ccid}
    assert all(row.processed_at is not None for row in rows)


class SimTimeClock(SystemClock):
    """The simulator's wall time (webhook timestamps verify against it) with real sleeps (handler budget)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def test_simulated_call_drives_the_call_and_leg_state(edge_db_url: str, database: Database) -> None:
    call = await seed_call(database, status="dialing")
    transport = RecordingTransport()
    state = telnyx_state(transport, seed=secrets.randbits(32))
    leg_state = encode(ClientState(call_id=call.call_id, leg="primary", org_id=call.org_id))
    async with telnyx_client(state) as sim:
        _, ccid = await answered_call(sim, state, client_state=leg_state)
        await ok(sim, ccid, "hangup")
        await settle(state)

    redis = FakeAsyncRedis()
    signaller = RecordingSignaller()
    handler = CarrierEventHandler(
        database,
        redis,
        EventPublisher(redis, call_events=None),
        signaller,
        clock=SystemClock(),
        metrics=build_metrics(Service.TELEPHONY_EDGE),
    )
    app = create_app(
        edge_settings(state.signing_key.public_key_b64),
        container_factory=DbContainer(edge_db_url),
        init_observability=False,
        clock=SimTimeClock(state.clock.now()),
        handlers=handler.handlers(),
    )
    async with make_client(app) as client:
        for item in transport.received:
            if "/telnyx/voice" in item.url:
                response = await client.post("/telnyx/voice", content=item.body, headers=dict(item.headers))
                assert response.status_code == 200, response.text
        await app.state.processor.join()

    stored = await load_call(database, call)
    assert stored.status == "completed"
    assert stored.answered_at is not None
    assert stored.end_reason == "user_hangup"
    leg = await load_leg(database, call, ccid)
    assert leg is not None
    assert leg.state == "hangup"
    assert leg.hangup_cause is not None
    assert {event.type for event in signaller.signals} >= {"call.answered", "call.hangup"}


# ---- inbound routing (specs/06 §7, Decision P05-T12b) -----------------------------------------------------


async def route_through_edge(
    rig: RoutingRig, edge_db_url: str, initiated: Received, acceptor: InboundAcceptor | None = None
) -> None:
    """Deliver FakeTelnyx's ``call.initiated`` to a running edge app with the production handlers, whose
    commands go back to the same FakeTelnyx; returns once the event was processed and the sim settled."""
    container = DbContainer(edge_db_url, redis=rig.redis, telnyx=rig.factory)
    app = create_app(
        edge_settings(rig.state.signing_key.public_key_b64),
        container_factory=container,
        init_observability=False,
        clock=SimTimeClock(rig.state.clock.now()),
        acceptor=acceptor,
    )
    async with make_client(app) as client:
        response = await client.post("/telnyx/voice", content=initiated.body, headers=dict(initiated.headers))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
        await app.state.processor.join()
    await settle(rig.state)


async def stored_error(database: Database, initiated: Received) -> str | None:
    async with database.system_session() as session:
        row = await session.scalar(
            select(TelnyxEvent).where(TelnyxEvent.telnyx_event_id == initiated.envelope["data"]["id"])
        )
    assert row is not None
    return row.process_error


async def test_unknown_number_is_rejected_by_the_edge(edge_db_url: str, database: Database) -> None:
    async with routing_rig(database) as rig:
        to = random_number()
        record_id = rig.state.ids.uuid()
        rig.state.telnyx.accounts[DEFAULT_API_KEY].numbers[record_id] = {
            "id": record_id,
            "phone_number": to,
            "connection_id": rig.app_ids[DEFAULT_API_KEY],
            "messaging_profile_id": None,
        }
        ccid, initiated = await rig.place(to)
        await route_through_edge(rig, edge_db_url, initiated)
        leg = await rig.leg(ccid)
        assert (leg["ended_at"] is not None, leg["hangup_cause"]) == (True, "call_rejected")
        assert await stored_error(database, initiated) == "unknown_number"


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"blocked_callers": [CALLER], "fallback_number": FALLBACK}, None),
        ({"prompt": "Hello"}, "org_suspended"),
    ],
)
async def test_rejected_calls_end_in_the_sim(
    edge_db_url: str, database: Database, config: dict[str, Any], reason: str | None
) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="active" if reason is None else "suspended")
        ccid, initiated = await rig.place(await rig.number(org, config))
        await route_through_edge(rig, edge_db_url, initiated)
        leg = await rig.leg(ccid)
        assert (leg["hangup_cause"], leg["answered_at"], leg["transfers"]) == ("call_rejected", None, [])
        assert await stored_error(database, initiated) == (reason or "caller_blocked")


@pytest.mark.parametrize(
    ("status", "config", "reason"),
    [
        ("suspended", {"fallback_number": FALLBACK}, "org_suspended"),
        ("active", {"fallback_number": FALLBACK, "maintenance": {"enabled": True}}, "maintenance"),
    ],
)
async def test_fallback_transfers_answer_the_caller_when_the_fallback_answers(
    edge_db_url: str, database: Database, status: str, config: dict[str, Any], reason: str
) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status=status)
        ccid, initiated = await rig.place(await rig.number(org, config))
        await route_through_edge(rig, edge_db_url, initiated)
        await at(rig.state, rig.state.clock.virtual_ms() / 1000 + 2)
        leg = await rig.leg(ccid)
        [transfer] = leg["transfers"]
        assert transfer["to"] == FALLBACK
        # The accelerated sim clock may already have run the bridged call to its time limit: assert history.
        assert leg["answered_at"] is not None
        assert [e["event_type"] for e in leg["events"]][:3] == [
            "call.initiated",
            "call.answered",
            "call.bridged",
        ]
        assert await stored_error(database, initiated) == reason


async def test_accepted_calls_reach_the_acceptor_given_to_the_app(
    edge_db_url: str, database: Database
) -> None:
    acceptor = RecordingAcceptor()
    async with routing_rig(database) as rig:
        org = await rig.org()
        to = await rig.number(org, {"prompt": "Hello"})
        ccid, initiated = await rig.place(to)
        await route_through_edge(rig, edge_db_url, initiated, acceptor)
        [call] = acceptor.calls
        assert (call.payload.call_control_id, call.number.e164, call.caller) == (ccid, to, CALLER)
        assert await stored_error(database, initiated) is None
        assert (await rig.leg(ccid))["ended_at"] is None
