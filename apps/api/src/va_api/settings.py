"""API service settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``api``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["ApiSettings"]


class ApiSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Settings of the public REST API; secrets (auth, vendors) arrive with their phases."""

    service_name: ClassVar[str] = "api"

    public_api_base_url: str = "http://localhost:8000"
    dashboard_base_url: str = "http://localhost:3000"
    openapi_public_server_url: str = "http://localhost:8000"
