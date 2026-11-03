"""``GET /v1/calls/{call_id}/events/stream`` over HTTP (specs/19-events-and-webhooks.md §9 Decision P06-T06a):
the response headers, errors raised before the stream starts, frames arriving while the response is open on
a real socket, and the subscription released when the client goes away."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Final

import httpx
import pytest
from call_helpers import bearer
from clock_helpers import ManualClock
from identity_db import IdentityDb
from live_stream_helpers import ReleaseWatch, partition_clock, publish_event, streamed_call
from service_helpers import serving
from session_helpers import SessionContainer, error_of, session_app, session_client

from va_events import EventType
from va_events.keys import call_live_channel

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

WAIT_S: Final = 5.0


def _stream_path(call_id: uuid.UUID | str) -> str:
    return f"/v1/calls/{call_id}/events/stream"


@pytest.fixture
def clock() -> ManualClock:
    return partition_clock()


@pytest.fixture
def streaming(identity: IdentityDb, clock: ManualClock) -> SessionContainer:
    return SessionContainer(identity=identity, clock=clock)


@pytest.fixture
async def http(streaming: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(streaming)) as client:
        yield client


async def _finished_call(container: SessionContainer) -> tuple[uuid.UUID, str]:
    """A call with ``call.started`` (seq 1) and a ``call.completed`` from ten minutes ago (seq 2) stored."""
    org_id, call_id, key = await streamed_call(container)
    await publish_event(container, org_id, call_id, EventType.CALL_STARTED, live=False)
    old = container.clock.now() - timedelta(minutes=10)
    await publish_event(container, org_id, call_id, EventType.CALL_COMPLETED, at=old, live=False)
    return call_id, key


async def test_a_finished_call_streams_its_events_and_ends(
    streaming: SessionContainer, http: httpx.AsyncClient
) -> None:
    call_id, key = await _finished_call(streaming)

    response = await http.get(_stream_path(call_id), headers=bearer(key))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-request-id"]
    chunks = response.content.split(b"\n\n")
    assert chunks[0] == b"retry: 3000"
    assert [chunk.split(b"\n")[:2] for chunk in chunks[1:3]] == [
        [b"id: 1", b"event: call.started"],
        [b"id: 2", b"event: call.completed"],
    ]
    assert b'"metadata":{"crm":"7"}' in chunks[1]
    assert streaming.live_hub.subscribed_channels == frozenset()


async def test_last_event_id_header_skips_received_events(
    streaming: SessionContainer, http: httpx.AsyncClient
) -> None:
    call_id, key = await _finished_call(streaming)

    response = await http.get(_stream_path(call_id), headers={**bearer(key), "Last-Event-ID": "1"})

    assert response.status_code == 200
    assert response.content.split(b"\n\n")[1].startswith(b"id: 2\nevent: call.completed\n")


async def test_errors_are_answered_before_the_stream_starts(
    streaming: SessionContainer, http: httpx.AsyncClient
) -> None:
    _, call_id, key = await streamed_call(streaming)
    _, _, narrow = await streamed_call(streaming, scopes=("org:read",))

    assert error_of(await http.get(_stream_path(uuid.uuid4()), headers=bearer(key)))[:2] == (
        404,
        "CALL_NOT_FOUND",
    )
    assert error_of(await http.get(_stream_path("not-a-uuid"), headers=bearer(key)))[:2] == (
        404,
        "CALL_NOT_FOUND",
    )
    bad = await http.get(_stream_path(call_id), headers={**bearer(key), "Last-Event-ID": "x"})
    assert error_of(bad) == (400, "INVALID_INPUT", "Last-Event-ID")
    assert error_of(await http.get(_stream_path(call_id), headers=bearer(narrow)))[:2] == (403, "FORBIDDEN")
    await streaming.live_hub.aclose()
    unavailable = await http.get(_stream_path(call_id), headers=bearer(key))
    assert error_of(unavailable)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert streaming.live_hub.subscribed_channels == frozenset()


async def _next_frame(chunks: AsyncIterator[bytes], buffer: bytearray) -> bytes:
    async with asyncio.timeout(WAIT_S):
        while b"\n\n" not in buffer:
            buffer.extend(await anext(chunks))
    end = buffer.index(b"\n\n") + 2
    frame = bytes(buffer[:end])
    del buffer[:end]
    return frame


async def test_live_frames_arrive_on_an_open_socket_and_disconnect_releases_the_subscription(
    streaming: SessionContainer, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    await publish_event(streaming, org_id, call_id, EventType.CALL_STARTED, live=False)
    async with serving(session_app(streaming)) as server, httpx.AsyncClient(base_url=server.url) as client:
        watch = ReleaseWatch(streaming.live_hub, call_live_channel(call_id))
        monkeypatch.setattr(streaming.live_hub, "unsubscribe", watch)
        async with client.stream("GET", _stream_path(call_id), headers=bearer(key)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
            chunks = response.aiter_bytes()
            buffer = bytearray()
            assert await _next_frame(chunks, buffer) == b"retry: 3000\n\n"
            assert (await _next_frame(chunks, buffer)).startswith(b"id: 1\nevent: call.started\n")
            live = await publish_event(streaming, org_id, call_id, EventType.CALL_LATENCY)
            assert (await _next_frame(chunks, buffer)).startswith(
                f"id: {live.seq}\nevent: call.latency\n".encode()
            )
            assert streaming.live_hub.subscribers(call_live_channel(call_id)) == 1
        # The server notices the closed socket at the latest when the next heartbeat is written.
        await clock.run_for(15.0)
        async with asyncio.timeout(WAIT_S):
            await watch.released.wait()
    assert streaming.live_hub.subscribed_channels == frozenset()
