"""A protocol-neutral recording WebSocket server standing in for ElevenLabs in client tests.

Waits are bounded by a wall-clock timeout so a broken client fails instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Self
from urllib.parse import parse_qsl, urlsplit

import orjson
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

__all__ = ["WAIT_S", "Connection", "Message", "RecordingWsServer"]

WAIT_S: Final = 5.0

type Message = dict[str, Any]


@dataclass(eq=False)
class Connection:
    """One client connection to a :class:`RecordingWsServer`."""

    ws: ServerConnection
    path: str
    query: list[tuple[str, str]]
    headers: Headers
    messages: asyncio.Queue[Message] = field(default_factory=asyncio.Queue)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def next(self) -> Message:
        """The next message the client sent."""
        async with asyncio.timeout(WAIT_S):
            return await self.messages.get()

    async def take(self, count: int) -> list[Message]:
        return [await self.next() for _ in range(count)]

    async def until(self, predicate: Callable[[Message], bool]) -> list[Message]:
        """Messages up to and including the first that satisfies ``predicate``."""
        seen: list[Message] = []
        while True:
            message = await self.next()
            seen.append(message)
            if predicate(message):
                return seen

    def pending(self) -> list[Message]:
        """Messages already received and not yet read."""
        items: list[Message] = []
        while not self.messages.empty():
            items.append(self.messages.get_nowait())
        return items

    async def send(self, frame: Message | str) -> None:
        await self.ws.send(frame if isinstance(frame, str) else orjson.dumps(frame).decode())

    async def close(self, code: int = 1011, reason: str = "") -> None:
        await self.ws.close(code, reason)
        await self.wait_closed()

    async def wait_closed(self) -> None:
        async with asyncio.timeout(WAIT_S):
            await self.closed.wait()


class RecordingWsServer:
    """A WebSocket server on 127.0.0.1 recording every connection and message.

    ``reject_status`` answers the handshake with that HTTP status. Subclasses react through
    :meth:`on_connect` (before messages are read) and :meth:`on_message`.
    """

    def __init__(self) -> None:
        self.reject_status: int | None = None
        self.connections: list[Connection] = []
        self.handshakes = 0
        self._new: asyncio.Queue[Connection] = asyncio.Queue()
        self._server: Server | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            msg = "server not started"
            raise RuntimeError(msg)
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def __aenter__(self) -> Self:
        self._server = await serve(self._handler, "127.0.0.1", 0, process_request=self._process)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def next_connection(self) -> Connection:
        async with asyncio.timeout(WAIT_S):
            return await self._new.get()

    async def on_connect(self, conn: Connection) -> None:
        del conn

    async def on_message(self, conn: Connection, message: Message) -> None:
        del conn, message

    def _process(self, connection: ServerConnection, request: Request) -> Response | None:
        del request
        self.handshakes += 1
        if self.reject_status is not None:
            return connection.respond(self.reject_status, "rejected\n")
        return None

    async def _handler(self, ws: ServerConnection) -> None:
        split = urlsplit(ws.request.path if ws.request is not None else "")
        headers = ws.request.headers if ws.request is not None else Headers()
        conn = Connection(ws, split.path, parse_qsl(split.query), headers)
        self.connections.append(conn)
        try:
            await self.on_connect(conn)
            self._new.put_nowait(conn)
            async for raw in ws:
                message: Message = orjson.loads(raw)
                conn.messages.put_nowait(message)
                await self.on_message(conn, message)
        except ConnectionClosed:
            pass
        finally:
            conn.closed.set()
