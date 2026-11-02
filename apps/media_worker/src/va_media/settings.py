"""Media worker settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``media``)."""

from __future__ import annotations

import socket
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from va_common.settings import (
    CryptoSettings,
    DatabaseSettings,
    RedisSettings,
    StreamTokenSettings,
    TelnyxPublicKeySettings,
    TemporalSettings,
)
from va_common.stream_tokens import media_ws_scheme
from va_elevenlabs.settings import ElevenLabsSettings
from va_llm.settings import LlmSettings
from va_voice.audio import L16ByteOrder

__all__ = ["MediaSettings"]


class MediaSettings(
    RedisSettings,
    DatabaseSettings,
    TemporalSettings,
    StreamTokenSettings,
    TelnyxPublicKeySettings,
    CryptoSettings,
    ElevenLabsSettings,
    LlmSettings,
):
    """Admission, drain, media WebSocket authentication, vendor, and LLM settings of a media-worker pod.

    ``STREAM_TOKEN_SECRET`` and ``TELNYX_PUBLIC_KEY`` may be empty at construction so the module imports
    without environment; the app lifespan refuses to start without them (specs/07 §3 Decision P05-T07a).
    ``LOCAL_MASTER_KEY`` (local KMS provider) is needed once the call runtime opens (org LLM provider keys).
    """

    service_name: ClassVar[str] = "media-worker"

    media_drain_timeout_s: float = Field(default=2700, gt=0)
    media_max_calls_per_pod: int = Field(default=60, ge=1)
    # Silero VAD inference threads per process (specs/07-media-pipeline.md §5.3).
    media_vad_threads: int = Field(default=2, ge=1)
    # The host Telnyx opens media WebSockets to; the signed stream URL is rebuilt with it (specs/06 §4.2).
    media_public_host: str = "localhost:8200"
    # Byte order of L16 payloads on the Telnyx media WS (hd_audio legs, specs/07 §15).
    telnyx_l16_byte_order: L16ByteOrder = "big"
    # Empty or unset → the hostname (the pod name under Kubernetes).
    pod_id: str = ""

    @field_validator("pod_id", mode="after")
    @classmethod
    def _default_pod_id(cls, value: str) -> str:
        return value.strip() or socket.gethostname()

    @property
    def media_ws_scheme(self) -> Literal["ws", "wss"]:
        """``ws`` in ``local``/``test``, ``wss`` elsewhere (the scheme the worker put into ``stream_url``)."""
        return media_ws_scheme(self.va_env)
