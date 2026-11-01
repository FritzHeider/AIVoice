"""VoiceAgents ElevenLabs client: the multi-context TTS WebSocket (specs/09-tts-elevenlabs.md) and the
realtime STT WebSocket (specs/08-stt-elevenlabs.md)."""

from va_elevenlabs.stt_errors import (
    SttAuthError,
    SttError,
    SttProtocolError,
    SttQuotaError,
    SttTermsError,
    SttUnavailableError,
)
from va_elevenlabs.stt_metrics import CatalogSttMetrics, NullSttMetrics, SttMetrics
from va_elevenlabs.stt_models import (
    StreamingStt,
    SttAudioFormat,
    SttConfig,
    SttConnection,
    SttEntity,
    SttEvent,
    SttWord,
)
from va_elevenlabs.stt_realtime import ElevenLabsRealtimeStt
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
    "CatalogSttMetrics",
    "CatalogTtsMetrics",
    "ChunkAlignment",
    "ElevenLabsRealtimeStt",
    "NullSttMetrics",
    "NullTtsMetrics",
    "OutputFormat",
    "StreamingStt",
    "StreamingTts",
    "SttAudioFormat",
    "SttAuthError",
    "SttConfig",
    "SttConnection",
    "SttEntity",
    "SttError",
    "SttEvent",
    "SttMetrics",
    "SttProtocolError",
    "SttQuotaError",
    "SttTermsError",
    "SttUnavailableError",
    "SttWord",
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
