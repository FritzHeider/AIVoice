"""Simulator runner settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``sim``)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from va_common.settings import RedisSettings

__all__ = ["SimSettings"]


class SimSettings(RedisSettings):
    """Process-level simulator settings; per-vendor behaviour settings arrive with ``packages/va_sim``."""

    service_name: ClassVar[str] = "sim"

    sim_clock: Literal["realtime", "accelerated"] = "realtime"
    # Empty → in-process state only, and no readiness check.
    sim_state_redis_url: str = ""
    sim_workers: int = Field(default=1, ge=1)
