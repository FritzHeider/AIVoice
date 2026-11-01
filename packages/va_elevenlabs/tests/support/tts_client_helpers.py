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
from typing import Any, Final

from rig_helpers import Observed
from ws_server_helpers import WAIT_S, Connection, Message, RecordingWsServer

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


class RecordingTtsServer(RecordingWsServer):
    """A :class:`RecordingWsServer` for the TTS protocol: by default a ``close_context`` is answered with
    ``isFinal`` and ``close_socket`` with a normal close (``auto_final`` / ``auto_close``)."""

    def __init__(self, *, auto_final: bool = True, auto_close: bool = True) -> None:
        super().__init__()
        self.auto_final = auto_final
        self.auto_close = auto_close

    async def on_message(self, conn: Connection, message: Message) -> None:
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


class Rig(Observed):
    """A :class:`TtsSocket` on a :class:`ManualClock`, a media-worker registry, and an in-memory exporter."""

    def __init__(self, base: str, **config: Any) -> None:
        super().__init__()
        metrics = CatalogTtsMetrics(self.service_metrics)
        self.tts = TtsSocket(
            config_for(base, **config), clock=self.clock, metrics=metrics, tracer_provider=self.provider
        )


def rig(base: str, **config: Any) -> Rig:
    return Rig(base, **config)
