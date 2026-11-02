"""ElevenLabs client settings (specs/02-repo-layout-and-conventions.md §6 rows ``ELEVENLABS_*``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_helpers import isolate_env, settings_of

from va_elevenlabs.settings import ElevenLabsSettings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, ElevenLabsSettings)


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ELEVENLABS_") and "=" in line:
            key, rest = line.split("=", 1)
            values[key] = rest.split("#", 1)[0].strip()
    return values


def test_defaults_equal_the_env_example_dev_values() -> None:
    settings = settings_of(ElevenLabsSettings)
    example = _env_example()
    assert settings.elevenlabs_ws_base == example["ELEVENLABS_WS_BASE"]
    assert settings.elevenlabs_api_key.get_secret_value() == example["ELEVENLABS_API_KEY"]
    assert settings.elevenlabs_tts_model == example["ELEVENLABS_TTS_MODEL"]
    assert settings.elevenlabs_stt_model == example["ELEVENLABS_STT_MODEL"]
    assert (
        str(settings.elevenlabs_zero_retention_available).lower()
        == example["ELEVENLABS_ZERO_RETENTION_AVAILABLE"]
    )
    assert settings.elevenlabs_stt_session_rotate_s == float(example["ELEVENLABS_STT_SESSION_ROTATE_S"])
    assert settings.elevenlabs_stt_keyterms_encoding == example["ELEVENLABS_STT_KEYTERMS_ENCODING"]


def test_environment_overrides_and_hides_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-live-key-value")
    monkeypatch.setenv("ELEVENLABS_STT_SESSION_ROTATE_S", "600")
    settings = settings_of(ElevenLabsSettings)
    assert settings.elevenlabs_api_key.get_secret_value() == "xi-live-key-value"
    assert settings.elevenlabs_stt_session_rotate_s == 600
    assert "xi-live-key-value" not in repr(settings)
