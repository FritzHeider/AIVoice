"""Telephony edge settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``edge``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import (
    SIM_PUBLIC_KEY_FILE,
    CryptoSettings,
    DatabaseSettings,
    RedisSettings,
    TelnyxPublicKeySettings,
    TemporalSettings,
)
from va_telnyx.settings import TelnyxApiSettings

__all__ = ["SIM_PUBLIC_KEY_FILE", "EdgeSettings"]


class EdgeSettings(
    RedisSettings,
    DatabaseSettings,
    TemporalSettings,
    TelnyxPublicKeySettings,
    TelnyxApiSettings,
    CryptoSettings,
):
    """Settings of the Telnyx webhook ingress (``TELNYX_PUBLIC_KEY`` via :class:`TelnyxPublicKeySettings`).

    The inbound router (specs/06 §7) sends Call Control commands through the platform key
    (:class:`~va_telnyx.settings.TelnyxApiSettings`) and decrypts BYO Telnyx keys (envelope encryption,
    ``LOCAL_MASTER_KEY`` needed once the container opens with the local KMS provider).
    """

    service_name: ClassVar[str] = "telephony-edge"

    edge_public_host: str = "localhost:8100"
    media_public_host: str = "localhost:8200"
