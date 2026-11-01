"""FakeTelnyx webhooks through the edge intake (specs/06-telephony-telnyx.md §4.1; specs/30 FakeTelnyx).

A simulated call's webhooks are captured by a recording transport and replayed against the edge with the
simulator's public key as the platform key: every one verifies and is stored exactly once, including the
redeliveries of ``force_duplicate_webhooks``.
"""

from __future__ import annotations

import secrets

import pytest
from edge_helpers import DbContainer, edge_settings, metric
from service_helpers import make_client
from sqlalchemy import func, select
from telnyx_helpers import RecordingTransport, answered_call, ok, settle, telnyx_client, telnyx_state

from va_common.time import FrozenClock
from va_db.models import TelnyxEvent
from va_db.session import Database
from va_edge.main import create_app

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
