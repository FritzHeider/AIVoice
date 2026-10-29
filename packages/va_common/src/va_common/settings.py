"""Base settings shared by every service (specs/02-repo-layout-and-conventions.md §6, rows scoped "all").

Each service defines one ``Settings`` class extending :class:`BaseServiceSettings`. Field names are the
lower-cased environment variable names.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BaseServiceSettings",
    "CryptoSettings",
    "DatabaseSettings",
    "Environment",
    "KmsProviderName",
    "LogFormat",
    "RedisSettings",
    "TemporalSettings",
]

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_MASTER_KEY_HEX_LEN = 64


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


class RedisSettings(BaseServiceSettings):
    """Redis connection for backend services (``REDIS_URL``, scope "all backend" in specs/02 §6)."""

    redis_url: str = "redis://localhost:6379/0"


class DatabaseSettings(BaseServiceSettings):
    """Postgres (``DATABASE_URL``, ``DATABASE_POOL_SIZE``: api, edge, worker, media, dispatcher)."""

    database_url: str = "postgresql+asyncpg://va:va@localhost:5432/va"
    database_pool_size: int = Field(default=10, ge=1)


class TemporalSettings(BaseServiceSettings):
    """Temporal client (``TEMPORAL_HOST``/``_NAMESPACE``/``_TASK_QUEUE``: api, edge, worker, media)."""

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "va-main"


class KmsProviderName(StrEnum):
    """Key-encryption-key provider selected by ``KMS_PROVIDER`` (specs/28-security.md §3)."""

    LOCAL = "local"
    AWS = "aws"


def _is_master_key_hex(value: str) -> bool:
    return len(value) == _MASTER_KEY_HEX_LEN and set(value) <= _HEX_DIGITS


class CryptoSettings(BaseServiceSettings):
    """Envelope-encryption settings for services that store tenant secrets (api, worker, media).

    ``LOCAL_MASTER_KEY`` may be empty at construction so a service module imports without environment;
    :func:`va_common.crypto.open_kms_provider` refuses to open the local provider without it.
    """

    kms_provider: KmsProviderName = KmsProviderName.LOCAL
    local_master_key: SecretStr = SecretStr("")
    aws_kms_key_id: str = ""

    @model_validator(mode="after")
    def _check_kms_provider(self) -> CryptoSettings:
        if self.kms_provider is KmsProviderName.AWS:
            if not self.aws_kms_key_id.strip():
                msg = "AWS_KMS_KEY_ID is required when KMS_PROVIDER=aws"
                raise ValueError(msg)
            return self
        if self.is_production:
            msg = "KMS_PROVIDER=local is refused when VA_ENV=production"
            raise ValueError(msg)
        master_key = self.local_master_key.get_secret_value()
        if master_key and not _is_master_key_hex(master_key):
            msg = "LOCAL_MASTER_KEY must be 64 hex characters (32 bytes) when set"
            raise ValueError(msg)
        return self
