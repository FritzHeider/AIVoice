"""Worker settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``worker``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["WorkerSettings"]


class WorkerSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Settings of the Temporal worker host; activity settings arrive with the workflows that need them."""

    service_name: ClassVar[str] = "worker"
