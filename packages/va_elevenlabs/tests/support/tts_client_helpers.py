"""va_elevenlabs TTS client test helpers: a recording WebSocket server standing in for ElevenLabs, server
frame builders, audio collection, and a rig with a manual clock, metrics, and spans.

Waits are bounded by a wall-clock timeout so a broken client fails instead of hanging; time inside the client
moves only through the injected clock.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Self
from urllib.parse import parse_qsl, urlsplit

import orjson
from clock_helpers import ManualClock
from obs_helpers import memory_provider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from va_common.metrics import Service, build_metrics, new_registry
from va_elevenlabs.tts_metrics import CatalogTtsMetrics
from va_elevenlabs.tts_models import TtsAudio, TtsConfig, TtsVoice
from va_elevenlabs.tts_ws import TtsSocket

__all__ = [
    "API_KEY",
    "WAIT_S",
    "Connection",
    "RecordingTtsServer",
    "Rig",
    "audio_frame",
    "collect",
    "config_for",
    "final_frame",
    "opened",
    "rig",
    "voice",
]

API_KEY: Final = "test-elevenlabs-key"
WAIT_S: Final = 5.0

type Message = dict[str, Any]


def voice(voice_id: str = "voice123", **fields: Any) -> TtsVoice:
    return TtsVoice(provider_voice_id=voice_id, **fields)


def config_for(base: str, **fields: Any) -> TtsConfig:
    """A config for ``base`` (``ws://``) with the test key."""
    return TtsConfig(ws_base=base, api_key=fields.pop("api_key", API_KEY), **fields)


def audio_frame(wire_id: str, text: str | None, *, audio: bytes = b"\xff" * 160) -> Message:
    """An audio frame whose alignment spells ``text`` (``None``: ``alignment: null``)."""
    alignment = None
    if text is not None:
        alignment = {
            "chars": list(text),
            "charStartTimesMs": [index * 10 for index in range(len(text))],
            "charDurationsMs": [10] * len(text),
        }
    return {
        "audio": base64.b64encode(audio).decode(),
        "contextId": wire_id,
        "alignment": alignment,
        "normalizedAlignment": alignment,
    }


def final_frame(wire_id: str) -> Message:
    return {"isFinal": True, "contextId": wire_id}


@dataclass(eq=False)
class Connection:
    """One client connection to the :class:`RecordingTtsServer`."""

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
        async with asyncio.timeout(WAIT_S):
            await self.closed.wait()


class RecordingTtsServer:
    """A WebSocket server on 127.0.0.1 recording every connection and message.

    ``reject_status`` answers the handshake with that HTTP status. By default a ``close_context`` is answered
    with ``isFinal`` and ``close_socket`` with a normal close (``auto_final`` / ``auto_close``).
    """

    def __init__(self, *, auto_final: bool = True, auto_close: bool = True) -> None:
        self.auto_final = auto_final
        self.auto_close = auto_close
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
        self._new.put_nowait(conn)
        try:
            async for raw in ws:
                message: Message = orjson.loads(raw)
                conn.messages.put_nowait(message)
                await self._auto(conn, message)
        except ConnectionClosed:
            pass
        finally:
            conn.closed.set()

    async def _auto(self, conn: Connection, message: Message) -> None:
        if self.auto_final and message.get("close_context") is True:
            await conn.send(final_frame(message["context_id"]))
        if self.auto_close and message.get("close_socket") is True:
            await conn.ws.close(1000)


async def collect(tts: TtsSocket, until: Callable[[list[TtsAudio]], bool]) -> list[TtsAudio]:
    """Items from ``tts.audio()`` until ``until(items)`` holds (wall-clock bounded)."""
    items: list[TtsAudio] = []
    async with asyncio.timeout(WAIT_S * 2):
        async for item in tts.audio():
            items.append(item)
            if until(items):
                return items
    return items


@asynccontextmanager
async def opened(
    tts: TtsSocket, server: RecordingTtsServer, **voice_fields: Any
) -> AsyncIterator[Connection]:
    """Open ``tts`` against ``server``; yields the connection after reading the ``ka`` init."""
    await tts.open("ulaw_8000", voice(**voice_fields))
    conn = await server.next_connection()
    assert await conn.next() == {"text": " ", "context_id": "ka"}
    try:
        yield conn
    finally:
        await tts.close()


@dataclass(eq=False)
class Rig:
    """A :class:`TtsSocket` on a :class:`ManualClock`, a media-worker registry, and an in-memory exporter."""

    tts: TtsSocket
    clock: ManualClock
    registry: CollectorRegistry
    exporter: InMemorySpanExporter

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    def spans(self, name: str) -> list[Any]:
        return [span for span in self.exporter.get_finished_spans() if span.name == name]


def rig(base: str, **config: Any) -> Rig:
    clock = ManualClock()
    registry = new_registry(process_collectors=False)
    metrics = CatalogTtsMetrics(build_metrics(Service.MEDIA_WORKER, registry))
    provider, exporter = memory_provider()
    tts = TtsSocket(config_for(base, **config), clock=clock, metrics=metrics, tracer_provider=provider)
    return Rig(tts, clock, registry, exporter)
