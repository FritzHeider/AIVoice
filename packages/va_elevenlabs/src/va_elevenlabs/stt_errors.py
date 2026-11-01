"""Realtime STT errors and the action for every server error ``message_type`` and close frame (specs/08 §4;
Decision P03-T11 rule 6).

Messages never contain the API key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

__all__ = [
    "ACTIONS",
    "MINIMAL",
    "SttAction",
    "SttAuthError",
    "SttError",
    "SttProtocolError",
    "SttQuotaError",
    "SttTermsError",
    "SttUnavailableError",
    "classify_close",
    "fatal_error",
]

type SttAction = Literal[
    "fatal", "reconnect", "backoff", "minimal", "warn", "shrink", "grow", "throttled", "log"
]

POLICY_VIOLATION: Final = 1008
AUTH_CLOSE_REASONS: Final = frozenset({"Invalid API key", "Missing API key"})
MINIMAL: Final = "minimal"

# Every specs/08 §4 row that the server sends as an error or warning frame (``{"message_type", "error"}``).
ACTIONS: Final[Mapping[str, SttAction]] = {
    "warning": "warn",
    "error": "reconnect",
    "auth_error": "fatal",
    "quota_exceeded": "fatal",
    "commit_throttled": "throttled",
    "unaccepted_terms": "fatal",
    "rate_limited": "backoff",
    "queue_overflow": "grow",
    "resource_exhausted": "backoff",
    "session_time_limit_exceeded": "reconnect",
    "input_error": "log",
    "invalid_request": "minimal",
    "chunk_size_exceeded": "shrink",
    "transcriber_error": "backoff",
}


class SttError(Exception):
    """A realtime STT failure; ``code`` is the server ``message_type`` or a client-side reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class SttAuthError(SttError):
    """The API key was missing or rejected (fatal; platform misconfiguration)."""


class SttQuotaError(SttError):
    """The account's quota is exhausted (fatal)."""


class SttTermsError(SttError):
    """The account has not accepted the terms (fatal)."""


class SttProtocolError(SttError):
    """A frame broke the protocol, the echoed config differs, or the request stays invalid (fatal)."""


class SttUnavailableError(SttError):
    """The service could not be reached after the reconnect attempts (fatal)."""


_FATAL: Final[Mapping[str, type[SttError]]] = {
    "auth_error": SttAuthError,
    "quota_exceeded": SttQuotaError,
    "unaccepted_terms": SttTermsError,
}


def fatal_error(message_type: str, message: str) -> SttError:
    """The error a fatal server frame stands for."""
    return _FATAL.get(message_type, SttProtocolError)(message_type, message)


def classify_close(code: int | None, reason: str) -> SttError | Literal["minimal"] | None:
    """What a close with no preceding error frame means: a fatal error, ``"minimal"`` (reconnect once with a
    minimal config, like ``invalid_request``), or ``None`` (reconnect)."""
    if code != POLICY_VIOLATION:
        return None
    if reason in AUTH_CLOSE_REASONS:
        return SttAuthError("auth_error", reason)
    if reason.startswith("invalid "):
        return MINIMAL
    return None
