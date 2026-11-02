"""Pure mapping of a call's resolved configuration onto the pipeline and the vendor sockets
(specs/07-media-pipeline.md §16 Decision P05-T10a).

- :func:`pipeline_config`: the :class:`PipelineConfig` of a leg from the ``CallContext`` and the org settings
  (``max_duration_s = max_duration_min x 60``, the warning 10 s before it — less on calls shorter than 20 s —,
  the org's ``max_duration_message``/``no_input_goodbye``, the call's interruption, DTMF and no-input
  settings, ``noise_cancellation`` (the spectral gate on the VAD path) and pronunciation guide).
- :func:`stt_config`: the realtime STT session (``ulaw_8000`` on PCMU legs, ``pcm_16000`` on 16 kHz legs;
  the specs/08 §5 language row fixes ``language_code`` or turns detection on; ``keywords`` become keyterms;
  ``noise_cancellation`` sets Scribe's ``filter_background_audio``, specs/07 §10.1 Decision P05-T15).
- :func:`tts_voice` / :func:`tts_output_format`: the TTS socket's voice and output format.
- :func:`zero_retention`: vendor zero retention for the org (``privacy.zero_retention`` or HIPAA mode) when
  the platform account supports it.
- :func:`session_end`: the :class:`~va_media.session.SessionEnd` of a pipeline result (specs/05 §9 end
  reasons).
"""

from __future__ import annotations

from typing import Final, cast

from va_common.logging import get_logger
from va_db.call_states import END_REASONS, EndReason
from va_domain.call_context import CallContext
from va_domain.languages import lookup_language
from va_domain.org import OrgSettings
from va_elevenlabs.keyterms import merge_keyterms
from va_elevenlabs.stt_models import SAMPLE_RATES, SttAudioFormat, SttConfig
from va_elevenlabs.tts_models import OutputFormat, TtsVoice
from va_media.session import STREAM_CLOSED, SessionEnd
from va_voice.audio import ULAW_8000, AudioFormat, L16ByteOrder
from va_voice.pipeline import PipelineConfig, PipelineResult

__all__ = [
    "AGENT_END_CALL",
    "ERROR_END",
    "MAX_DURATION_WARNING_S",
    "pipeline_config",
    "session_end",
    "stt_audio_format",
    "stt_config",
    "tts_output_format",
    "tts_voice",
    "zero_retention",
]

AGENT_END_CALL: Final = "agent_end_call"
ERROR_END: Final = SessionEnd("ended", end_reason="error", ended_by="error")
MAX_DURATION_WARNING_S: Final = 10.0
"""The ``max_duration_message`` is spoken this long before ``max_duration`` (specs/07 §18)."""

_SECONDS_PER_MINUTE: Final = 60.0
_TRANSPORT_CLOSED: Final = "transport_closed"
_PIPELINE_HANGUP: Final = "agent_hangup"

_log = get_logger(__name__)


def pipeline_config(
    context: CallContext, org: OrgSettings, *, fmt: AudioFormat, l16_byte_order: L16ByteOrder
) -> PipelineConfig:
    """The leg's pipeline settings; ``ValueError`` when a value is outside the pipeline's bounds."""
    config = context.config
    max_duration_s = config.max_duration_min * _SECONDS_PER_MINUTE
    return PipelineConfig(
        fmt=fmt,
        l16_byte_order=l16_byte_order,
        interruptibility=config.interruptibility,
        block_interruptions=config.block_interruptions,
        interruption_threshold_ms=config.interruption_threshold_ms,
        ivr_mode=config.ivr_mode,
        wait_for_greeting=config.wait_for_greeting,
        greeting_timeout_ms=config.greeting_timeout_ms,
        max_duration_s=max_duration_s,
        max_duration_message=org.max_duration_message,
        max_duration_warning_s=min(MAX_DURATION_WARNING_S, max_duration_s / 2),
        no_input_timeout_ms=config.no_input_timeout_ms,
        max_reprompts=config.max_reprompts,
        no_input_goodbye=org.no_input_goodbye,
        ignore_button_press=config.ignore_button_press,
        noise_cancellation=config.noise_cancellation,
        pronunciation=tuple(config.pronunciation_guide),
    )


def stt_audio_format(fmt: AudioFormat) -> SttAudioFormat:
    """``ulaw_8000`` on PCMU legs; 16 kHz legs send little-endian PCM16 (specs/07 §16 Decision P04-T12a)."""
    return "ulaw_8000" if fmt == ULAW_8000 else "pcm_16000"


def stt_config(context: CallContext, *, fmt: AudioFormat) -> SttConfig:
    """The realtime STT session of the call; an unknown ``language`` falls back to detection."""
    audio_format = stt_audio_format(fmt)
    mapping = lookup_language(context.config.language)
    language_code = mapping.scribe_code if mapping is not None else None
    keyterms = merge_keyterms([context.config.keywords])
    return SttConfig(
        audio_format=audio_format,
        sample_rate=SAMPLE_RATES[audio_format],
        language_code=language_code,
        include_language_detection=language_code is None,
        keyterms=list(keyterms.terms),
        filter_background_audio=context.config.noise_cancellation,
    )


def tts_output_format(fmt: AudioFormat) -> OutputFormat:
    """``ulaw_8000`` on PCMU legs, ``pcm_16000`` on 16 kHz legs (``PipelineConfig.tts_format``)."""
    return "ulaw_8000" if fmt == ULAW_8000 else "pcm_16000"


def tts_voice(context: CallContext, *, model_id: str) -> TtsVoice:
    """The call's voice; a fixed language row pins ``language_code`` (detection leaves it to the model)."""
    config = context.config
    mapping = lookup_language(config.language)
    return TtsVoice(
        provider_voice_id=config.provider_voice_id,
        model_id=model_id,
        settings=config.voice_settings,
        language_code=mapping.tts_code if mapping is not None else None,
    )


def zero_retention(org: OrgSettings, *, available: bool) -> bool:
    """Whether the vendor sockets send ``enable_logging=false``; a wanted but unavailable option is logged."""
    wanted = org.privacy.zero_retention or org.hipaa_mode
    if wanted and not available:
        _log.warning("media.zero_retention_unavailable")
    return wanted and available


def session_end(result: PipelineResult) -> SessionEnd:
    """``transport_closed`` → :data:`STREAM_CLOSED`; the pipeline's generic hangup → ``agent_end_call``; any
    specs/05 §9 reason → that reason and its ``ended_by``; anything else → :data:`ERROR_END` (logged)."""
    reason = result.end_reason
    if reason == _TRANSPORT_CLOSED:
        return STREAM_CLOSED
    if reason == _PIPELINE_HANGUP:
        reason = AGENT_END_CALL
    if reason not in END_REASONS:
        _log.error("media.unknown_end_reason", end_reason=reason)
        return ERROR_END
    known = cast("EndReason", reason)
    return SessionEnd("ended", end_reason=known, ended_by=END_REASONS[known])
