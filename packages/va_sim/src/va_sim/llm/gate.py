"""Checks and fault answers shared by every FakeLLM ``/v1`` endpoint (specs/30 §3.4 Decision P03-T06a rules 1,
2, 10; Decision P03-T06b rules 5, 6): Bearer auth, the unknown-model 404, the ``rate_limit``/``server_error``
answers, and holding a request for a ``timeout`` fault.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from starlette.responses import JSONResponse

from va_sim.llm.errors import openai_error

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import Message, Receive

    from va_sim.llm.store import LlmFault, LlmStore

__all__ = [
    "TIMEOUT_STATUS",
    "auth_error",
    "fault_response",
    "hold_request",
    "model_not_found",
    "timeout_response",
]

TIMEOUT_STATUS: Final = 504
_BEARER: Final = "bearer "
_MISSING_KEY: Final = (
    "You didn't provide an API key. You need to provide your API key in an Authorization header using Bearer "
    "auth (i.e. Authorization: Bearer YOUR_KEY)."
)
_SERVER_ERROR: Final = "The server had an error while processing your request. Sorry about that!"
_DEFAULT_RETRY_AFTER_S: Final = 1
_DEFAULT_SERVER_STATUS: Final = 500


def auth_error(request: Request, store: LlmStore) -> JSONResponse | None:
    """The 401 answer for a request without an accepted ``Authorization: Bearer`` key, else None."""
    header = request.headers.get("authorization", "")
    key = header[len(_BEARER) :].strip() if header.lower().startswith(_BEARER) else ""
    if not key:
        return openai_error(401, _MISSING_KEY, error_type="invalid_request_error")
    if not store.accepts(key):
        return openai_error(
            401, "Incorrect API key provided.", error_type="invalid_request_error", code="invalid_api_key"
        )
    return None


def model_not_found(model: str) -> JSONResponse:
    return openai_error(
        404,
        f"The model `{model}` does not exist or you do not have access to it.",
        error_type="invalid_request_error",
        code="model_not_found",
        param="model",
    )


def fault_response(fault: LlmFault) -> JSONResponse:
    """The 429 of a ``rate_limit`` fault (with ``Retry-After``) or the 5xx of a ``server_error`` fault."""
    if fault.kind == "rate_limit":
        retry_after = fault.retry_after_s if fault.retry_after_s is not None else _DEFAULT_RETRY_AFTER_S
        return openai_error(
            429,
            "Rate limit reached for requests. Please try again later.",
            code="rate_limit_exceeded",
            headers={"retry-after": str(retry_after)},
        )
    return openai_error(fault.status or _DEFAULT_SERVER_STATUS, _SERVER_ERROR)


def timeout_response() -> JSONResponse:
    """What a held request answers once the simulator releases it."""
    return openai_error(TIMEOUT_STATUS, "The simulated timeout was released.", code="timeout")


async def _until_disconnect(receive: Receive) -> None:
    while True:
        message: Message = await receive()
        if message["type"] == "http.disconnect":
            return


async def hold_request(store: LlmStore, request: Request) -> None:
    """Hold the request until the client disconnects or the store releases it."""
    watcher = asyncio.ensure_future(_until_disconnect(request.receive))
    try:
        await store.hold(watcher)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
