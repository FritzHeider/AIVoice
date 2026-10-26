from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from va_common.settings import BaseServiceSettings, Environment, LogFormat

NO_DOTENV: dict[str, Any] = {"_env_file": None}


def load[T: BaseServiceSettings](cls: type[T]) -> T:
    """Build settings from the process environment only, ignoring any `.env` file."""
    return cls(**NO_DOTENV)


def test_defaults_match_spec() -> None:
    settings = load(BaseServiceSettings)
    assert settings.model_dump() == {
        "va_env": Environment.LOCAL,
        "log_level": "INFO",
        "log_transcripts": True,
        "log_format": LogFormat.CONSOLE,
        "otel_exporter_otlp_endpoint": "http://localhost:4317",
        "otel_traces_sampler_arg": 0.1,
        "sentry_dsn": "",
    }
    assert (settings.is_local, settings.is_production) == (True, False)
    assert BaseServiceSettings.service_name == "va"


def test_environment_variables_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VA_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_TRANSCRIPTS", "false")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example/1")
    settings = load(BaseServiceSettings)
    assert settings.is_production
    assert not settings.is_local
    assert settings.log_level == "WARNING"
    assert settings.log_transcripts is False
    assert settings.log_format is LogFormat.JSON
    assert settings.otel_traces_sampler_arg == 0.5
    assert settings.sentry_dsn == "https://key@sentry.example/1"


@pytest.mark.parametrize("env", ["test", "staging", "production"])
def test_non_local_defaults_to_json_logs(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("VA_ENV", env)
    assert load(BaseServiceSettings).log_format is LogFormat.JSON


def test_explicit_log_format_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VA_ENV", "production")
    monkeypatch.setenv("LOG_FORMAT", "console")
    assert load(BaseServiceSettings).log_format is LogFormat.CONSOLE


def test_empty_log_format_uses_environment_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "")
    assert load(BaseServiceSettings).log_format is LogFormat.CONSOLE


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VA_ENV", "dev"),
        ("LOG_LEVEL", "LOUD"),
        ("OTEL_TRACES_SAMPLER_ARG", "1.5"),
        ("OTEL_TRACES_SAMPLER_ARG", "-0.1"),
        ("LOG_FORMAT", "xml"),
    ],
)
def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        load(BaseServiceSettings)


def test_dotenv_file_in_working_directory_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("VA_ENV=staging\nLOG_LEVEL=debug\n", encoding="utf-8")
    settings = BaseServiceSettings()
    assert settings.va_env is Environment.STAGING
    assert settings.log_level == "DEBUG"
    assert settings.log_format is LogFormat.JSON


def test_settings_are_frozen() -> None:
    settings = load(BaseServiceSettings)
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"


class ExampleSettings(BaseServiceSettings):
    service_name: ClassVar[str] = "example"
    example_port: int = 8123


def test_subclass_adds_fields_and_ignores_unrelated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_PORT", "9000")
    monkeypatch.setenv("SOME_OTHER_SERVICE_VAR", "x")
    monkeypatch.setenv("VA_ENV", "test")
    settings = load(ExampleSettings)
    assert settings.example_port == 9000
    assert settings.va_env is Environment.TEST
    assert ExampleSettings.service_name == "example"
    assert not hasattr(settings, "some_other_service_var")
