"""Embeddings wire parsing, normalisation, truncation, and the cache key (Decision P03-T14b rules 3, 4, 6)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import orjson
import pytest
from llm_client_helpers import embedding_body

from va_llm.embedding_wire import (
    MAX_INPUT_BYTES,
    is_vector,
    l2_normalize,
    parse_embeddings,
    query_cache_key,
    truncate_input,
)
from va_llm.errors import LlmProtocolError


def _parse(document: Any, expected: int = 2, dimensions: int = 4) -> Any:
    body = document if isinstance(document, bytes) else orjson.dumps(document)
    return parse_embeddings(body, expected=expected, dimensions=dimensions, provider_id="p", model="m")


def test_vectors_are_normalised_and_sorted_by_index() -> None:
    document = embedding_body(3, value=3.0)
    document["data"].reverse()
    parsed = _parse(document, expected=3)
    assert parsed.vectors == [[1.0, 0.0, 0.0, 0.0]] * 3
    assert parsed.prompt_tokens == 7


def test_order_follows_the_index_not_the_position() -> None:
    document = {
        "data": [
            {"index": 1, "embedding": [0, 2, 0, 0]},
            {"index": 0, "embedding": [3, 0, 4, 0]},
        ]
    }
    parsed = _parse(document)
    assert parsed.vectors == [[0.6, 0.0, 0.8, 0.0], [0.0, 1.0, 0.0, 0.0]]
    assert parsed.prompt_tokens is None


def test_unit_norm_for_arbitrary_vectors() -> None:
    vector = l2_normalize([0.3, -1.7, 2.25, 1e-3])
    assert math.isclose(math.fsum(value * value for value in vector), 1.0, rel_tol=1e-12)


def test_the_zero_vector_cannot_be_normalised() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        l2_normalize([0.0, 0.0])


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (b"not json", "not JSON"),
        ([1, 2], "not an object"),
        ({"data": "x"}, "expected 2 embeddings, got None"),
        ({"data": [{"index": 0, "embedding": [1, 0, 0, 0]}]}, "expected 2 embeddings, got 1"),
        ({"data": [1, 2]}, "item is not an object"),
        (
            {"data": [{"index": 0, "embedding": [1, 0, 0, 0]}, {"index": 0, "embedding": [1, 0, 0, 0]}]},
            "duplicate index 0",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 0, 0, 0]}, {"index": 2, "embedding": [1, 0, 0, 0]}]},
            "index 2",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 0, 0, 0]}, {"index": True, "embedding": [1, 0, 0, 0]}]},
            "index True",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 0, 0]}, {"index": 1, "embedding": [1, 0, 0, 0]}]},
            "length 3",
        ),
        (
            {"data": [{"index": 0, "embedding": "abc"}, {"index": 1, "embedding": [1, 0, 0, 0]}]},
            "length None",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 0, "x", 0]}, {"index": 1, "embedding": [1, 0, 0, 0]}]},
            "finite numbers",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 0, True, 0]}, {"index": 1, "embedding": [1, 0, 0, 0]}]},
            "finite numbers",
        ),
        (
            {"data": [{"index": 0, "embedding": [0, 0, 0, 0]}, {"index": 1, "embedding": [1, 0, 0, 0]}]},
            "zero vector",
        ),
        ({**embedding_body(2), "usage": "lots"}, "usage is not an object"),
        ({**embedding_body(2), "usage": {"prompt_tokens": -1}}, "prompt_tokens -1"),
        ({**embedding_body(2), "usage": {"prompt_tokens": "3"}}, "prompt_tokens '3'"),
    ],
)
def test_unusable_bodies_are_protocol_errors(document: Any, message: str) -> None:
    with pytest.raises(LlmProtocolError, match=message) as caught:
        _parse(document)
    assert caught.value.error_kind == "parse"
    assert caught.value.provider_id == "p"


def test_non_finite_numbers_are_rejected() -> None:
    assert not is_vector([1.0, math.inf], 2)
    assert not is_vector([1.0, math.nan], 2)
    assert is_vector([1, 2.5], 2)
    assert not is_vector((1, 2), 2)


def test_usage_without_prompt_tokens_is_absent() -> None:
    document = {**embedding_body(2), "usage": {"total_tokens": 4}}
    assert _parse(document).prompt_tokens is None


def test_short_texts_are_not_truncated() -> None:
    text = "é" * (MAX_INPUT_BYTES // 2)
    assert truncate_input(text) == (text, False)


def test_long_texts_are_cut_on_a_character_boundary() -> None:
    text = "a" + "€" * 20_000  # 1 + 60 000 bytes; a 3-byte character straddles byte 32 000
    cut, truncated = truncate_input(text)
    assert truncated
    encoded = cut.encode()
    assert len(encoded) <= MAX_INPUT_BYTES
    assert len(encoded) > MAX_INPUT_BYTES - 3
    assert text.startswith(cut)


def test_the_cache_key_hashes_model_dimensions_and_text() -> None:
    digest = hashlib.sha256(b"m|1024|hello").hexdigest()
    assert query_cache_key("m", 1024, "hello") == f"emb:q:{{{digest}}}"
    keys = {
        query_cache_key("m", 1024, "hello"),
        query_cache_key("n", 1024, "hello"),
        query_cache_key("m", 512, "hello"),
        query_cache_key("m", 1024, "hello!"),
    }
    assert len(keys) == 4
