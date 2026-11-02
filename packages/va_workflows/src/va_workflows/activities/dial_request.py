"""The outbound ``DialRequest`` of a call (specs/06-telephony-telnyx.md §6 step 5, Decision P05-T06b).

Pure: everything comes from the call row, its resolved :class:`~va_domain.calls.SendCallConfig`, and the
worker's :class:`DialSettings`.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Final, Literal

from va_common.stream_tokens import media_ws_scheme as scheme_for_env
from va_domain.calls import SendCallConfig
from va_domain.voicemail import amd_mode
from va_telnyx import AmdConfig, ClientState, CustomHeader, DialRequest, StreamParams
from va_telnyx.client_state import encode
from va_telnyx.streams import RECORDING_FIELDS, media_stream_url
from va_telnyx.streams import stream_params as shared_stream_params

__all__ = [
    "CALL_ID_HEADER",
    "MAX_TIME_LIMIT_S",
    "PREMIUM_AMD_CONFIG",
    "DialSettings",
    "amd_mode",
    "build_dial_request",
    "dial_command_id",
    "stream_params",
    "stream_url",
    "time_limit_secs",
]

MAX_TIME_LIMIT_S: Final = 14_400
TIME_LIMIT_GRACE_S: Final = 30
CALL_ID_HEADER: Final = "X-VA-Call-Id"

PREMIUM_AMD_CONFIG: Final = AmdConfig(
    total_analysis_time_millis=5000,
    after_greeting_silence_millis=800,
    greeting_duration_millis=3500,
    initial_silence_millis=3500,
    maximum_number_of_words=5,
    silence_threshold=256,
    greeting_total_analysis_time_millis=8000,
    greeting_silence_duration_millis=1500,
)
"""The premium AMD tuning of specs/06 §6 step 5, verbatim."""


@dataclass(frozen=True, slots=True)
class DialSettings:
    """Worker settings the dial needs: the platform Call Control App, the public media host and its scheme,
    the stream token secret (hidden from ``repr``), and the international destination allow-list."""

    call_control_app_id: str
    media_public_host: str
    media_ws_scheme: Literal["ws", "wss"]
    stream_secret: bytes = field(repr=False)
    allowed_countries: frozenset[str]

    @classmethod
    def for_env(
        cls,
        va_env: str,
        *,
        call_control_app_id: str,
        media_public_host: str,
        stream_secret: bytes,
        allowed_countries: frozenset[str],
    ) -> DialSettings:
        """Settings with the scheme ``VA_ENV`` implies: ``ws`` in ``local``/``test``, ``wss`` elsewhere."""
        return cls(
            call_control_app_id=call_control_app_id,
            media_public_host=media_public_host,
            media_ws_scheme=scheme_for_env(va_env),
            stream_secret=stream_secret,
            allowed_countries=allowed_countries,
        )


def dial_command_id(call_id: uuid.UUID) -> str:
    """``dial-{call_id}`` (specs/06 §6 step 6): every retry of the dial is deduplicated by Telnyx."""
    return f"dial-{call_id}"


def time_limit_secs(max_duration_min: float) -> int:
    """``min(14400, ceil(max_duration_min * 60) + 30)``."""
    return min(MAX_TIME_LIMIT_S, math.ceil(max_duration_min * 60) + TIME_LIMIT_GRACE_S)


def stream_url(settings: DialSettings, call_id: uuid.UUID, token: str) -> str:
    """``{scheme}://{MEDIA_PUBLIC_HOST}/ws/telnyx?call_id=…&token=…``."""
    return media_stream_url(settings.media_ws_scheme, settings.media_public_host, call_id, token)


def stream_params(config: SendCallConfig, url: str) -> StreamParams:
    """Bidirectional RTP on the inbound track: PCMU 8 kHz, or L16 16 kHz with ``hd_audio``."""
    return shared_stream_params(url, hd_audio=config.hd_audio)


def build_dial_request(  # noqa: PLR0913 - the call, its config, keyword-only dial facts
    call_id: uuid.UUID,
    org_id: uuid.UUID,
    config: SendCallConfig,
    *,
    to: str,
    from_: str,
    connection_id: str,
    timeout_secs: int,
    stream_token: str,
    settings: DialSettings,
) -> DialRequest:
    """The ``POST /calls`` body of the call's primary leg (``webhook_url`` = the app's own URL)."""
    mode = amd_mode(config)
    record: dict[str, str] = {}
    if config.record:
        record = {**RECORDING_FIELDS, "record_track": "both"}
    return DialRequest.model_validate(
        {
            "connection_id": connection_id,
            "to": to,
            "from_": from_,
            "timeout_secs": timeout_secs,
            "time_limit_secs": time_limit_secs(config.max_duration_min),
            "answering_machine_detection": mode,
            "answering_machine_detection_config": PREMIUM_AMD_CONFIG if mode == "premium" else None,
            "client_state": encode(ClientState(call_id=call_id, leg="primary", org_id=org_id)),
            "command_id": dial_command_id(call_id),
            "stream": stream_params(config, stream_url(settings, call_id, stream_token)),
            "custom_headers": [CustomHeader(name=CALL_ID_HEADER, value=str(call_id))],
            **record,
        }
    )
