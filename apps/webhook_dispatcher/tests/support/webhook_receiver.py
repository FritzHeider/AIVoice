"""A real-socket webhook receiver for dispatcher tests (served by uvicorn on 127.0.0.1, port 0).

Requests are recorded with their raw body and headers. Replies are scripted per path (FIFO, then
``default``); a reply with ``hold=True`` waits until :meth:`Receiver.release` (always released at teardown).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from service_helpers import serving
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    hold: bool = False
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class Received:
    path: str
    host: str
    headers: dict[str, str]
    body: bytes


class Receiver:
    """Records requests; scripted replies per path; tracks in-flight requests per ``Host``."""

    def __init__(self, default: Reply | None = None) -> None:
        self.default = default if default is not None else Reply()
        self.scripts: dict[str, list[Reply]] = {}
        self.requests: list[Received] = []
        self.in_flight: Counter[str] = Counter()
        self.max_in_flight: Counter[str] = Counter()
        self.completed: Counter[str] = Counter()
        self.changed = asyncio.Event()
        self._released = asyncio.Event()

    def script(self, path: str, *replies: Reply) -> None:
        self.scripts.setdefault(path, []).extend(replies)

    def release(self) -> None:
        self._released.set()

    def paths(self) -> list[str]:
        return [request.path for request in self.requests]

    async def wait_for(self, predicate: Callable[[], bool], timeout_s: float = 10.0) -> None:
        """Wait (event-driven) until ``predicate()`` holds after a request arrived or finished."""
        async with asyncio.timeout(timeout_s):
            while not predicate():
                self.changed.clear()
                await self.changed.wait()

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        host = request.headers.get("host", "").split(":", 1)[0]
        self.requests.append(Received(request.url.path, host, dict(request.headers), body))
        queue = self.scripts.get(request.url.path)
        reply = queue.pop(0) if queue else self.default
        self.in_flight[host] += 1
        self.max_in_flight[host] = max(self.max_in_flight[host], self.in_flight[host])
        self.changed.set()
        try:
            if reply.hold:
                await self._released.wait()
            return Response(reply.body, status_code=reply.status, headers=reply.headers)
        finally:
            self.in_flight[host] -= 1
            self.completed[host] += 1
            self.changed.set()


@asynccontextmanager
async def served_receiver(receiver: Receiver) -> AsyncIterator[int]:
    """Serve ``receiver`` on 127.0.0.1; yields the port (held replies are released before shutdown)."""
    app = Starlette(routes=[Route("/{path:path}", receiver.handle, methods=["POST", "GET"])])
    async with serving(app) as server:
        try:
            yield int(server.url.rsplit(":", 1)[1])
        finally:
            receiver.release()


def hex_hmac(secret: str, message: bytes) -> str:
    """Independent HMAC-SHA256 hex digest for signature assertions."""
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_ok(received: Received, secret: str) -> bool:
    """Whether both signature headers of ``received`` verify with ``secret`` over the raw body."""
    parts = dict(item.split("=", 1) for item in received.headers["x-va-signature"].split(",")[:2])
    signed = f"{parts['t']}.".encode() + received.body
    return (
        parts["t"] == received.headers["x-va-timestamp"]
        and hmac.compare_digest(parts["v1"], hex_hmac(secret, signed))
        and hmac.compare_digest(received.headers["x-webhook-signature"], hex_hmac(secret, received.body))
    )
