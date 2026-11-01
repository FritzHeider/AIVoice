"""REST synthesis for the phrase cache and previews (specs/09-tts-elevenlabs.md §9, specs/15 §3.6; Decision
2026-11-01 P03-T12 rules 2-3).

``POST /v1/text-to-speech/{voice_id}`` returns the whole audio; ``.../with-timestamps`` returns
``audio_base64`` with absolute alignments. Both time out after 10 s and retry once after 200 ms: callers fall
back to the live socket on failure (specs/09 §9).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Final, Literal

from pydantic import ValidationError

from va_domain.voices import VoiceSettings
from va_elevenlabs.rest_errors import ElevenLabsProtocolError
from va_elevenlabs.rest_models import RestAlignment, TtsRestAudio, TtsTimestamped, optional_int
from va_elevenlabs.rest_retry import TTS_RETRY
from va_elevenlabs.rest_transport import ElevenLabsTransport, RestOperation, RestResponse, segment

__all__ = ["TTS_CONVERT", "TTS_WITH_TIMESTAMPS", "TextNormalization", "TtsRestCommands"]

type TextNormalization = Literal["auto", "on", "off"]

TTS_CONVERT: Final = RestOperation("tts_convert", "POST", 10.0, TTS_RETRY)
TTS_WITH_TIMESTAMPS: Final = RestOperation("tts_with_timestamps", "POST", 10.0, TTS_RETRY)
_AUDIO_ACCEPT: Final = "audio/*"
_DEFAULT_AUDIO_TYPE: Final = "application/octet-stream"


def _body(
    text: str,
    model_id: str,
    voice_settings: VoiceSettings | None,
    language_code: str | None,
    normalization: TextNormalization,
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text, "model_id": model_id, "apply_text_normalization": normalization}
    if voice_settings is not None:
        body["voice_settings"] = voice_settings.model_dump(mode="json")
    if language_code is not None:
        body["language_code"] = language_code
    return body


def _alignment(document: dict[str, Any], key: str) -> RestAlignment | None:
    raw = document.get(key)
    if raw is None:
        return None
    try:
        return RestAlignment.model_validate(raw)
    except ValidationError as exc:
        raise ElevenLabsProtocolError(TTS_WITH_TIMESTAMPS.name, f"{key}: {exc.errors()[0]['msg']}") from exc


def _audio(response: RestResponse) -> TtsRestAudio:
    return TtsRestAudio(
        audio=response.content,
        content_type=response.headers.get("content-type", _DEFAULT_AUDIO_TYPE),
        character_count=optional_int(response.headers.get("x-character-count")),
        request_id=response.headers.get("request-id"),
    )


def _timestamped(response: RestResponse) -> TtsTimestamped:
    document = response.json_object(TTS_WITH_TIMESTAMPS)
    encoded = document.get("audio_base64")
    if not isinstance(encoded, str):
        raise ElevenLabsProtocolError(TTS_WITH_TIMESTAMPS.name, "audio_base64 is missing")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ElevenLabsProtocolError(TTS_WITH_TIMESTAMPS.name, "audio_base64 is not base64") from exc
    return TtsTimestamped(
        audio=audio,
        alignment=_alignment(document, "alignment"),
        normalized_alignment=_alignment(document, "normalized_alignment"),
        character_count=optional_int(response.headers.get("x-character-count")),
        request_id=response.headers.get("request-id"),
    )


class TtsRestCommands:
    """Text-to-speech REST operations of :class:`va_elevenlabs.rest.ElevenLabsRest`."""

    _transport: ElevenLabsTransport

    async def tts_convert(  # noqa: PLR0913 - the vendor request fields, keyword-only
        self,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        output_format: str,
        voice_settings: VoiceSettings | None = None,
        language_code: str | None = None,
        apply_text_normalization: TextNormalization = "auto",
    ) -> TtsRestAudio:
        """The whole audio of ``text`` in ``output_format`` (e.g. ``ulaw_8000``, ``mp3_44100_128``)."""
        return await self._transport.call(
            TTS_CONVERT,
            f"/v1/text-to-speech/{segment(voice_id)}",
            _audio,
            json=_body(text, model_id, voice_settings, language_code, apply_text_normalization),
            params={"output_format": output_format},
            voice_id=voice_id,
            accept=_AUDIO_ACCEPT,
        )

    async def tts_with_timestamps(  # noqa: PLR0913 - the vendor request fields, keyword-only
        self,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        output_format: str,
        voice_settings: VoiceSettings | None = None,
        language_code: str | None = None,
        apply_text_normalization: TextNormalization = "auto",
    ) -> TtsTimestamped:
        """The audio of ``text`` with its character alignments (absolute seconds)."""
        return await self._transport.call(
            TTS_WITH_TIMESTAMPS,
            f"/v1/text-to-speech/{segment(voice_id)}/with-timestamps",
            _timestamped,
            json=_body(text, model_id, voice_settings, language_code, apply_text_normalization),
            params={"output_format": output_format},
            voice_id=voice_id,
        )
