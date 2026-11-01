"""``POST /v1/embeddings`` (specs/30 §3.4; Decision P03-T06b rule 5).

Check order: auth → JSON body → validation → log entry → model → armed ``embeddings`` fault → vectors. No
pacing: the vectors answer at once. Vector maths runs in a worker thread so a 2048-input request never blocks
the loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue
from starlette.responses import JSONResponse, Response

from va_sim.clock import iso_z
from va_sim.llm.embeddings import EmbeddingRequest, embed, encode, parse_embeddings
from va_sim.llm.gate import (
    TIMEOUT_STATUS,
    auth_error,
    fault_response,
    hold_request,
    model_not_found,
    timeout_response,
)
from va_sim.llm.store import EmbeddingRecord, FaultKind

if TYPE_CHECKING:
    from starlette.requests import Request

    from va_sim.state import SimState

__all__ = ["embeddings"]

_FAULTS: Final[frozenset[FaultKind]] = frozenset({"rate_limit", "server_error", "timeout"})


def _body(request: EmbeddingRequest) -> dict[str, JsonValue]:
    data: list[JsonValue] = [
        {
            "object": "embedding",
            "index": index,
            "embedding": encode(embed(source, request.size), request.encoding_format),
        }
        for index, source in enumerate(request.sources)
    ]
    tokens = request.prompt_tokens
    return {
        "object": "list",
        "data": data,
        "model": request.model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


async def embeddings(state: SimState, request: Request) -> Response:
    store = state.llm
    denied = auth_error(request, store)
    if denied is not None:
        return denied
    parsed = parse_embeddings(await request.body())
    if isinstance(parsed, JSONResponse):
        return parsed
    record = EmbeddingRecord(
        id=store.next_embedding_id(),
        received_at=iso_z(state.clock.now()),
        model=parsed.model,
        inputs=len(parsed.sources),
        dimensions=parsed.size,
        encoding_format=parsed.encoding_format,
    )
    store.embedding_requests.append(record)
    if parsed.model not in store.models:
        record.status = 404
        return model_not_found(parsed.model)
    fault = store.take_fault(_FAULTS, endpoint="embeddings")
    if fault is not None:
        record.fault = fault.kind
        if fault.kind == "timeout":
            await hold_request(store, request)
            record.status = TIMEOUT_STATUS
            return timeout_response()
        answer = fault_response(fault)
        record.status = answer.status_code
        return answer
    body = await asyncio.to_thread(_body, parsed)
    record.status = 200
    return JSONResponse(body)
