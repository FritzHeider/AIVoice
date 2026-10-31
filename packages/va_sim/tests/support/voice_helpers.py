"""FakeElevenLabs voices test helpers: sample files and the add-voice multipart request.

Clients come from ``tts_helpers.rest_client`` (in-process, ``xi-api-key`` header).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

import httpx

from va_sim.elevenlabs.mp3 import FRAME_SAMPLES, MP3_SAMPLE_RATE, silent_frames
from va_sim.wav import pcm16_wav

__all__ = ["ADD", "V2", "add_voice", "id3_tag", "mp3_sample", "voice_files", "wav_sample"]

ADD: Final = "/v1/voices/add"
V2: Final = "/v2/voices"

type Upload = tuple[str, tuple[str, bytes, str]]


def wav_sample(seconds: float, rate: int = 16_000) -> bytes:
    """A silent mono PCM16 WAV of ``seconds`` (rounded to whole samples)."""
    return pcm16_wav([bytes(round(seconds * rate) * 2)], rate)


def id3_tag(payload_size: int, *, footer: bool = False) -> bytes:
    """An ID3v2.4 tag whose syncsafe size field says ``payload_size`` (zero-filled payload)."""
    size = bytes((payload_size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    flags = 0x10 if footer else 0x00
    body = b"ID3\x04\x00" + bytes((flags,)) + size + bytes(payload_size)
    return body + (b"3DI\x04\x00" + bytes((flags,)) + size if footer else b"")


def mp3_sample(seconds: float, *, tag: bytes = b"") -> bytes:
    """Silent MPEG-1 Layer III frames covering at least ``seconds``, after an optional ID3 ``tag``."""
    return tag + silent_frames(0, math.ceil(seconds * MP3_SAMPLE_RATE / FRAME_SAMPLES))


def voice_files(*samples: tuple[str, bytes]) -> list[Upload]:
    return [("files", (name, data, "application/octet-stream")) for name, data in samples]


async def add_voice(
    client: httpx.AsyncClient,
    name: str | None = "My clone",
    *,
    files: Sequence[tuple[str, bytes]] = (("sample.wav", wav_sample(1.5)),),
    path: str = ADD,
    **fields: str,
) -> httpx.Response:
    """``POST /v1/voices/add`` (or an edit ``path``) with ``name`` (omitted when ``None``), ``files``, and
    extra form ``fields``. Text fields go out as file-name-less parts, so the body is multipart even without
    files."""
    data = {**({} if name is None else {"name": name}), **fields}
    parts: list[tuple[str, tuple[str | None, bytes | str] | tuple[str, bytes, str]]] = [
        (key, (None, value)) for key, value in data.items()
    ]
    parts.extend(voice_files(*files))
    return await client.post(path, files=parts)
