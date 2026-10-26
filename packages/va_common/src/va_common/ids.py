"""UUIDv7 identifiers and the public prefixed forms (``TL-``, ``KB-``, ``PT-``)."""

from __future__ import annotations

import re
import uuid
from enum import StrEnum

from uuid_utils.compat import uuid7

from va_common.errors import AppError

__all__ = ["PublicPrefix", "from_public", "is_public", "new_id", "parse_public", "to_public"]

_PUBLIC_RE = re.compile(
    r"(?P<prefix>[A-Z]{2})-(?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
_MAX_ECHO = 64


class PublicPrefix(StrEnum):
    """Prefixes of public identifiers exposed by Bland-compatible endpoints."""

    TOOL = "TL"
    KNOWLEDGE_BASE = "KB"
    PROMPT = "PT"


def new_id() -> uuid.UUID:
    """A new time-ordered UUIDv7."""
    return uuid7()


def to_public(prefix: PublicPrefix, value: uuid.UUID) -> str:
    """Render ``value`` as ``<PREFIX>-<lower-case uuid>``."""
    return f"{prefix.value}-{value}"


def _echo(value: str) -> str:
    shown = value if len(value) <= _MAX_ECHO else value[:_MAX_ECHO] + "…"
    return repr(shown)


def _match(value: str) -> tuple[PublicPrefix, uuid.UUID] | None:
    m = _PUBLIC_RE.fullmatch(value)
    if m is None or m["prefix"] not in PublicPrefix._value2member_map_:
        return None
    return PublicPrefix(m["prefix"]), uuid.UUID(m["uuid"])


def parse_public(value: str) -> tuple[PublicPrefix, uuid.UUID]:
    """Parse a public id with any known prefix; ``INVALID_INPUT`` when malformed."""
    parsed = _match(value)
    if parsed is None:
        raise AppError("INVALID_INPUT", f"Invalid public id: {_echo(value)}")
    return parsed


def from_public(value: str, prefix: PublicPrefix) -> uuid.UUID:
    """Parse a public id that must carry ``prefix``; ``INVALID_INPUT`` otherwise."""
    parsed = _match(value)
    if parsed is None or parsed[0] is not prefix:
        raise AppError("INVALID_INPUT", f"Invalid {prefix.value}- id: {_echo(value)}")
    return parsed[1]


def is_public(value: str, prefix: PublicPrefix) -> bool:
    """Whether ``value`` is a well-formed public id with ``prefix``."""
    parsed = _match(value)
    return parsed is not None and parsed[0] is prefix
