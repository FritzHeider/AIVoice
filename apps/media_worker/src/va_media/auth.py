"""Media WebSocket authentication (specs/06-telephony-telnyx.md §4.2, specs/28-security.md §6.2).

Checks, in order (specs/07 §3 Decision P05-T07a): the query (``call_id`` UUID, non-empty ``token``, ``leg``
``primary`` (default) or ``warm_proxy``), the Telnyx Ed25519 signature over
``{timestamp}|{scheme}://{MEDIA_PUBLIC_HOST}{path}?{raw query}``, the stream token (current or previous
secret, expiry, bound to ``call_id``), and ``Authorization: Bearer <token>`` equal to the query token when
present.
Every failure is a :class:`MediaAuthError` whose ``reason`` is logged; the caller closes with 4401.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from urllib.parse import parse_qs

from nacl.signing import VerifyKey

from va_common.stream_tokens import StreamTokenError, verify_stream_token
from va_telnyx.signatures import WebhookVerificationError, verify_media_ws

__all__ = ["MediaAuth", "MediaAuthError", "MediaClaims", "MediaLeg", "signed_stream_url"]

MediaLeg = Literal["primary", "warm_proxy"]
_LEGS: Final[Mapping[str, MediaLeg]] = {"primary": "primary", "warm_proxy": "warm_proxy"}


class MediaAuthError(Exception):
    """The media WebSocket handshake failed authentication; ``reason`` names the check (never a secret)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"media websocket rejected: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MediaClaims:
    """What an authenticated stream asserts: its call and leg."""

    call_id: uuid.UUID
    leg: MediaLeg


def signed_stream_url(scheme: str, host: str, path: str, raw_query: str) -> str:
    """The ``stream_url`` Telnyx signed: ``{scheme}://{host}{path}`` plus ``?{raw_query}`` when present."""
    url = f"{scheme}://{host}{path}"
    return f"{url}?{raw_query}" if raw_query else url


def _single(params: Mapping[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if values is None or len(values) != 1:
        return None
    return values[0]


def _parse_query(raw_query: str) -> tuple[uuid.UUID, str, MediaLeg]:
    params = parse_qs(raw_query, keep_blank_values=True)
    call_id_raw, token = _single(params, "call_id"), _single(params, "token")
    leg_raw = _single(params, "leg") if "leg" in params else "primary"
    leg = _LEGS.get(leg_raw) if leg_raw is not None else None
    if call_id_raw is None or not token or leg is None:
        raise MediaAuthError("query")
    try:
        call_id = uuid.UUID(call_id_raw)
    except ValueError:
        raise MediaAuthError("query") from None
    return call_id, token, leg


class MediaAuth:
    """Verifies media WebSocket handshakes for one public host (``scheme`` follows ``VA_ENV``)."""

    def __init__(self, *, scheme: str, host: str, public_key: VerifyKey, secrets: Sequence[bytes]) -> None:
        if not secrets:
            msg = "at least one stream token secret is required"
            raise ValueError(msg)
        self._scheme = scheme
        self._host = host
        self._public_key = public_key
        self._secrets = tuple(secrets)

    def verify(self, path: str, raw_query: str, headers: Mapping[str, str], now: datetime) -> MediaClaims:
        """The claims of a handshake; raises :class:`MediaAuthError`."""
        call_id, token, leg = _parse_query(raw_query)
        url = signed_stream_url(self._scheme, self._host, path, raw_query)
        try:
            verify_media_ws(url, headers, self._public_key, now)
        except WebhookVerificationError as exc:
            raise MediaAuthError(exc.reason) from None
        try:
            verify_stream_token(token, secrets=self._secrets, now=now, call_id=call_id)
        except StreamTokenError as exc:
            raise MediaAuthError(f"token_{exc.reason}") from None
        lowered = {name.lower(): value for name, value in headers.items()}
        authorization = lowered.get("authorization")
        if authorization is not None and not hmac.compare_digest(
            authorization.encode("utf-8"), f"Bearer {token}".encode()
        ):
            raise MediaAuthError("authorization")
        return MediaClaims(call_id=call_id, leg=leg)
