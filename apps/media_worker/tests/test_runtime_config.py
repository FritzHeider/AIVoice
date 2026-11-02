"""Mapping a call context onto the pipeline and the vendor sockets (specs/07 §16 Decision P05-T10a)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from media_helpers import call_context
from service_helpers import json_log_stream, log_events

from va_domain.org import OrgSettings
from va_media.runtime_config import (
    ERROR_END,
    pipeline_config,
    session_end,
    stt_audio_format,
    stt_config,
    tts_output_format,
    tts_voice,
    zero_retention,
)
from va_media.session import STREAM_CLOSED, SessionEnd
from va_voice.audio import L16_16000, PCM16LE_16000, ULAW_8000
from va_voice.pipeline import PipelineResult


def _context(**config: Any) -> Any:
    return call_context(uuid.uuid4(), **config)


def test_pipeline_config_maps_call_and_org_settings() -> None:
    rule = {"word": "Bella", "pronunciation": "Bel-la", "case_sensitive": True, "spaced": False}
    context = _context(
        interruptibility=3,
        block_interruptions=True,
        interruption_threshold_ms=200,
        ivr_mode=True,
        max_duration_min=2.5,
        no_input_timeout_ms=12_000,
        max_reprompts=1,
        ignore_button_press=True,
        pronunciation_guide=[rule],
        wait_for_greeting=True,
        greeting_timeout_ms=9000,
    )
    org = OrgSettings.model_validate(
        {"max_duration_message": "Time is up, bye.", "no_input_goodbye": "Nobody there, bye."}
    )
    config = pipeline_config(context, org, fmt=L16_16000, l16_byte_order="little")
    assert (config.fmt, config.l16_byte_order) == (L16_16000, "little")
    assert (config.interruptibility, config.block_interruptions, config.interruption_threshold_ms) == (
        3,
        True,
        200,
    )
    assert (config.ivr_mode, config.ignore_button_press) == (True, True)
    assert (config.wait_for_greeting, config.greeting_timeout_ms) == (True, 9000)
    assert (config.max_duration_s, config.max_duration_warning_s) == (150.0, 10.0)
    assert (config.max_duration_message, config.no_input_goodbye) == (
        "Time is up, bye.",
        "Nobody there, bye.",
    )
    assert (config.no_input_timeout_ms, config.max_reprompts) == (12_000, 1)
    assert [(r.word, r.pronunciation, r.case_sensitive) for r in config.pronunciation] == [
        ("Bella", "Bel-la", True)
    ]


def test_pipeline_config_defaults_follow_org_defaults() -> None:
    config = pipeline_config(_context(), OrgSettings(), fmt=ULAW_8000, l16_byte_order="big")
    org = OrgSettings()
    assert (config.max_duration_message, config.no_input_goodbye) == (
        org.max_duration_message,
        org.no_input_goodbye,
    )
    assert config.max_duration_s == 300.0
    assert config.tts_format == ULAW_8000
    snapshot = _context().config
    assert (config.wait_for_greeting, config.greeting_timeout_ms) == (
        snapshot.wait_for_greeting,
        snapshot.greeting_timeout_ms,
    )


def test_short_calls_warn_half_way() -> None:
    config = pipeline_config(
        _context(max_duration_min=0.25), OrgSettings(), fmt=ULAW_8000, l16_byte_order="big"
    )
    assert (config.max_duration_s, config.max_duration_warning_s) == (15.0, 7.5)


def test_stt_config_fixed_language_and_keyterms() -> None:
    cfg = stt_config(_context(language="es", keywords=["Bella Pasta:5", "tonight"]), fmt=ULAW_8000)
    assert (cfg.audio_format, cfg.sample_rate) == ("ulaw_8000", 8000)
    assert (cfg.language_code, cfg.include_language_detection) == ("es", False)
    assert cfg.keyterms == ["Bella Pasta", "tonight"]


def test_stt_config_detection_on_16k_legs() -> None:
    cfg = stt_config(_context(language="babel"), fmt=L16_16000)
    assert (cfg.audio_format, cfg.sample_rate) == ("pcm_16000", 16000)
    assert (cfg.language_code, cfg.include_language_detection) == (None, True)
    assert cfg.keyterms == []


@pytest.mark.parametrize(
    ("fmt", "expected"), [(ULAW_8000, "ulaw_8000"), (L16_16000, "pcm_16000"), (PCM16LE_16000, "pcm_16000")]
)
def test_vendor_formats(fmt: Any, expected: str) -> None:
    assert stt_audio_format(fmt) == expected
    assert tts_output_format(fmt) == expected


def test_tts_voice_pins_fixed_languages_only() -> None:
    fixed = tts_voice(_context(language="fr-CA", provider_voice_id="voice-x"), model_id="eleven_flash_v2_5")
    assert (fixed.provider_voice_id, fixed.model_id, fixed.language_code) == (
        "voice-x",
        "eleven_flash_v2_5",
        "fr",
    )
    detected = tts_voice(_context(language="babel"), model_id="m")
    assert detected.language_code is None
    assert detected.settings == _context().config.voice_settings


@pytest.mark.parametrize(
    ("privacy", "hipaa", "available", "expected"),
    [
        (False, False, True, False),
        (True, False, True, True),
        (False, True, True, True),
        (True, False, False, False),
    ],
)
def test_zero_retention(privacy: bool, hipaa: bool, available: bool, expected: bool) -> None:
    org = OrgSettings.model_validate({"privacy": {"zero_retention": privacy}, "hipaa_mode": hipaa})
    assert zero_retention(org, available=available) is expected


def test_unavailable_zero_retention_is_logged() -> None:
    stream = json_log_stream()
    org = OrgSettings.model_validate({"privacy": {"zero_retention": True}})
    zero_retention(org, available=False)
    assert [event["event"] for event in log_events(stream)] == ["media.zero_retention_unavailable"]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("transport_closed", STREAM_CLOSED),
        ("agent_hangup", SessionEnd("ended", "agent_end_call", "agent")),
        ("agent_end_call", SessionEnd("ended", "agent_end_call", "agent")),
        ("max_duration", SessionEnd("ended", "max_duration", "timeout")),
        ("no_input_timeout", SessionEnd("ended", "no_input_timeout", "system")),
        ("ivr_dead_end", SessionEnd("ended", "ivr_dead_end", "system")),
        ("error", ERROR_END),
    ],
)
def test_session_end(reason: str, expected: SessionEnd) -> None:
    assert session_end(PipelineResult(reason, 3)) == expected


def test_unknown_end_reason_is_an_error() -> None:
    stream = json_log_stream()
    assert session_end(PipelineResult("teleported", 0)) == ERROR_END
    assert [(e["event"], e["end_reason"]) for e in log_events(stream)] == [
        ("media.unknown_end_reason", "teleported")
    ]
