"""FakeLLM embeddings (specs/30 §3.4; Decision P03-T06b rule 5): request parsing and deterministic vectors.

Pure. A text's words (or a token array's ids) are hashed with blake2b into ``dimensions`` buckets with a
signed weight each, accumulated, and L2-normalized, so texts that share words have a higher cosine similarity.
Vectors are computed in float64 and emitted as float32 (JSON floats, or little-endian float32 bytes in
base64).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, StrictStr, ValidationError
from starlette.responses import JSONResponse

from va_sim.llm.params import invalid, json_object, validation_error
from va_sim.llm.text import words

__all__ = [
    "DEFAULT_DIMENSIONS",
    "MAX_DIMENSIONS",
    "MAX_INPUTS",
    "EmbeddingRequest",
    "Vector",
    "embed",
    "encode",
    "parse_embeddings",
]

type Vector = npt.NDArray[np.float32]
type EmbeddingInput = (
    StrictStr
    | Annotated[list[StrictStr], Field(min_length=1, max_length=2048)]
    | Annotated[list[StrictInt], Field(min_length=1)]
    | Annotated[list[Annotated[list[StrictInt], Field(min_length=1)]], Field(min_length=1, max_length=2048)]
)

DEFAULT_DIMENSIONS: Final = 1024
MAX_DIMENSIONS: Final = 4096
MAX_INPUTS: Final = 2048
_PERSON: Final = b"va-sim-embed"
_DIGEST_BYTES: Final = 8
_BUCKET_BYTES: Final = 4


class EmbeddingRequest(BaseModel):
    """``POST /v1/embeddings`` body; unknown OpenAI parameters are accepted."""

    model_config = ConfigDict(extra="allow", frozen=True)

    model: Annotated[str, Field(min_length=1)]
    input: EmbeddingInput
    dimensions: Annotated[int, Field(ge=1, le=MAX_DIMENSIONS)] | None = None
    encoding_format: Literal["float", "base64"] = "float"
    user: str | None = None

    @property
    def size(self) -> int:
        return self.dimensions if self.dimensions is not None else DEFAULT_DIMENSIONS

    @property
    def sources(self) -> list[str | list[int]]:
        """One entry per input: a text, or a token array."""
        value = self.input
        if isinstance(value, str):
            return [value]
        if value and isinstance(value[0], int):
            return [[token for token in value if isinstance(token, int)]]
        return [item for item in value if isinstance(item, str | list)]

    @property
    def prompt_tokens(self) -> int:
        """Whitespace words of every text plus the length of every token array."""
        return sum(len(source.split()) if isinstance(source, str) else len(source) for source in self.sources)


def _bucket(token: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=_DIGEST_BYTES, person=_PERSON).digest()
    bucket = int.from_bytes(digest[:_BUCKET_BYTES], "big") % dimensions
    return bucket, 1.0 if digest[_BUCKET_BYTES] % 2 == 0 else -1.0


def embed(source: str | Sequence[int], dimensions: int = DEFAULT_DIMENSIONS) -> Vector:
    """The unit vector of ``source`` (a text's words, or a token array's ids as strings)."""
    tokens = words(source) if isinstance(source, str) else [str(token) for token in source]
    accumulated = np.zeros(dimensions, dtype=np.float64)
    for token in tokens or [""]:
        bucket, sign = _bucket(token, dimensions)
        accumulated[bucket] += sign
    norm = float(np.linalg.norm(accumulated))
    if norm == 0.0:
        accumulated[_bucket("", dimensions)[0]] = 1.0
        norm = 1.0
    result: Vector = (accumulated / norm).astype(np.float32)
    return result


def encode(vector: Vector, encoding_format: Literal["float", "base64"]) -> JsonValue:
    """JSON floats, or the little-endian float32 bytes in base64."""
    if encoding_format == "base64":
        return base64.b64encode(vector.astype("<f4").tobytes()).decode("ascii")
    floats: list[JsonValue] = vector.tolist()
    return floats


def _empty_text(payload: dict[str, Any]) -> bool:
    value = payload.get("input")
    return value == "" or (isinstance(value, list) and any(item == "" for item in value))


def parse_embeddings(raw: bytes) -> EmbeddingRequest | JSONResponse:
    """``raw`` as a validated :class:`EmbeddingRequest`, or the OpenAI 400 response."""
    payload = json_object(raw)
    if isinstance(payload, JSONResponse):
        return payload
    try:
        request = EmbeddingRequest.model_validate(payload)
    except ValidationError as exc:
        return validation_error(payload, exc.errors(include_url=False)[0])
    if _empty_text(payload):
        return invalid(
            "Invalid value for 'input': input must not contain empty strings.", "input", "invalid_value"
        )
    return request
