"""Telephony edge settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``edge``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["EdgeSettings"]


class EdgeSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Settings of the Telnyx webhook ingress; Telnyx credentials arrive with the vendor clients."""

    service_name: ClassVar[str] = "telephony-edge"

    edge_public_host: str = "localhost:8100"
    media_public_host: str = "localhost:8200"
