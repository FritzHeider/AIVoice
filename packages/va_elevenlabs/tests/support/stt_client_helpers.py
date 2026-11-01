"""va_elevenlabs realtime STT client test helpers: a recording server speaking the realtime protocol, server
frame builders, event collection, and a rig with a manual clock, metrics, and spans.

Waits are bounded by a wall-clock timeout so a broken client fails instead of hanging; time inside the client
moves only through the injected clock.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from rig_helpers import Observed
from ws_server_helpers import WAIT_S, Connection, Message, RecordingWsServer

from va_elevenlabs.stt_metrics import CatalogSttMetrics
from va_elevenlabs.stt_models import SttConfig, SttConnection, SttEvent
from va_elevenlabs.stt_realtime import ElevenLabsRealtimeStt

__all__ = [
    "API_KEY",
    "FRAME",
    "RecordingSttServer",
    "SttRig",
    "audio_of",
    "collect_events",
    "committed",
    "committed_words",
    "entities",
    "opened_stt",
    "partial",
    "server_error",
    "session_started",
    "stt_rig",
]

API_KEY: Final = "test-elevenlabs-key"
FRAME: Final = b"\x10" * 160
_RATES: Final = {"ulaw_8000": 8000, "pcm_16000": 16000}


def session_started(query: Sequence[tuple[str, str]], session_id: str, **config: Any) -> Message:
    """A ``session_started`` echoing ``query`` (``config`` overrides echoed fields)."""
    params = dict(query)
    audio_format = params.get("audio_format", "pcm_16000")
    echoed: dict[str, Any] = {
        "model_id": params.get("model_id"),
        "audio_format": audio_format,
        "sample_rate": _RATES.get(audio_format, 16000),
        "commit_strategy": params.get("commit_strategy", "manual"),
        "keyterms": [value for key, value in query if key == "keyterms"],
        **config,
    }
    return {"message_type": "session_started", "session_id": session_id, "config": echoed}


def partial(text: str) -> Message:
    return {"message_type": "partial_transcript", "text": text}


def committed(text: str) -> Message:
    return {"message_type": "committed_transcript", "text": text}


def committed_words(text: str, language: str | None = "en") -> Message:
    """A timestamped transcript: one ``word`` per token (0.1 s each), ``spacing`` between."""
    words: list[dict[str, Any]] = []
    for index, token in enumerate(text.split()):
        if index:
            words.append({"text": " ", "start": index * 0.1, "end": index * 0.1, "type": "spacing"})
        words.append(
            {"text": token, "start": index * 0.1, "end": index * 0.1 + 0.08, "type": "word", "logprob": -0.1}
        )
    return {
        "message_type": "committed_transcript_with_timestamps",
        "text": text,
        "language_code": language,
        "words": words,
    }


def entities(text: str, found: Sequence[dict[str, Any]]) -> Message:
    return {"message_type": "committed_transcript_entities", "text": text, "entities": list(found)}


def server_error(message_type: str, error: str = "") -> Message:
    return {"message_type": message_type, "error": error or f"test {message_type}"}


def audio_of(message: Message) -> bytes:
    return base64.b64decode(message["audio_base_64"])


class RecordingSttServer(RecordingWsServer):
    """A :class:`RecordingWsServer` that greets each connection with ``session_started`` (ids ``sess-1``,
    ``sess-2``, ...) unless ``greetings`` holds a frame (or ``None`` for no greeting) for it."""

    def __init__(self) -> None:
        super().__init__()
        self.greetings: deque[Message | str | None] = deque()
        self.echo: dict[str, Any] = {}

    async def on_connect(self, conn: Connection) -> None:
        if self.greetings:
            greeting = self.greetings.popleft()
            if greeting is not None:
                await conn.send(greeting)
            return
        session_id = f"sess-{len(self.connections)}"
        await conn.send(session_started(conn.query, session_id, **self.echo))


async def collect_events(
    stt: ElevenLabsRealtimeStt, until: Callable[[list[SttEvent]], bool]
) -> list[SttEvent]:
    """Events until ``until(events)`` holds or the stream ends (wall-clock bounded)."""
    items: list[SttEvent] = []
    async with asyncio.timeout(WAIT_S * 2):
        async for event in stt.events():
            items.append(event)
            if until(items):
                return items
    return items


class SttRig(Observed):
    """An :class:`ElevenLabsRealtimeStt` on a :class:`ManualClock`, a media-worker registry, and spans."""

    def __init__(self, base: str, **conn: Any) -> None:
        super().__init__()
        connection = SttConnection(ws_base=base, api_key=conn.pop("api_key", API_KEY), **conn)
        self.stt = ElevenLabsRealtimeStt(
            connection,
            clock=self.clock,
            metrics=CatalogSttMetrics(self.service_metrics),
            tracer_provider=self.provider,
        )

    async def next_event(self) -> SttEvent:
        events = await collect_events(self.stt, bool)
        return events[0]


def stt_rig(base: str, **conn: Any) -> SttRig:
    return SttRig(base, **conn)


@asynccontextmanager
async def opened_stt(r: SttRig, server: RecordingSttServer, **cfg: Any) -> AsyncIterator[Connection]:
    """Open the rig's client against ``server``; yields the connection; closes the client afterwards."""
    await r.stt.open(SttConfig(**cfg))
    conn = await server.next_connection()
    try:
        yield conn
    finally:
        await r.stt.close()
