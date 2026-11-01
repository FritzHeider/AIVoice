"""Field readers shared by the non-streaming and streaming response parsers (Decision P03-T13a rule 8,
Decision P03-T13b rule 3).

Each reader raises :class:`Invalid` naming the field; the parsers turn it into
:class:`va_llm.errors.LlmProtocolError`.
"""

from __future__ import annotations

from typing import Any

from va_llm.models import TokenUsage

__all__ = ["Invalid", "read_count", "read_object", "read_text", "read_usage"]


class Invalid(Exception):  # noqa: N818 - internal signal turned into LlmProtocolError
    """A body that is not a usable chat completion; the message says what is wrong."""


def read_object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{where} is not an object"
        raise Invalid(msg)
    return value


def read_text(value: object, where: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        msg = f"{where} is not a {'non-empty ' if required else ''}string"
        raise Invalid(msg)
    return value


def read_count(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"{where} is not a non-negative integer"
        raise Invalid(msg)
    return value


def read_usage(value: object) -> TokenUsage | None:
    if value is None:
        return None
    usage = read_object(value, "usage")
    return TokenUsage(
        prompt_tokens=read_count(usage.get("prompt_tokens"), "usage.prompt_tokens"),
        completion_tokens=read_count(usage.get("completion_tokens"), "usage.completion_tokens"),
    )
