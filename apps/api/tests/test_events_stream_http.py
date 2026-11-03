"""``GET /v1/events/stream`` over HTTP (specs/19-events-and-webhooks.md §9 Decision P06-T06c): who may open
it, errors answered before the stream starts, frames of the caller's org arriving on a real socket, and the
subscription released when the client goes away."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import httpx
import orjson
import pytest
from call_helpers import bearer
from clock_helpers import ManualClock
from identity_db import IdentityDb
from live_stream_helpers import next_frame, partition_clock, streamed_call
from service_helpers import serving
from session_helpers import SessionContainer, error_of, session_app, session_client, signed_in

from va_events import DomainEvent, EventPublisher, EventType, StreamTailHub, TailSubscription, new_event

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

PATH: Final = "/v1/events/stream"
WAIT_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class Served:
    """The container (on a manual clock inside the ``call_events`` partitions) and an in-process client."""

    container: SessionContainer
    clock: ManualClock
    http: httpx.AsyncClient


@pytest.fixture
async def served(identity: IdentityDb) -> AsyncIterator[Served]:
    manual = partition_clock()
    container = SessionContainer(identity=identity, clock=manual)
    async with session_client(session_app(container)) as client:
        yield Served(container, manual, client)


class ReleaseWatch:
    """Wraps ``hub.unsubscribe``; :attr:`released` is set once the hub has no subscriber left."""

    def __init__(self, hub: StreamTailHub) -> None:
        self.released = asyncio.Event()
        self._hub = hub
        self._unsubscribe = hub.unsubscribe

    async def __call__(self, subscription: TailSubscription) -> None:
        await self._unsubscribe(subscription)
        if self._hub.subscribers() == 0:
            self.released.set()


async def _publish(
    container: SessionContainer, org_id: uuid.UUID, event_type: EventType, call_id: uuid.UUID | None = None
) -> DomainEvent:
    event = new_event(event_type, org_id=org_id, clock=container.clock, call_id=call_id, data={"n": 1})
    await EventPublisher(container.redis, call_events=None).broadcast(event)
    return event


async def test_an_api_key_streams_its_orgs_events_and_disconnect_releases_the_subscription(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    streaming, clock = served.container, served.clock
    org_id, call_id, key = await streamed_call(streaming, scopes=("webhooks:read",))
    other_org, other_call, _ = await streamed_call(streaming)
    async with serving(session_app(streaming)) as server, httpx.AsyncClient(base_url=server.url) as client:
        watch = ReleaseWatch(streaming.stream_tail)
        monkeypatch.setattr(streaming.stream_tail, "unsubscribe", watch)
        params = {"types": "call.*"}
        async with client.stream("GET", PATH, params=params, headers=bearer(key)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
            assert response.headers["cache-control"] == "no-store"
            chunks = response.aiter_bytes()
            buffer = bytearray()
            assert await next_frame(chunks, buffer) == b"retry: 3000\n\n"
            await _publish(streaming, other_org, EventType.CALL_LATENCY, other_call)
            await _publish(streaming, org_id, EventType.SMS_RECEIVED)
            mine = await _publish(streaming, org_id, EventType.CALL_LATENCY, call_id)
            frame = await next_frame(chunks, buffer)
            assert frame.startswith(f"id: {mine.id}\nevent: call.latency\ndata: ".encode())
            body = orjson.loads(frame.split(b"data: ", 1)[1])
            assert (body["id"], body["org_id"], body["data"]["metadata"]) == (
                mine.id,
                str(org_id),
                {"crm": "7"},
            )
            assert streaming.stream_tail.subscribers(org_id) == 1
        # The server notices the closed socket at the latest when the next heartbeat is written.
        await clock.run_for(15.0)
        async with asyncio.timeout(WAIT_S):
            await watch.released.wait()
    assert not streaming.stream_tail.reader_running


async def test_errors_are_answered_before_the_stream_starts(served: Served) -> None:
    streaming, http = served.container, served.http
    _, _, key = await streamed_call(streaming, scopes=("webhooks:read",))
    _, _, narrow = await streamed_call(streaming, scopes=("calls:read",))

    bad = await http.get(PATH, params={"types": "call.*,BAD!"}, headers=bearer(key))
    assert error_of(bad) == (400, "INVALID_INPUT", "types")
    assert error_of(await http.get(PATH, headers=bearer(narrow)))[:2] == (403, "FORBIDDEN")
    await streaming.stream_tail.aclose()
    assert error_of(await http.get(PATH, headers=bearer(key)))[:2] == (503, "SERVICE_UNAVAILABLE")
    assert streaming.stream_tail.subscribers() == 0


async def test_dashboard_sessions_need_a_role_with_webhook_access(
    identity: IdentityDb, served: Served
) -> None:
    streaming, http = served.container, served.http
    await signed_in(http, identity, streaming, "billing")
    assert error_of(await http.get(PATH))[:2] == (403, "FORBIDDEN")
    http.cookies.clear()
    await signed_in(http, identity, streaming, "developer")
    # Past authentication and the scope check, the malformed filter is what is refused.
    assert error_of(await http.get(PATH, params={"types": "BAD!"})) == (400, "INVALID_INPUT", "types")
