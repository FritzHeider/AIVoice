"""Worker settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``worker``)."""

from __future__ import annotations

from typing import ClassVar, Final

from pydantic import model_validator

from va_common.email import EmailSettings
from va_common.settings import (
    CryptoSettings,
    DatabaseSettings,
    Environment,
    RedisSettings,
    StreamTokenSettings,
    TemporalSettings,
)
from va_common.storage import S3Settings
from va_compliance.international import parse_allowed_countries
from va_elevenlabs.settings import ElevenLabsRestSettings
from va_telnyx.settings import SIM_TELNYX_API_KEY, TelnyxApiSettings
from va_workflows.activities.dial_request import DialSettings

__all__ = ["SIM_TELNYX_API_KEY", "WorkerSettings"]

_DEPLOYED: Final = frozenset({Environment.STAGING, Environment.PRODUCTION})


class WorkerSettings(
    RedisSettings,
    DatabaseSettings,
    TemporalSettings,
    EmailSettings,
    CryptoSettings,
    StreamTokenSettings,
    TelnyxApiSettings,
    ElevenLabsRestSettings,
    S3Settings,
):
    """Settings of the Temporal worker host: the call activities dial through Telnyx (platform key, Call
    Control App, media host, stream token secret) and decrypt BYO Telnyx keys (envelope encryption).

    ``STREAM_TOKEN_SECRET`` and ``TELNYX_CALL_CONTROL_APP_ID`` may be empty at construction in ``local``/
    ``test`` so the module imports without environment; :meth:`dial_settings` refuses them then. Staging and
    production refuse an empty secret, an empty app id, and an empty or simulator Telnyx key at
    construction (the platform key check lives in :class:`~va_telnyx.settings.TelnyxApiSettings`). Errors
    name the variable, never its value. The ElevenLabs REST settings serve the opener pre-render (specs/07
    §11 Decision P05-T10d); the ``S3_*`` settings the recording copy (specs/06 §11 Decision P05-T14).
    """

    service_name: ClassVar[str] = "worker"

    telnyx_call_control_app_id: str = ""
    media_public_host: str = "localhost:8200"
    # Destinations ever dialable; a scheduled call outside them fails admission (specs/24 §6 item 7).
    international_allowed_countries: str = "US,CA,GB,AU,IE,NZ,DE,FR,ES,IT,NL,BE,SE,NO,DK,FI,MX,BR,IN,PH,SG"

    @model_validator(mode="after")
    def _require_dialing_outside_dev(self) -> WorkerSettings:
        if self.va_env not in _DEPLOYED:
            return self
        missing = [
            name
            for name, empty in (
                ("STREAM_TOKEN_SECRET", not self.stream_token_secret.get_secret_value()),
                ("TELNYX_CALL_CONTROL_APP_ID", not self.telnyx_call_control_app_id.strip()),
            )
            if empty
        ]
        if missing:
            msg = f"{', '.join(missing)} must be configured when VA_ENV={self.va_env.value}"
            raise ValueError(msg)
        return self

    def allowed_destination_countries(self) -> frozenset[str]:
        """``INTERNATIONAL_ALLOWED_COUNTRIES`` as a set of ISO regions."""
        return parse_allowed_countries(self.international_allowed_countries)

    def dial_settings(self) -> DialSettings:
        """What the ``dial`` activity needs; ``RuntimeError`` when the stream secret or app id is empty."""
        secret = self.stream_token_secret.get_secret_value()
        if not secret:
            msg = "STREAM_TOKEN_SECRET is not configured"
            raise RuntimeError(msg)
        app_id = self.telnyx_call_control_app_id.strip()
        if not app_id:
            msg = "TELNYX_CALL_CONTROL_APP_ID is not configured"
            raise RuntimeError(msg)
        return DialSettings.for_env(
            self.va_env.value,
            call_control_app_id=app_id,
            media_public_host=self.media_public_host,
            stream_secret=bytes.fromhex(secret),
            allowed_countries=self.allowed_destination_countries(),
        )
