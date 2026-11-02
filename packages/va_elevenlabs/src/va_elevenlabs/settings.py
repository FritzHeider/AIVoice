"""ElevenLabs settings (specs/02-repo-layout-and-conventions.md §6 rows ``ELEVENLABS_*``).

:class:`ElevenLabsSettings` is mixed into the settings of every service that opens the realtime STT/TTS
sockets (media-worker); :class:`ElevenLabsRestSettings` into the services that call the REST API (the worker
renders the opener and fillers into the phrase cache, specs/07 §11 Decision P05-T10d). Both share the platform
key and the TTS model, so phrases rendered over REST match the socket's voice. Defaults are the
``.env.example`` dev values (the simulator on port 9200).
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from va_common.settings import BaseServiceSettings
from va_elevenlabs.stt_models import DEFAULT_STT_MODEL, KeytermsEncoding
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL

__all__ = ["ElevenLabsAccountSettings", "ElevenLabsRestSettings", "ElevenLabsSettings"]


class ElevenLabsAccountSettings(BaseServiceSettings):
    """The platform key and the TTS model."""

    elevenlabs_api_key: SecretStr = SecretStr("sim-eleven-key")
    elevenlabs_tts_model: str = DEFAULT_TTS_MODEL


class ElevenLabsSettings(ElevenLabsAccountSettings):
    """The WebSocket base, the realtime models, and the zero-retention capability."""

    elevenlabs_ws_base: str = "ws://localhost:9200"
    elevenlabs_stt_model: str = DEFAULT_STT_MODEL
    # The platform account supports ``enable_logging=false`` (required for HIPAA mode).
    elevenlabs_zero_retention_available: bool = False
    elevenlabs_stt_session_rotate_s: float = Field(default=1500, gt=0)
    elevenlabs_stt_keyterms_encoding: KeytermsEncoding = "repeated"


class ElevenLabsRestSettings(ElevenLabsAccountSettings):
    """The REST API base."""

    elevenlabs_api_base: str = "http://localhost:9200"
