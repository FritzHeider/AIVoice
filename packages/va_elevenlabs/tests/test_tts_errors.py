"""Classification of server error frames and closes (Decision P03-T10 rule 7)."""

from __future__ import annotations

import pytest

from va_elevenlabs.tts_errors import (
    TtsAuthError,
    TtsError,
    TtsProtocolError,
    TtsServerError,
    TtsUnavailableError,
    TtsVoiceNotFoundError,
    classify_close,
    classify_error,
    is_fatal,
)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("voice_not_found", TtsVoiceNotFoundError),
        ("invalid_api_key", TtsAuthError),
        ("needs_authorization", TtsAuthError),
        ("context_closed", TtsProtocolError),
        ("invalid_message", TtsProtocolError),
        ("max_active_conversations", TtsProtocolError),
        ("quota_exceeded", TtsServerError),
        ("internal_error", TtsServerError),
        ("something_new", TtsServerError),
    ],
)
def test_error_frames(error: str, kind: type[TtsError]) -> None:
    classified = classify_error(error, "details")
    assert type(classified) is kind
    assert (classified.code, classified.message) == (error, "details")
    assert is_fatal(classified) is (kind is not TtsServerError)


@pytest.mark.parametrize(
    ("code", "reason", "kind"),
    [
        (1008, "Invalid API key", TtsAuthError),
        (1008, "voice_not_found", TtsVoiceNotFoundError),
        (1011, "voice_not_found", TtsVoiceNotFoundError),
        (1008, "invalid output_format: unsupported", TtsProtocolError),
        (1008, "invalid message", TtsProtocolError),
        (1008, "Have not received a new text input within the timeout of 20 seconds.", None),
        (1008, "quota exceeded", None),
        (1011, "simulated disconnect", None),
        (1012, "simulator reset", None),
        (1006, "", None),
        (1000, "", None),
        (None, "", None),
    ],
)
def test_closes(code: int | None, reason: str, kind: type[TtsError] | None) -> None:
    classified = classify_close(code, reason)
    assert (type(classified) if classified is not None else None) is kind


def test_messages_and_codes() -> None:
    error = TtsUnavailableError("unavailable", "3 reconnect attempts failed")
    assert str(error) == "unavailable: 3 reconnect attempts failed"
    assert is_fatal(error)
    assert str(TtsError("client_error", "")) == "client_error"
