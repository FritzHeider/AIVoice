"""Telephony edge settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``edge``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import (
    SIM_PUBLIC_KEY_FILE,
    DatabaseSettings,
    RedisSettings,
    TelnyxPublicKeySettings,
    TemporalSettings,
)

__all__ = ["SIM_PUBLIC_KEY_FILE", "EdgeSettings"]


class EdgeSettings(RedisSettings, DatabaseSettings, TemporalSettings, TelnyxPublicKeySettings):
    """Settings of the Telnyx webhook ingress (``TELNYX_PUBLIC_KEY`` via :class:`TelnyxPublicKeySettings`)."""

    service_name: ClassVar[str] = "telephony-edge"

    edge_public_host: str = "localhost:8100"
    media_public_host: str = "localhost:8200"
