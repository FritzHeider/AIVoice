"""API service settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``api``)."""

from __future__ import annotations

from typing import ClassVar, Final

from pydantic import Field, SecretStr, field_validator, model_validator

from va_common.auth import ApiKeyHasher, Pepper
from va_common.email import EmailSettings
from va_common.settings import CryptoSettings, DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["ApiSettings"]

_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
_PEPPER_HEX_LEN: Final = 64
_FIRST_ROTATED_VERSION: Final = 2


class ApiSettings(RedisSettings, DatabaseSettings, TemporalSettings, EmailSettings, CryptoSettings):
    """Settings of the public REST API; further secrets (vendors) arrive with their phases.

    ``API_KEY_PEPPER`` and ``LOCAL_MASTER_KEY`` may be empty at construction so importing ``va_api.main``
    needs no environment; the container refuses to start without them (:meth:`api_key_hasher`,
    ``va_common.crypto.open_kms_provider``).
    """

    service_name: ClassVar[str] = "api"

    public_api_base_url: str = "http://localhost:8000"
    dashboard_base_url: str = "http://localhost:3000"
    openapi_public_server_url: str = "http://localhost:8000"
    # Parent domain of the dashboard/api session cookies; empty keeps them host-only (specs/04 §4.3).
    session_cookie_domain: str = ""
    api_key_pepper: SecretStr = SecretStr("")
    api_key_pepper_previous: SecretStr = SecretStr("")
    api_key_pepper_version: int = Field(default=1, ge=1)
    # Enterprise OIDC SSO; the /v1/org/sso routes refuse to work while false (specs/04 §5).
    oidc_enabled: bool = False

    @field_validator("api_key_pepper", "api_key_pepper_previous")
    @classmethod
    def _check_pepper(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and not (len(raw) == _PEPPER_HEX_LEN and set(raw) <= _HEX_DIGITS):
            msg = "API key peppers must be empty or 64 hex characters (32 bytes)"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_previous_pepper(self) -> ApiSettings:
        if (
            self.api_key_pepper_previous.get_secret_value()
            and self.api_key_pepper_version < _FIRST_ROTATED_VERSION
        ):
            msg = "API_KEY_PEPPER_PREVIOUS requires API_KEY_PEPPER_VERSION of at least 2"
            raise ValueError(msg)
        return self

    def api_key_hasher(self) -> ApiKeyHasher:
        """The hasher for the configured peppers; ``RuntimeError`` when ``API_KEY_PEPPER`` is empty."""
        current = self.api_key_pepper.get_secret_value()
        if not current:
            msg = "API_KEY_PEPPER is not configured"
            raise RuntimeError(msg)
        version = self.api_key_pepper_version
        previous_hex = self.api_key_pepper_previous.get_secret_value()
        previous = Pepper.from_hex(previous_hex, version - 1) if previous_hex else None
        return ApiKeyHasher(Pepper.from_hex(current, version), previous)
