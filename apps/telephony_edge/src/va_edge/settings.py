"""Telephony edge settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``edge``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import (
    SIM_PUBLIC_KEY_FILE,
    CryptoSettings,
    DatabaseSettings,
    RedisSettings,
    StreamTokenSettings,
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
    StreamTokenSettings,
):
    """Settings of the Telnyx webhook ingress (``TELNYX_PUBLIC_KEY`` via :class:`TelnyxPublicKeySettings`).

    The inbound router (specs/06 §7) sends Call Control commands through the platform key
    (:class:`~va_telnyx.settings.TelnyxApiSettings`) and decrypts BYO Telnyx keys (envelope encryption,
    ``LOCAL_MASTER_KEY`` needed once the container opens with the local KMS provider). Accepting an inbound
    call (specs/06 §7 Decision P05-T12c) mints a media stream token (``STREAM_TOKEN_SECRET``, refused empty
    when the lifespan builds the acceptor) for ``MEDIA_PUBLIC_HOST`` and records the platform model ids in the
    resolved config.
    """

    service_name: ClassVar[str] = "telephony-edge"

    edge_public_host: str = "localhost:8100"
    media_public_host: str = "localhost:8200"
    # Platform model ids recorded in every resolved inbound call config (specs/05 §4.4).
    llm_dialogue_model: str = "zai-org/GLM-5.3-Flash"
    llm_router_model: str = "zai-org/GLM-5.3-Flash"
