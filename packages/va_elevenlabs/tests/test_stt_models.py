"""SttConfig consistency rules and SttEvent defaults (specs/08 §1; Decision P03-T11 rule 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from va_elevenlabs import SttConfig, SttConnection, SttEvent


def test_defaults_are_ulaw_with_detection() -> None:
    cfg = SttConfig()
    assert (cfg.audio_format, cfg.sample_rate, cfg.language_code, cfg.include_language_detection) == (
        "ulaw_8000",
        8000,
        None,
        True,
    )
    assert SttConfig(audio_format="pcm_16000", sample_rate=16000).sample_rate == 16000


@pytest.mark.parametrize(
    "fields",
    [
        {"audio_format": "pcm_16000"},
        {"sample_rate": 16000},
        {"language_code": "en"},
        {"include_language_detection": False},
    ],
)
def test_inconsistent_configs_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SttConfig.model_validate(fields)


def test_event_defaults() -> None:
    event = SttEvent(kind="partial", received_at=1.0)
    assert (
        event.text,
        event.words,
        event.entities,
        event.language_code,
        event.error_type,
        event.is_update,
    ) == (
        "",
        [],
        [],
        None,
        None,
        False,
    )


def test_connection_hides_the_key() -> None:
    assert "secret-key" not in repr(SttConnection(ws_base="ws://x", api_key="secret-key"))
