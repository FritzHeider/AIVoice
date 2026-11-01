"""Language resolution for the ElevenLabs clients and the detection lock (specs/08 §5); the table lives in
va_domain.languages."""

from __future__ import annotations

from va_common.errors import AppError
from va_domain.languages import (
    DEFAULT_TTS_LANGUAGE,
    LANGUAGE_MAP,
    TTS_SUPPORTED,
    LanguageMapping,
    lookup_language,
)

__all__ = [
    "DEFAULT_TTS_LANGUAGE",
    "LANGUAGE_MAP",
    "TTS_SUPPORTED",
    "LanguageLock",
    "LanguageMapping",
    "resolve_language",
    "tts_language",
]


def resolve_language(value: str) -> LanguageMapping:
    """The mapping of a Bland ``language`` value (case-insensitive); unknown values are ``INVALID_INPUT``."""
    mapping = lookup_language(value)
    if mapping is None:
        msg = f"unsupported language {value!r}"
        raise AppError("INVALID_INPUT", msg, field="language")
    return mapping


def tts_language(mapping: LanguageMapping, detected: str | None) -> str:
    """The TTS ``language_code`` for a session with ``mapping`` whose working language is ``detected``."""
    if mapping.tts_code is not None:
        return mapping.tts_code
    if detected is not None and detected in mapping.tts_choices:
        return detected
    return DEFAULT_TTS_LANGUAGE


class LanguageLock:
    """The working language under detection: two consecutive committed utterances that agree lock it; until
    then the latest detected language is used (specs/08 §5)."""

    def __init__(self) -> None:
        self._previous: str | None = None
        self._locked: str | None = None

    @property
    def locked(self) -> str | None:
        return self._locked

    @property
    def current(self) -> str | None:
        return self._locked or self._previous

    def observe(self, code: str | None) -> str | None:
        """Record the ``language_code`` of a committed transcript; returns the working language."""
        if self._locked is not None or code is None:
            return self.current
        if code == self._previous:
            self._locked = code
        self._previous = code
        return code
