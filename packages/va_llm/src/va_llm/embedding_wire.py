"""Embeddings wire parsing, normalisation, truncation, and the query cache key (specs/10-llm-gateway.md §10;
Decision 2026-11-01 P03-T14b rules 3, 4, 6).

Pure. The response body is parsed by our own code (not the SDK) so a malformed vector is a protocol error of
the attempt: ``data`` holds exactly one object per input with distinct ``index`` values ``0..n-1``, each
``embedding`` a list of ``dimensions`` finite numbers with a non-zero norm; ``usage.prompt_tokens`` is
optional. Vectors come back L2-normalised in input order.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import orjson

from va_llm.errors import LlmProtocolError

__all__ = [
    "MAX_INPUT_BYTES",
    "ParsedEmbeddings",
    "is_vector",
    "l2_normalize",
    "parse_embeddings",
    "query_cache_key",
    "truncate_input",
]

MAX_INPUT_BYTES: Final = 32_000


@dataclass(frozen=True, slots=True)
class ParsedEmbeddings:
    """The unit vectors of one batch in input order and the provider's ``usage.prompt_tokens``."""

    vectors: list[list[float]]
    prompt_tokens: int | None


def truncate_input(text: str) -> tuple[str, bool]:
    """``text`` cut to its first 32 000 UTF-8 bytes on a character boundary (8 000 estimated tokens), and
    whether it was cut."""
    encoded = text.encode()
    if len(encoded) <= MAX_INPUT_BYTES:
        return text, False
    return encoded[:MAX_INPUT_BYTES].decode(errors="ignore"), True


def query_cache_key(model: str, dimensions: int, text: str) -> str:
    """``emb:q:{sha256(model|dims|text)}`` with the digest as a Redis hash tag (specs/03 §11)."""
    digest = hashlib.sha256(f"{model}|{dimensions}|{text}".encode()).hexdigest()
    return f"emb:q:{{{digest}}}"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def is_vector(value: object, dimensions: int) -> bool:
    """Whether ``value`` is a list of ``dimensions`` finite numbers."""
    return isinstance(value, list) and len(value) == dimensions and all(_is_number(item) for item in value)


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """``vector`` scaled to unit length (float64); ``ValueError`` for the zero vector."""
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0.0:
        msg = "cannot normalise the zero vector"
        raise ValueError(msg)
    return [value / norm for value in vector]


class _Rejected(Exception):  # noqa: N818 - internal signal, not raised to callers
    """A reason the body is unusable; ``parse_embeddings`` turns it into :class:`LlmProtocolError`."""


def _index(item: dict[str, Any], expected: int, seen: set[int]) -> int:
    index = item.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected or index in seen:
        msg = f"invalid or duplicate index {index!r}"
        raise _Rejected(msg)
    seen.add(index)
    return index


def _vector(item: dict[str, Any], dimensions: int) -> list[float]:
    embedding = item.get("embedding")
    if not isinstance(embedding, list) or not is_vector(embedding, dimensions):
        length = len(embedding) if isinstance(embedding, list) else None
        msg = f"embedding is not {dimensions} finite numbers (length {length})"
        raise _Rejected(msg)
    try:
        return l2_normalize([float(value) for value in embedding])
    except ValueError as exc:
        raise _Rejected(str(exc)) from exc


def _prompt_tokens(document: dict[str, Any]) -> int | None:
    usage = document.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        msg = "usage is not an object"
        raise _Rejected(msg)
    tokens = usage.get("prompt_tokens")
    if tokens is None:
        return None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        msg = f"invalid usage.prompt_tokens {tokens!r}"
        raise _Rejected(msg)
    return tokens


def _parse(body: bytes, expected: int, dimensions: int) -> ParsedEmbeddings:
    try:
        document = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        msg = "body is not JSON"
        raise _Rejected(msg) from exc
    if not isinstance(document, dict):
        msg = "body is not an object"
        raise _Rejected(msg)
    data = document.get("data")
    if not isinstance(data, list) or len(data) != expected:
        count = len(data) if isinstance(data, list) else None
        msg = f"expected {expected} embeddings, got {count}"
        raise _Rejected(msg)
    vectors: list[list[float] | None] = [None] * expected
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            msg = "embedding item is not an object"
            raise _Rejected(msg)
        vectors[_index(item, expected, seen)] = _vector(item, dimensions)
    return ParsedEmbeddings([vector for vector in vectors if vector is not None], _prompt_tokens(document))


def parse_embeddings(
    body: bytes, *, expected: int, dimensions: int, provider_id: str, model: str
) -> ParsedEmbeddings:
    """The unit vectors of an embeddings response for ``expected`` inputs; :class:`LlmProtocolError` for any
    body that does not hold exactly them."""
    try:
        return _parse(body, expected, dimensions)
    except _Rejected as exc:
        msg = f"{provider_id} {model}: unusable embeddings response: {exc}"
        raise LlmProtocolError(msg, provider_id=provider_id, model=model) from exc
