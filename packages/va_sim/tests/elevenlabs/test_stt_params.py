"""Realtime STT query parameters: defaults, validation reasons, repeated parameters, and the config echo."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from starlette.datastructures import QueryParams

from va_sim.elevenlabs.stt_params import AUDIO_FORMATS, config_json, parse_stt_params


def query(**params: str | list[str]) -> QueryParams:
    return QueryParams(urlencode({"model_id": "scribe_v2_realtime", **params}, doseq=True))


def test_defaults() -> None:
    params = parse_stt_params(query())
    assert params.model_id == "scribe_v2_realtime"
    assert params.audio_format == AUDIO_FORMATS["pcm_16000"]
    assert params.commit_strategy == "manual"
    assert (params.include_timestamps, params.include_language_detection) == (False, False)
    assert (params.filter_background_audio, params.enable_logging, params.no_verbatim) == (False, True, False)
    assert params.language_code is None
    assert (params.keyterms, params.entity_detection) == ((), ())
    assert params.keepalive_interval_ms is None
    assert params.vad_silence_threshold_secs == 1.5


def test_every_audio_format() -> None:
    names = ["ulaw_8000", "pcm_8000", "pcm_16000", "pcm_22050", "pcm_24000", "pcm_44100", "pcm_48000"]
    assert list(AUDIO_FORMATS) == names
    ulaw = parse_stt_params(query(audio_format="ulaw_8000")).audio_format
    assert (ulaw.sample_rate, ulaw.encoding) == (8_000, "ulaw")
    pcm = parse_stt_params(query(audio_format="pcm_44100")).audio_format
    assert (pcm.sample_rate, pcm.encoding) == (44_100, "pcm")


def test_repeated_keyterms_and_entity_detection() -> None:
    params = parse_stt_params(query(keyterms=["Acme", "Karen"], entity_detection=["pii", "pci"]))
    assert params.keyterms == ("Acme", "Karen")
    assert params.entity_detection == ("pii", "pci")


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"model_id": ""}, "invalid model_id: required"),
        ({"audio_format": "mp3_44100_128"}, "invalid audio_format: must be one of ulaw_8000, pcm_8000"),
        ({"commit_strategy": "auto"}, "invalid commit_strategy: must be manual or vad"),
        ({"include_timestamps": "yes"}, "invalid include_timestamps: must be true or false"),
        ({"no_verbatim": "1"}, "invalid no_verbatim: must be true or false"),
        ({"language_code": "EN"}, "invalid language_code: must be 2 or 3 lowercase letters"),
        ({"keyterms": ["x" * 51]}, "invalid keyterms: each term must be 1 to 50 characters"),
        ({"keyterms": [f"t{i}" for i in range(101)]}, "invalid keyterms: at most 100 terms"),
        ({"entity_detection": ["ssn"]}, "invalid entity_detection: each value must be pii, pci, phi, or all"),
        ({"keepalive_interval_ms": "soon"}, "invalid keepalive_interval_ms: must be an integer"),
        ({"keepalive_interval_ms": "999"}, "invalid keepalive_interval_ms: must be between 1000 and 60000"),
        ({"vad_silence_threshold_secs": "x"}, "invalid vad_silence_threshold_secs: must be a number"),
        (
            {"vad_silence_threshold_secs": "nan"},
            "invalid vad_silence_threshold_secs: must be between 0.3 and 3.0",
        ),
        (
            {"vad_silence_threshold_secs": "3.5"},
            "invalid vad_silence_threshold_secs: must be between 0.3 and 3.0",
        ),
    ],
)
def test_invalid_parameters(params: dict[str, str | list[str]], reason: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid ") as raised:
        parse_stt_params(query(**params))
    assert str(raised.value).startswith(reason)


def test_missing_model_id() -> None:
    with pytest.raises(ValueError, match=r"^invalid model_id: required$"):
        parse_stt_params(QueryParams("audio_format=ulaw_8000"))


def test_unknown_parameters_are_ignored() -> None:
    assert parse_stt_params(query(colour="blue")).model_id == "scribe_v2_realtime"


def test_config_echo_key_order() -> None:
    params = parse_stt_params(
        query(
            audio_format="ulaw_8000",
            commit_strategy="vad",
            include_timestamps="true",
            language_code="es",
            keyterms=["Acme"],
            entity_detection=["all"],
            keepalive_interval_ms="5000",
            vad_silence_threshold_secs="0.8",
        )
    )
    config = config_json(params)
    assert list(config) == [
        "model_id",
        "audio_format",
        "sample_rate",
        "commit_strategy",
        "include_timestamps",
        "include_language_detection",
        "language_code",
        "keyterms",
        "entity_detection",
        "filter_background_audio",
        "keepalive_interval_ms",
        "enable_logging",
        "no_verbatim",
        "vad_silence_threshold_secs",
    ]
    assert config["sample_rate"] == 8_000
    assert config["commit_strategy"] == "vad"
    assert config["language_code"] == "es"
    assert config["keyterms"] == ["Acme"]
    assert config["entity_detection"] == ["all"]
    assert config["keepalive_interval_ms"] == 5_000
    assert config["vad_silence_threshold_secs"] == 0.8
