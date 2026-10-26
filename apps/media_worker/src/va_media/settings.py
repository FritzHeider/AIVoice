"""Media worker settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``media``)."""

from __future__ import annotations

import socket
from typing import ClassVar

from pydantic import Field, field_validator

from va_common.settings import DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["MediaSettings"]


class MediaSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Admission and drain settings of a media-worker pod; vendor settings arrive with the pipeline."""

    service_name: ClassVar[str] = "media-worker"

    media_drain_timeout_s: float = Field(default=2700, gt=0)
    media_max_calls_per_pod: int = Field(default=60, ge=1)
    # Empty or unset → the hostname (the pod name under Kubernetes).
    pod_id: str = ""

    @field_validator("pod_id", mode="after")
    @classmethod
    def _default_pod_id(cls, value: str) -> str:
        return value.strip() or socket.gethostname()
