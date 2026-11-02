"""``StarletteSocket``: messages, peer close, idempotent close."""

from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
from typing import Any

import pytest
from starlette.websockets import WebSocket

from va_media.socket import SocketClosedError, StarletteSocket


class Peer:
    """ASGI receive/send pair: ``incoming`` messages are delivered in order; sent messages are recorded."""

    def __init__(self, *incoming: dict[str, Any]) -> None:
        self.incoming = deque([{"type": "websocket.connect"}, *incoming])
        self.sent: list[MutableMapping[str, Any]] = []
        self.fail_send = False

    async def receive(self) -> dict[str, Any]:
        return self.incoming.popleft()

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if self.fail_send and message["type"] == "websocket.close":
            msg = "peer vanished"
            raise OSError(msg)
        self.sent.append(message)


async def accepted(peer: Peer) -> StarletteSocket:
    websocket = WebSocket({"type": "websocket", "path": "/ws/telnyx", "headers": []}, peer.receive, peer.send)
    await websocket.accept()
    return StarletteSocket(websocket)


async def test_text_and_binary_messages_then_peer_close() -> None:
    peer = Peer(
        {"type": "websocket.receive", "text": "hello"},
        {"type": "websocket.receive", "bytes": b"\x01\x02"},
        {"type": "websocket.disconnect", "code": 1001},
    )
    socket = await accepted(peer)
    assert await socket.receive() == "hello"
    assert await socket.receive() == b"\x01\x02"
    with pytest.raises(SocketClosedError) as caught:
        await socket.receive()
    assert caught.value.code == 1001
    with pytest.raises(SocketClosedError):
        await socket.receive()
    await socket.close(1000)
    assert [m["type"] for m in peer.sent] == ["websocket.accept"], "no close after the peer closed"


async def test_close_once_and_tolerates_a_vanished_peer() -> None:
    peer = Peer()
    socket = await accepted(peer)
    await socket.close(4409)
    await socket.close(1000)
    assert [m.get("code") for m in peer.sent if m["type"] == "websocket.close"] == [4409]
    assert socket.close_code == 4409

    vanished = Peer()
    socket = await accepted(vanished)
    vanished.fail_send = True
    await socket.close(1000)
    assert socket.close_code == 1000
