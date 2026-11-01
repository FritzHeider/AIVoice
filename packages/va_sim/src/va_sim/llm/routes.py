"""Everything FakeLLM serves beyond the shared ``/sim/*`` control API (specs/30 §3.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from va_sim.llm.chat import chat_completions, list_models
from va_sim.llm.control import llm_control_routes
from va_sim.llm.embed_route import embeddings

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["llm_routes"]


def llm_routes(state: SimState) -> list[BaseRoute]:
    """``POST /v1/chat/completions``, ``POST /v1/embeddings``, ``GET /v1/models``, and the ``/sim/llm/*``
    control routes."""

    async def completions(request: Request) -> Response:
        return await chat_completions(state, request)

    async def embed(request: Request) -> Response:
        return await embeddings(state, request)

    async def models(request: Request) -> Response:
        return await list_models(state, request)

    return [
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/v1/embeddings", embed, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        *llm_control_routes(state),
    ]
