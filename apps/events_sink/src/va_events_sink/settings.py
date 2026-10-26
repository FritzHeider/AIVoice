"""Events sink settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``sink``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import RedisSettings

__all__ = ["EventsSinkSettings"]


class EventsSinkSettings(RedisSettings):
    """Settings of the Redis Streams → ClickHouse consumer."""

    service_name: ClassVar[str] = "events-sink"

    clickhouse_url: str = "http://default:@localhost:8123/va"
