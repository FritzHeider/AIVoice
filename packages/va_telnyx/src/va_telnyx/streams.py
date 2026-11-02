"""Media stream and recording parameters shared by the outbound dial and the inbound answer (specs/06 §6
step 5, §7 step 9; Decision P05-T12c).

Pure: the caller supplies the media worker's public host, the ``ws``/``wss`` scheme, and a minted stream
token.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from urllib.parse import urlencode

from va_telnyx.models import StreamParams

__all__ = ["RECORDING_FIELDS", "media_stream_url", "stream_params"]

RECORDING_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {"record": "record-from-answer", "record_channels": "dual", "record_format": "wav"}
)
"""Dual-channel WAV recording from the answer (``dial`` adds ``record_track: both``; ``answer`` has no
track)."""


def media_stream_url(scheme: str, host: str, call_id: uuid.UUID, token: str) -> str:
    """``{scheme}://{host}/ws/telnyx?call_id=…&token=…`` (the URL the media worker verifies, specs/07 §3)."""
    query = urlencode({"call_id": str(call_id), "token": token})
    return f"{scheme}://{host}/ws/telnyx?{query}"


def stream_params(url: str, *, hd_audio: bool) -> StreamParams:
    """Bidirectional RTP on the inbound track: PCMU 8 kHz, or L16 16 kHz with ``hd_audio``."""
    if hd_audio:
        return StreamParams(
            stream_url=url,
            stream_codec="L16",
            stream_bidirectional_codec="L16",
            stream_bidirectional_sampling_rate=16000,
        )
    return StreamParams(stream_url=url)
