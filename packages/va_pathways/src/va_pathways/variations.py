"""Simulated variations of a unit test's caller message (specs/12 §8 step 3; Decision P07-T11a there).

One analysis completion asks for ``n`` paraphrases (the spec prompt as the system message, the caller message
as the user message); the first JSON array in the answer is read. Only non-blank strings count, stripped and
deduplicated case-insensitively, the original message excluded, the first ``n`` kept. Fewer usable texts are
reported as ``short``; an unavailable model gives no texts and ``error`` ``variations_unavailable``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from va_common.logging import get_logger
from va_llm import ChatMessage, Completion, LlmUnavailableError
from va_llm.policy import CallType

__all__ = [
    "UNAVAILABLE",
    "VARIATION_PROMPT",
    "CompletionPort",
    "Variations",
    "first_json_array",
    "paraphrases",
    "parse_variations",
    "variation_messages",
]

VARIATION_PROMPT: Final = (
    "Rewrite the caller message in {n} different natural ways a real caller might say it on the phone. "
    "Keep the meaning identical. Return JSON list of strings."
)
UNAVAILABLE: Final = "variations_unavailable"
VARIATION_TEMPERATURE: Final = 0.7
VARIATION_MAX_TOKENS: Final = 1_024

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Variations:
    """The usable paraphrases, how many of the ``n`` asked for are missing, and a failure reason."""

    texts: tuple[str, ...]
    short: int
    error: str | None = None


class CompletionPort(Protocol):
    """Where the paraphrases come from (:meth:`va_llm.LlmGateway.complete`)."""

    async def complete(
        self,
        call_type: CallType,
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> Completion: ...


def first_json_array(text: str) -> list[object] | None:
    """The first JSON array that starts at a ``[`` of ``text`` (prose around it is ignored)."""
    decoder = json.JSONDecoder()
    position = text.find("[")
    while position != -1:
        try:
            value, _ = decoder.raw_decode(text, position)
        except ValueError:
            value = None
        if isinstance(value, list):
            return value
        position = text.find("[", position + 1)
    return None


def parse_variations(content: str | None, original: str, n: int) -> Variations:
    """The usable paraphrases of ``content`` (see the module rules)."""
    found = first_json_array(content or "") or []
    seen = {original.strip().casefold()}
    texts: list[str] = []
    for value in found:
        if not isinstance(value, str):
            continue
        text = value.strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        texts.append(text)
        if len(texts) == n:
            break
    return Variations(tuple(texts), n - len(texts))


def variation_messages(message: str, n: int) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=VARIATION_PROMPT.replace("{n}", str(n))),
        ChatMessage(role="user", content=message),
    ]


async def paraphrases(port: CompletionPort, *, org_id: uuid.UUID, message: str, n: int) -> Variations:
    """``n`` paraphrases of ``message`` (none asked for → none produced, no request)."""
    if n <= 0:
        return Variations((), 0)
    try:
        completion = await port.complete(
            "analysis",
            org_id=org_id,
            messages=variation_messages(message, n),
            temperature=VARIATION_TEMPERATURE,
            max_tokens=VARIATION_MAX_TOKENS,
        )
    except LlmUnavailableError as exc:
        _log.warning("pathway_unit_test.variations_unavailable", reason=exc.reason)
        return Variations((), n, UNAVAILABLE)
    return parse_variations(completion.content, message, n)
