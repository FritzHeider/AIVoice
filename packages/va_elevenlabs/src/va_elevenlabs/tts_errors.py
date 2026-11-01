"""TTS socket errors and the classification of server error frames and close frames (specs/09 §11;
Decision P03-T10 rule 7).

Fatal errors stop the socket and are raised from ``audio()`` (and from later calls). A
:class:`TtsServerError` is recoverable: it is counted and the reconnect path handles the close that follows.
Messages never contain the API key.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AUTH_ERROR_CODES",
    "INACTIVITY_PREFIX",
    "INVALID_API_KEY",
    "PROTOCOL_ERROR_CODES",
    "VOICE_NOT_FOUND",
    "TtsAuthError",
    "TtsError",
    "TtsProtocolError",
    "TtsServerError",
    "TtsUnavailableError",
    "TtsVoiceNotFoundError",
    "classify_close",
    "classify_error",
    "is_fatal",
]

POLICY_VIOLATION: Final = 1008
INVALID_API_KEY: Final = "Invalid API key"
VOICE_NOT_FOUND: Final = "voice_not_found"
INACTIVITY_PREFIX: Final = "Have not received"
PROTOCOL_ERROR_CODES: Final = frozenset({"context_closed", "invalid_message", "max_active_conversations"})
AUTH_ERROR_CODES: Final = frozenset({"invalid_api_key", "needs_authorization"})


class TtsError(Exception):
    """A TTS socket failure; ``code`` is the vendor error code or a client-side reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class TtsAuthError(TtsError):
    """The API key was missing or rejected (fatal)."""


class TtsVoiceNotFoundError(TtsError):
    """The socket's voice does not exist (fatal; the caller switches to another voice)."""


class TtsProtocolError(TtsError):
    """A message we sent or received broke the protocol (fatal: a client bug or an unexpected frame)."""


class TtsServerError(TtsError):
    """A server-side failure (quota, internal error); the reconnect path handles the close that follows."""


class TtsUnavailableError(TtsError):
    """The service could not be reached after the reconnect attempts (fatal)."""


def classify_error(error: str, message: str) -> TtsError:
    """The error for a server error frame ``{"error", "message", ...}``."""
    if error == VOICE_NOT_FOUND:
        return TtsVoiceNotFoundError(error, message)
    if error in AUTH_ERROR_CODES:
        return TtsAuthError(error, message)
    if error in PROTOCOL_ERROR_CODES:
        return TtsProtocolError(error, message)
    return TtsServerError(error, message)


def classify_close(code: int | None, reason: str) -> TtsError | None:
    """The fatal error an unexpected close stands for, or ``None`` when the client should reconnect."""
    if reason == VOICE_NOT_FOUND:
        return TtsVoiceNotFoundError(VOICE_NOT_FOUND, "the voice does not exist")
    if code != POLICY_VIOLATION or reason.startswith(INACTIVITY_PREFIX):
        return None
    if reason == INVALID_API_KEY:
        return TtsAuthError("invalid_api_key", reason)
    if reason.startswith("invalid "):
        return TtsProtocolError("invalid_request", reason)
    return None


def is_fatal(error: TtsError) -> bool:
    """Whether ``error`` stops the socket."""
    return not isinstance(error, TtsServerError)
