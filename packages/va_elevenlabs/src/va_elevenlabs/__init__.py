"""VoiceAgents ElevenLabs client: the multi-context TTS WebSocket (specs/09-tts-elevenlabs.md), the
realtime STT WebSocket (specs/08-stt-elevenlabs.md), and the REST client (specs/15-voices.md)."""

from va_elevenlabs.rest import ElevenLabsRest
from va_elevenlabs.rest_errors import (
    ElevenLabsApiError,
    ElevenLabsAuthError,
    ElevenLabsError,
    ElevenLabsNotFoundError,
    ElevenLabsProtocolError,
    ElevenLabsRateLimitedError,
    ElevenLabsTransportError,
)
from va_elevenlabs.rest_metrics import ElevenLabsRestMetrics
from va_elevenlabs.rest_models import (
    AddedVoice,
    RestAlignment,
    TtsRestAudio,
    TtsTimestamped,
    VendorVoice,
    VoiceFile,
)
from va_elevenlabs.stt_batch import (
    AudioUpload,
    BatchOptions,
    BatchTranscript,
    BatchWord,
    SourceUrl,
    Utterance,
    group_utterances,
)
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
    "AddedVoice",
    "AudioUpload",
    "BatchOptions",
    "BatchTranscript",
    "BatchWord",
    "CatalogSttMetrics",
    "CatalogTtsMetrics",
    "ChunkAlignment",
    "ElevenLabsApiError",
    "ElevenLabsAuthError",
    "ElevenLabsError",
    "ElevenLabsNotFoundError",
    "ElevenLabsProtocolError",
    "ElevenLabsRateLimitedError",
    "ElevenLabsRealtimeStt",
    "ElevenLabsRest",
    "ElevenLabsRestMetrics",
    "ElevenLabsTransportError",
    "NullSttMetrics",
    "NullTtsMetrics",
    "OutputFormat",
    "RestAlignment",
    "SourceUrl",
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
    "TtsRestAudio",
    "TtsServerError",
    "TtsSocket",
    "TtsTimestamped",
    "TtsUnavailableError",
    "TtsVoice",
    "TtsVoiceNotFoundError",
    "Utterance",
    "VendorVoice",
    "VoiceFile",
    "group_utterances",
]
