"""Pure masking helpers for phone numbers and secret-bearing keys (specs/27-observability.md §1).

No structlog import: the log redaction processor and the Sentry ``before_send`` hook both build on these.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["REDACTED", "is_secret_key", "mask_phone", "mask_phones_in_text"]

REDACTED: Final = "[REDACTED]"

_E164: Final = re.compile(r"^\+[1-9]\d{6,14}$")
_E164_IN_TEXT: Final = re.compile(r"(?<![\d+])\+[1-9]\d{6,14}(?!\d)")
_KEY_SEPARATORS: Final = re.compile(r"[_\-.\s]+")
_CAMEL_LOWER_UPPER: Final = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM: Final = re.compile(r"([A-Z]+)([A-Z][a-z])")
_LONG_NUMBER_DIGITS: Final = 11

_SECRET_SEGMENTS: Final = frozenset(
    {
        "key",
        "secret",
        "token",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "pepper",
        "dsn",
        "credential",
        "credentials",
    }
)
# A concatenated segment ending in one of these words is secret whatever precedes it (``dbpassword``).
_SECRET_SUFFIXES: Final = ("secret", "password", "passwd", "credential", "credentials")
# ``key``/``token`` endings are secret only after a known qualifier, so ``monkey``/``turkey`` stay visible.
_QUALIFIED_SECRET_WORDS: Final = ("key", "token")
_SECRET_QUALIFIERS: Final = frozenset(
    {
        "api",
        "access",
        "refresh",
        "client",
        "secret",
        "private",
        "session",
        "auth",
        "bearer",
        "webhook",
        "signing",
        "id",
        "x",
        "xi",
    }
)
_EXEMPT_LAST_SEGMENTS: Final = frozenset(
    {"prefix", "id", "ids", "count", "version", "type", "name", "length"}
)


def mask_phone(number: str) -> str:
    """Mask an E.164 number: ``+14155550123`` -> ``+1415***0123``; anything else is returned unchanged.

    Numbers with 11 or more digits keep the first 4 and last 4 digits; shorter ones
    (7-10 digits) keep 2 and 2.
    """
    if not _E164.match(number):
        return number
    digits = number[1:]
    keep = 4 if len(digits) >= _LONG_NUMBER_DIGITS else 2
    return f"+{digits[:keep]}***{digits[-keep:]}"


def mask_phones_in_text(text: str) -> str:
    """Mask every E.164 number embedded in free text (prose, URLs, header values)."""
    return _E164_IN_TEXT.sub(lambda match: mask_phone(match.group(0)), text)


def _key_segments(name: str) -> list[str]:
    """Split a key on separators and camelCase boundaries, lower-cased: ``apiKeyPrefix`` -> api/key/prefix."""
    spaced = _CAMEL_LOWER_UPPER.sub(r"\1_\2", _CAMEL_ACRONYM.sub(r"\1_\2", name))
    return [segment for segment in _KEY_SEPARATORS.split(spaced.lower()) if segment]


def _is_secret_segment(segment: str) -> bool:
    if segment in _SECRET_SEGMENTS or segment.endswith(_SECRET_SUFFIXES):
        return True
    for word in _QUALIFIED_SECRET_WORDS:
        if segment.endswith(word) and segment[: -len(word)] in _SECRET_QUALIFIERS:
            return True
    return False


def is_secret_key(name: str) -> bool:
    """Return True when a mapping key names a secret (``api_key``, ``xi-api-key``, ``apiKey``, ...).

    The key is split on ``_``, ``-``, ``.``, whitespace and camelCase boundaries. It is secret
    when any segment is a secret word, ends with ``secret``/``password``/``passwd``/``credential(s)``,
    or is ``key``/``token`` glued to a known qualifier (``apikey``, ``accesstoken``; not ``monkey``),
    and the last segment is not a descriptive suffix (``api_key_prefix``, ``keyId`` stay visible).
    Plural ``tokens`` is a count (``prompt_tokens``), not a secret.
    """
    segments = _key_segments(name)
    if not segments or segments[-1] in _EXEMPT_LAST_SEGMENTS:
        return False
    return any(_is_secret_segment(segment) for segment in segments)
