"""VoiceAgents ElevenLabs client: the multi-context TTS WebSocket (specs/09-tts-elevenlabs.md)."""

from va_elevenlabs.tts_errors import (
    TtsAuthError,
    TtsError,
    TtsProtocolError,
    TtsServerError,
    TtsUnavailableError,
    TtsVoiceNotFoundError,
)
from va_elevenlabs.tts_metrics import CatalogTtsMetrics, NullTtsMetrics, TtsMetrics
from va_elevenlabs.tts_models import ChunkAlignment, OutputFormat, StreamingTts, TtsAudio, TtsConfig, TtsVoice
from va_elevenlabs.tts_ws import TtsSocket

__all__ = [
    "CatalogTtsMetrics",
    "ChunkAlignment",
    "NullTtsMetrics",
    "OutputFormat",
    "StreamingTts",
    "TtsAudio",
    "TtsAuthError",
    "TtsConfig",
    "TtsError",
    "TtsMetrics",
    "TtsProtocolError",
    "TtsServerError",
    "TtsSocket",
    "TtsUnavailableError",
    "TtsVoice",
    "TtsVoiceNotFoundError",
]
