"""API service settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``api``)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from va_common.settings import RedisSettings

__all__ = ["ApiSettings"]


class ApiSettings(RedisSettings):
    """Settings of the public REST API; secrets (auth, vendors) arrive with their phases."""

    service_name: ClassVar[str] = "api"

    database_url: str = "postgresql+asyncpg://va:va@localhost:5432/va"
    database_pool_size: int = Field(default=10, ge=1)
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    public_api_base_url: str = "http://localhost:8000"
    dashboard_base_url: str = "http://localhost:3000"
    openapi_public_server_url: str = "http://localhost:8000"
