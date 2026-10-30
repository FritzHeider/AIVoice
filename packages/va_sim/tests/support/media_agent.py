"""A minimal platform-side media WebSocket for FakeTelnyx media tests.

:class:`MediaAgent` serves one WebSocket on an ephemeral localhost port (``url``), records the handshake
request and every frame the simulator sends (parsed JSON, with the sim clock reading when it arrived), and can
send frames back, close the socket, or reject the handshake. ``wait_for`` waits on a condition notified per
frame (never sleeps or polls), guarded by a wall-clock timeout so a broken simulator fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from http import HTTPStatus
from types import TracebackType
from typing import Any, Final, Self

import orjson
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from va_sim.clock import SimClock

__all__ = ["WAIT_TIMEOUT_S", "MediaAgent"]

WAIT_TIMEOUT_S: Final = 10.0

type Frame = dict[str, Any]


class MediaAgent:
    """The platform end of a FakeTelnyx media stream."""

    def __init__(self, *, reject: bool = False, path: str = "/ws/telnyx?call_id=1") -> None:
        self.reject = reject
        self._path = path
        self.clock: SimClock | None = None
        self.url = ""
        self.request_path: str | None = None
        self.headers: Headers | None = None
        self.frames: list[Frame] = []
        self.arrivals: list[int] = []
        self.close_code: int | None = None
        self.closed = False
        self._changed = asyncio.Condition()
        self._server: Server | None = None
        self._connection: ServerConnection | None = None

    async def __aenter__(self) -> Self:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            compression=None,
            max_size=2**22,
            ping_interval=None,
        )
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}{self._path}"
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.request_path = request.path
        self.headers = request.headers
        if self.reject:
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        self._connection = connection
        with contextlib.suppress(ConnectionClosed):
            async for raw in connection:
                frame: Frame = orjson.loads(raw)
                async with self._changed:
                    self.frames.append(frame)
                    self.arrivals.append(self.clock.virtual_ms() if self.clock is not None else 0)
                    self._changed.notify_all()
        async with self._changed:
            self.closed = True
            self.close_code = connection.close_code
            self._changed.notify_all()

    # Inspection.

    def of(self, event: str) -> list[Frame]:
        return [frame for frame in self.frames if frame["event"] == event]

    def media(self) -> list[Frame]:
        return self.of("media")

    async def wait_for(self, predicate: Callable[[MediaAgent], bool]) -> None:
        """Wait until ``predicate(self)`` holds (checked after every frame and on close)."""
        async with asyncio.timeout(WAIT_TIMEOUT_S), self._changed:
            await self._changed.wait_for(lambda: predicate(self))

    async def wait_media(self, count: int) -> None:
        """Wait until at least ``count`` caller media frames have arrived."""
        await self.wait_for(lambda agent: len(agent.media()) >= count)

    async def wait_closed(self) -> None:
        await self.wait_for(lambda agent: agent.closed)

    # Sending.

    async def send(self, frame: Frame) -> None:
        await self.send_raw(orjson.dumps(frame).decode())

    async def send_raw(self, message: str | bytes) -> None:
        assert self._connection is not None, "the simulator has not connected"
        await self._connection.send(message)

    async def close(self, code: int = 1000) -> None:
        assert self._connection is not None, "the simulator has not connected"
        await self._connection.close(code)
