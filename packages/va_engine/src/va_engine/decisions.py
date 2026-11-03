"""Shared pieces of the pathway decision calls (specs/11 §5.3-§5.5 Decision P07-T04).

:class:`StructuredPort` is the gateway seam (:class:`va_llm.LlmGateway` satisfies it), :class:`DecisionInputs`
the conversation a decision looks at, and the text helpers render it for the specs/10 §9 classifier prompts.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

import orjson
from pydantic import BaseModel, JsonValue

from va_engine.models.state import TurnRecord
from va_llm.models import ChatMessage
from va_llm.policy import CallType
from va_voice.text_norm import tokens

__all__ = [
    "STOPWORDS",
    "DecisionInputs",
    "StructuredPort",
    "content_words",
    "elapsed_ms",
    "known_information",
    "latest_line",
    "one_line",
    "transcript_lines",
]

_WHITESPACE: Final = re.compile(r"\s+")
_ROLE_PREFIX: Final = {"user": "Caller", "assistant": "Agent"}

STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "about", "above", "after", "again", "all", "am", "an", "and", "any", "are", "as", "at", "be",
        "because", "been", "before", "being", "below", "between", "both", "but", "by", "caller", "can",
        "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further",
        "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "i'd",
        "i'll", "i'm", "i've", "if", "in", "into", "is", "it", "it's", "its", "just", "like", "me", "more",
        "most", "my", "myself", "of", "off", "on", "once", "only", "or", "other", "our", "ours", "out",
        "over", "own", "please", "same", "she", "should", "so", "some", "such", "than", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "those", "through", "to", "too", "uh",
        "um", "under", "until", "up", "very", "want", "wants", "was", "we", "were", "what", "when", "where",
        "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours",
    }
)  # fmt: skip


class StructuredPort(Protocol):
    """Where structured outputs come from (:meth:`va_llm.LlmGateway.call_structured`)."""

    async def call_structured[M: BaseModel](  # noqa: PLR0913 - mirrors LlmGateway.call_structured
        self,
        call_type: CallType,
        schema: type[M],
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        function_name: str,
        description: str,
        call_id: uuid.UUID | None = None,
    ) -> M: ...


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    """The conversation a decision looks at.

    ``history`` holds the turns *before* the caller's latest message, ``latest`` that message (empty for a
    chained turn without speech), ``variables`` the engine's variable bag.
    """

    org_id: uuid.UUID
    latest: str
    history: Sequence[TurnRecord] = ()
    variables: Mapping[str, JsonValue] = field(default_factory=dict)
    call_id: uuid.UUID | None = None


def one_line(text: str) -> str:
    """``text`` with every whitespace run collapsed to one space, stripped."""
    return _WHITESPACE.sub(" ", text).strip()


def transcript_lines(history: Sequence[TurnRecord], limit: int) -> list[str]:
    """The last ``limit`` user/assistant turns as ``Caller: …`` / ``Agent: …`` lines (tool and system turns
    left out, each turn on one line)."""
    turns = [turn for turn in history if turn.role in _ROLE_PREFIX]
    kept = turns[-limit:] if limit > 0 else []
    return [f"{_ROLE_PREFIX[turn.role]}: {one_line(turn.content)}" for turn in kept]


def latest_line(latest: str) -> str:
    """``Latest caller message: "{text}"`` (one line)."""
    return f'Latest caller message: "{one_line(latest)}"'


def known_information(variables: Mapping[str, JsonValue]) -> str:
    """Compact JSON of ``variables`` with sorted keys; keys starting with ``_`` are left out."""
    visible = {name: value for name, value in variables.items() if not name.startswith("_")}
    return orjson.dumps(visible, option=orjson.OPT_SORT_KEYS).decode()


def content_words(text: str) -> frozenset[str]:
    """The lower-case word tokens of ``text`` that are not stop words."""
    return frozenset(word for word in tokens(text) if word not in STOPWORDS)


def elapsed_ms(start: float, end: float) -> int:
    """Whole milliseconds between two monotonic readings (never negative)."""
    return max(round((end - start) * 1000), 0)
