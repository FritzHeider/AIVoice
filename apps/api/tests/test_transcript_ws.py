"""``POST /v1/calls/{call_id}/transcript/token`` and ``WS /v1/calls/{call_id}/transcript/ws``
(specs/19-events-and-webhooks.md §9 Decision P06-T06b): the token route, the token-only handshake with its
refusals, transcript messages (replay then live, other event types skipped), the close after
``call.completed``, and the subscription released when the client goes away. Also the type filter of the
shared tail."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import orjson
import pytest
from call_helpers import bearer
from call_rows import seed_call
from clock_helpers import ManualClock
from identity_db import IdentityDb
from live_stream_helpers import ReleaseWatch, partition_clock, publish_event, streamed_call
from service_helpers import json_log_stream, log_events, serving
from session_helpers import SessionContainer, error_of, session_app, session_client
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from va_api.services import transcript_ws
from va_api.services.call_tail import (
    CLOSE_AFTER_COMPLETED_S,
    GAP_HOLD_S,
    MAX_STREAM_S,
    StreamDeps,
    call_metadata,
    subscribe_tail,
)
from va_api.services.transcript_tokens import TRANSCRIPT_TOKEN_TTL_S, TranscriptTokens, token_key
from va_api.services.transcript_ws import TRANSCRIPT_TYPES
from va_events import DomainEvent, EventType, iso_ms
from va_events.keys import call_live_channel

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

WAIT_S: Final = 5.0
TURN: Final = EventType.CALL_TURN


@pytest.fixture
def clock() -> ManualClock:
    return partition_clock()


@pytest.fixture
def streaming(identity: IdentityDb, clock: ManualClock) -> SessionContainer:
    return SessionContainer(identity=identity, clock=clock)


@pytest.fixture
async def ws_url(streaming: SessionContainer) -> AsyncIterator[str]:
    async with serving(session_app(streaming)) as server:
        yield server.url.replace("http://", "ws://", 1)


@pytest.fixture
async def http(streaming: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(streaming)) as client:
        yield client


def _token_path(call_id: uuid.UUID | str) -> str:
    return f"/v1/calls/{call_id}/transcript/token"


async def _turn(
    container: SessionContainer, org_id: uuid.UUID, call_id: uuid.UUID, text: str, **options: Any
) -> DomainEvent:
    return await publish_event(
        container, org_id, call_id, TURN, message="assistant", data={"text": text, "turn_index": 0}, **options
    )


async def _token(http: httpx.AsyncClient, call_id: uuid.UUID, key: str) -> str:
    response = await http.post(_token_path(call_id), headers=bearer(key))
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


def _ws_path(base: str, call_id: uuid.UUID | str, token: str | None) -> str:
    query = "" if token is None else f"?token={token}"
    return f"{base}/v1/calls/{call_id}/transcript/ws{query}"


async def _message(ws: ClientConnection) -> dict[str, Any]:
    async with asyncio.timeout(WAIT_S):
        raw = await ws.recv()
    message: dict[str, Any] = orjson.loads(raw)
    return message


async def _close_of(ws: ClientConnection) -> tuple[int | None, str | None]:
    async with asyncio.timeout(WAIT_S):
        await ws.wait_closed()
    return ws.close_code, ws.close_reason


async def _refused(url: str) -> tuple[int, dict[str, Any]]:
    with pytest.raises(InvalidStatus) as caught:
        async with connect(url):
            pytest.fail("the handshake was accepted")
    response = caught.value.response
    return response.status_code, orjson.loads(response.body or b"{}")


class _ClosedLog:
    """Records the socket's ``calls.transcript_ws_closed`` lines; :attr:`logged` is set by the first one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.logged = asyncio.Event()
        self.lines: list[dict[str, Any]] = []
        self._log = transcript_ws._log
        monkeypatch.setattr(transcript_ws, "_log", self)

    def info(self, event: str, **fields: Any) -> None:
        self._log.info(event, **fields)
        if event == "calls.transcript_ws_closed":
            self.lines.append(fields)
            self.logged.set()

    async def closed(self) -> list[tuple[str, int]]:
        async with asyncio.timeout(WAIT_S):
            await self.logged.wait()
        return [(line["reason"], line["messages"]) for line in self.lines]


# The token route.


async def test_the_token_route_mints_a_token_for_the_call(
    streaming: SessionContainer, http: httpx.AsyncClient, clock: ManualClock
) -> None:
    _, call_id, key = await streamed_call(streaming, scopes=("calls:read",))

    response = await http.post(_token_path(call_id), headers=bearer(key))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"status", "token", "expires_at", "ws_url"}
    assert body["status"] == "success"
    assert body["token"].startswith("vtt_")
    assert body["ws_url"] == f"/v1/calls/{call_id}/transcript/ws?token={body['token']}"
    expires_at = clock.now() + timedelta(seconds=TRANSCRIPT_TOKEN_TTL_S)
    assert datetime.fromisoformat(body["expires_at"]) == expires_at
    assert await streaming.redis.get(token_key(body["token"])) is not None


async def test_the_token_route_refuses_foreign_calls_and_narrow_keys(
    streaming: SessionContainer, http: httpx.AsyncClient
) -> None:
    _, call_id, key = await streamed_call(streaming)
    _, other_call, _ = await streamed_call(streaming)
    _, _, narrow = await streamed_call(streaming, scopes=("org:read",))

    for path in (_token_path(uuid.uuid4()), _token_path(other_call), _token_path("not-a-uuid")):
        assert error_of(await http.post(path, headers=bearer(key)))[:2] == (404, "CALL_NOT_FOUND")
    assert error_of(await http.post(_token_path(call_id), headers=bearer(narrow)))[:2] == (403, "FORBIDDEN")
    assert await streaming.redis.keys("transcript:token:*") == []


async def test_an_idempotent_replay_withholds_the_token(
    streaming: SessionContainer, http: httpx.AsyncClient
) -> None:
    _, call_id, key = await streamed_call(streaming)
    headers = {**bearer(key), "Idempotency-Key": f"tt-{uuid.uuid4()}"}

    first = await http.post(_token_path(call_id), headers=headers)
    replay = await http.post(_token_path(call_id), headers=headers)

    assert first.json()["token"].startswith("vtt_")
    assert (replay.json()["token"], replay.json()["ws_url"]) == (None, None)
    assert replay.json()["expires_at"] == first.json()["expires_at"]


# The socket.


async def test_turns_are_replayed_then_tailed_and_other_events_skipped(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    await publish_event(streaming, org_id, call_id, EventType.CALL_STARTED, live=False)
    first = await _turn(streaming, org_id, call_id, "Hello", live=False)
    await publish_event(streaming, org_id, call_id, EventType.CALL_LATENCY, live=False)
    second = await _turn(streaming, org_id, call_id, "How can I help?", live=False)
    token = await _token(http, call_id, key)

    async with connect(_ws_path(ws_url, call_id, token)) as ws:
        replayed = [await _message(ws), await _message(ws)]
        await publish_event(streaming, org_id, call_id, EventType.CALL_LATENCY)
        live = await _turn(streaming, org_id, call_id, "Sure.")
        tailed = await _message(ws)
        await ws.send("ignored client message")
        transfer = await publish_event(
            streaming,
            org_id,
            call_id,
            EventType.CALL_POST_TRANSFER_TRANSCRIPT,
            data={"text": "after the transfer"},
        )
        after_transfer = await _message(ws)

    assert [(m["id"], m["event"]) for m in replayed] == [(first.seq, "call.turn"), (second.seq, "call.turn")]
    assert replayed[0] == {
        "id": first.seq,
        "event": "call.turn",
        "data": {
            "call_id": str(call_id),
            "category": "call",
            "level": "info",
            "message": "assistant",
            "data": {"text": "Hello", "turn_index": 0},
            "seq": first.seq,
            "created_at": iso_ms(first.occurred_at),
            "metadata": {"crm": "7"},
        },
    }
    assert (tailed["id"], tailed["data"]["data"]["text"]) == (live.seq, "Sure.")
    assert (after_transfer["id"], after_transfer["event"]) == (transfer.seq, "call.post_transfer_transcript")


async def test_a_finished_call_replays_its_turns_and_closes_normally(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _ClosedLog(monkeypatch)
    org_id, call_id, key = await streamed_call(streaming)
    turn = await _turn(streaming, org_id, call_id, "Bye", live=False)
    old = streaming.clock.now() - timedelta(minutes=10)
    await publish_event(streaming, org_id, call_id, EventType.CALL_COMPLETED, at=old, live=False)
    token = await _token(http, call_id, key)

    async with connect(_ws_path(ws_url, call_id, token)) as ws:
        assert (await _message(ws))["id"] == turn.seq
        assert await _close_of(ws) == (1000, "completed")

    assert await log.closed() == [("completed", 1)]
    assert log.lines[0]["call_id"] == str(call_id)
    assert streaming.live_hub.subscribed_channels == frozenset()


async def test_the_socket_closes_thirty_seconds_after_a_live_call_completed(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str, clock: ManualClock
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    token = await _token(http, call_id, key)

    async with connect(_ws_path(ws_url, call_id, token)) as ws:
        turn = await _turn(streaming, org_id, call_id, "Goodbye")
        assert (await _message(ws))["id"] == turn.seq
        await publish_event(streaming, org_id, call_id, EventType.CALL_COMPLETED)
        await clock.wait_for_sleep(CLOSE_AFTER_COMPLETED_S)
        await clock.run_for(CLOSE_AFTER_COMPLETED_S)
        assert await _close_of(ws) == (1000, "completed")


async def test_a_token_opens_several_connections_until_it_expires(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str, clock: ManualClock
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    turn = await _turn(streaming, org_id, call_id, "Hi", live=False)
    token = await _token(http, call_id, key)

    async with (
        connect(_ws_path(ws_url, call_id, token)) as one,
        connect(_ws_path(ws_url, call_id, token)) as two,
    ):
        assert [(await _message(one))["id"], (await _message(two))["id"]] == [turn.seq, turn.seq]

    clock.advance(TRANSCRIPT_TOKEN_TTL_S)
    status, body = await _refused(_ws_path(ws_url, call_id, token))
    assert (status, body["errors"][0]["error"]) == (401, "AUTH_FAILURE")


async def test_refused_handshakes_answer_the_error_body(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str
) -> None:
    log = json_log_stream()
    org_id, call_id, key = await streamed_call(streaming)
    _, other_org_call, other_key = await streamed_call(streaming)
    token = await _token(http, call_id, key)
    foreign = await _token(http, other_org_call, other_key)
    second = await seed_call(streaming.database, status="in_progress", org_id=org_id)

    refusals = [
        await _refused(_ws_path(ws_url, call_id, None)),
        await _refused(_ws_path(ws_url, call_id, "vtt_unknown")),
        await _refused(_ws_path(ws_url, call_id, "garbage")),
        await _refused(_ws_path(ws_url, second.call_id, token)),
        await _refused(_ws_path(ws_url, call_id, foreign)),
        await _refused(_ws_path(ws_url, "not-a-uuid", token)),
    ]

    for status, body in refusals:
        assert status == 401
        assert body["status"] == "error"
        assert body["errors"] == [
            {"error": "AUTH_FAILURE", "message": "The transcript token is invalid or expired."}
        ]
        assert body["request_id"].startswith("req_")
    reasons = [e["reason"] for e in log_events(log) if e["event"] == "calls.transcript_ws_refused"]
    assert reasons == ["missing", "unknown", "unknown", "call_mismatch", "call_mismatch", "call_mismatch"]
    assert streaming.live_hub.subscribed_channels == frozenset()


async def test_a_deleted_call_and_an_unavailable_hub_are_refused(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    token = await _token(http, call_id, key)
    ghost = uuid.uuid4()
    minted = await _token_for(streaming, org_id, ghost)

    status, body = await _refused(_ws_path(ws_url, ghost, minted))
    assert (status, body["errors"][0]["error"]) == (404, "CALL_NOT_FOUND")
    await streaming.live_hub.aclose()
    status, body = await _refused(_ws_path(ws_url, call_id, token))
    assert (status, body["errors"][0]["error"]) == (503, "SERVICE_UNAVAILABLE")


async def _token_for(container: SessionContainer, org_id: uuid.UUID, call_id: uuid.UUID) -> str:
    """A token minted directly (for a call the route would refuse)."""
    return (await TranscriptTokens(container.redis, container.clock).mint(org_id, call_id)).token


async def test_a_lost_subscription_closes_with_1011(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str
) -> None:
    org_id, call_id, key = await streamed_call(streaming)
    turn = await _turn(streaming, org_id, call_id, "Hi", live=False)
    token = await _token(http, call_id, key)

    async with connect(_ws_path(ws_url, call_id, token)) as ws:
        assert (await _message(ws))["id"] == turn.seq
        await streaming.live_hub.aclose()
        assert await _close_of(ws) == (1011, "closed")


async def test_a_client_disconnect_releases_the_subscription(
    streaming: SessionContainer, http: httpx.AsyncClient, ws_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _ClosedLog(monkeypatch)
    org_id, call_id, key = await streamed_call(streaming)
    token = await _token(http, call_id, key)
    watch = ReleaseWatch(streaming.live_hub, call_live_channel(call_id))
    monkeypatch.setattr(streaming.live_hub, "unsubscribe", watch)

    async with connect(_ws_path(ws_url, call_id, token)) as ws:
        turn = await _turn(streaming, org_id, call_id, "Hi")
        assert (await _message(ws))["id"] == turn.seq
        assert streaming.live_hub.subscribers(call_live_channel(call_id)) == 1
    async with asyncio.timeout(WAIT_S):
        await watch.released.wait()

    assert streaming.live_hub.subscribed_channels == frozenset()
    assert await log.closed() == [("client_gone", 1)]


# The shared tail's type filter.


async def test_the_filtered_tail_waits_for_skipped_seqs_and_closes_on_a_filtered_completion(
    streaming: SessionContainer, clock: ManualClock
) -> None:
    org_id, call_id, _ = await streamed_call(streaming)
    deps = StreamDeps(database=streaming.database, live_hub=streaming.live_hub, clock=clock)
    tail = await subscribe_tail(
        deps,
        org_id=org_id,
        call_id=call_id,
        metadata=await call_metadata(deps, org_id, call_id),
        after=0,
        types=TRANSCRIPT_TYPES,
        heartbeat_s=None,
    )
    items = tail.items()
    try:
        pending = asyncio.ensure_future(anext(items))
        await clock.wait_for_sleep(MAX_STREAM_S)  # replayed nothing; tailing without a heartbeat deadline
        # A latency event (seq 1) published but not yet stored or seen live, then a live turn (seq 2).
        latency = await publish_event(
            streaming, org_id, call_id, EventType.CALL_LATENCY, live=False, stored=False
        )
        turn = await _turn(streaming, org_id, call_id, "held")
        await clock.wait_for_sleep(GAP_HOLD_S)
        assert not pending.done()  # the turn waits for the filtered-out seq below it
        await streaming.event_publisher.broadcast(latency)
        async with asyncio.timeout(WAIT_S):
            item = await pending
        assert item is not None
        assert (item.seq, item.event_type, latency.seq) == (turn.seq, "call.turn", 1)
        await publish_event(streaming, org_id, call_id, EventType.CALL_COMPLETED)
        pending = asyncio.ensure_future(anext(items))
        await clock.wait_for_sleep(CLOSE_AFTER_COMPLETED_S)
        assert not pending.done()
        await clock.run_for(CLOSE_AFTER_COMPLETED_S)
        with pytest.raises(StopAsyncIteration):
            async with asyncio.timeout(WAIT_S):
                await pending
        assert tail.reason == "completed"
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await items.aclose()
        await tail.aclose()
    assert streaming.live_hub.subscribed_channels == frozenset()
