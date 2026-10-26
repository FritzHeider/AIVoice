"""Base settings shared by every service (specs/02-repo-layout-and-conventions.md §6, rows scoped "all").

Each service defines one ``Settings`` class extending :class:`BaseServiceSettings`. Field names are the
lower-cased environment variable names.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BaseServiceSettings", "Environment", "LogFormat"]

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Environment(StrEnum):
    """Deployment environment selected by ``VA_ENV``."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """structlog renderer selected by ``LOG_FORMAT``."""

    CONSOLE = "console"
    JSON = "json"


class BaseServiceSettings(BaseSettings):
    """Settings every service reads; subclasses add service-specific fields and set ``service_name``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    service_name: ClassVar[str] = "va"

    va_env: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_transcripts: bool = True
    # Resolved before validation from VA_ENV when unset: console in local, json elsewhere.
    log_format: LogFormat = LogFormat.CONSOLE
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_traces_sampler_arg: float = Field(default=0.1, ge=0.0, le=1.0)
    sentry_dsn: str = ""

    @model_validator(mode="before")
    @classmethod
    def _default_log_format(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("log_format") not in (None, ""):
            return data
        env = str(data.get("va_env") or Environment.LOCAL.value).lower()
        default = LogFormat.CONSOLE if env == Environment.LOCAL.value else LogFormat.JSON
        return {**data, "log_format": default}

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> str:
        level = str(value).strip().upper()
        if level not in _LOG_LEVELS:
            msg = f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}"
            raise ValueError(msg)
        return level

    @property
    def is_production(self) -> bool:
        return self.va_env is Environment.PRODUCTION

    @property
    def is_local(self) -> bool:
        return self.va_env is Environment.LOCAL
