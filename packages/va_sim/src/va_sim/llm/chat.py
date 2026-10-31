"""``POST /v1/chat/completions`` and ``GET /v1/models`` (specs/30 §3.4; Decision P03-T06a rules 1-8, 10).

Check order: auth → JSON body → validation → model → armed fault → rule match → reply. ``rate_limit``,
``server_error``, and a ``timeout`` before headers answer before rule matching (they never advance a
``call_index`` counter); ``disconnect``, a ``timeout`` after n tokens, and ``malformed_tool_args`` shape the
reply. Pacing runs on the sim clock: the first token after ``ttft_ms`` (+ rule ``delay_ms``), then one token
every ``1 / tokens_per_s`` seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from va_sim.clock import iso_z
from va_sim.llm.errors import error_body, openai_error
from va_sim.llm.params import ChatRequest, forced_tool, last_user_text, parse_chat, system_text
from va_sim.llm.reply import Reply, malformed, plan_reply, truncate, usage
from va_sim.llm.rules import InjectedError, MatchContext, RuleResponse
from va_sim.llm.store import FaultKind, LlmFault, LlmRequest, LlmStore
from va_sim.llm.wire import DONE, Emission, Meta, completion_body, paced_count, sse, stream_emissions

if TYPE_CHECKING:
    from starlette.types import Message, Receive

    from va_sim.state import SimState

__all__ = ["CALL_ID_HEADER", "SimulatedDisconnect", "auth_error", "chat_completions", "list_models"]

CALL_ID_HEADER: Final = "x-va-call-id"
_BEARER: Final = "bearer "
_EARLY_FAULTS: Final[frozenset[FaultKind]] = frozenset(
    {"rate_limit", "server_error", "timeout", "disconnect"}
)
_MISSING_KEY: Final = (
    "You didn't provide an API key. You need to provide your API key in an Authorization header using Bearer "
    "auth (i.e. Authorization: Bearer YOUR_KEY)."
)
_SERVER_ERROR: Final = "The server had an error while processing your request. Sorry about that!"
_DEFAULT_RETRY_AFTER_S: Final = 1
_DEFAULT_SERVER_STATUS: Final = 500
_TIMEOUT_STATUS: Final = 504
_CHAT_ID_LENGTH: Final = 29
_CALL_ID_LENGTH: Final = 24
_SSE_HEADERS: Final = {"cache-control": "no-cache"}


class SimulatedDisconnect(Exception):  # noqa: N818 - names the simulated event, not an error condition
    """Raised inside a response body to drop the connection (the ``disconnect`` fault)."""


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


@dataclass(frozen=True, slots=True)
class Pacing:
    lead_s: float
    interval_s: float

    def wait_before(self, paced_index: int) -> float:
        return self.lead_s if paced_index == 0 else self.interval_s

    def total(self, paced: int) -> float:
        return self.lead_s + max(paced - 1, 0) * self.interval_s


def _dump(value: BaseModel | str | None) -> JsonValue:
    return value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value


def _record(state: SimState, request: ChatRequest, call_id: str | None) -> LlmRequest:
    store = state.llm
    record = LlmRequest(
        id=store.next_request_id(),
        received_at=iso_z(state.clock.now()),
        model=request.model,
        call_id=call_id,
        stream=request.stream,
        messages=[_dump(message) for message in request.messages],
        tools=[_dump(tool) for tool in request.tools] if request.tools is not None else None,
        tool_choice=_dump(request.tool_choice),
        temperature=request.temperature,
        max_tokens=request.token_limit,
        response_format=_dump(request.response_format),
    )
    store.requests.append(record)
    return record


async def _until_disconnect(receive: Receive) -> None:
    while True:
        message: Message = await receive()
        if message["type"] == "http.disconnect":
            return


async def _hold(store: LlmStore, request: Request) -> None:
    """Hold the request until the client disconnects or the store releases it."""
    watcher = asyncio.ensure_future(_until_disconnect(request.receive))
    try:
        await store.hold(watcher)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def _fault_response(fault: LlmFault) -> JSONResponse:
    if fault.kind == "rate_limit":
        retry_after = fault.retry_after_s if fault.retry_after_s is not None else _DEFAULT_RETRY_AFTER_S
        return openai_error(
            429,
            "Rate limit reached for requests. Please try again later.",
            code="rate_limit_exceeded",
            headers={"retry-after": str(retry_after)},
        )
    return openai_error(fault.status or _DEFAULT_SERVER_STATUS, _SERVER_ERROR)


def _injected(error: InjectedError, rule_id: str) -> JSONResponse:
    body = (
        error.body
        if error.body is not None
        else error_body(error.status, f"Injected error (rule {rule_id}).")
    )
    return JSONResponse(body, status_code=error.status, headers=error.headers or None)


def _context(request: ChatRequest, call_id: str | None) -> MatchContext:
    return MatchContext(
        model=request.model,
        system_text=system_text(request),
        last_user_text=last_user_text(request),
        forced_tool=forced_tool(request),
        tool_names=request.tool_names,
        call_id=call_id,
    )


async def _cut_off(first: bytes | None) -> AsyncIterator[bytes]:
    if first is not None:
        yield first
    msg = "simulated disconnect"
    raise SimulatedDisconnect(msg)


async def _stream(  # noqa: PLR0913 - keyword-only stream knobs
    emissions: list[Emission],
    *,
    pacing: Pacing,
    sleep: Callable[[float], Awaitable[None]],
    hold: Callable[[], Awaitable[None]],
    stall_after: int | None,
    disconnect: bool,
) -> AsyncIterator[bytes]:
    if disconnect:
        async for part in _cut_off(sse(emissions[0].chunk)):
            yield part
    paced = 0
    for emission in emissions:
        if emission.paced:
            if paced == stall_after:
                await hold()
                return
            await sleep(pacing.wait_before(paced))
            paced += 1
        yield sse(emission.chunk)
    yield DONE


class _Exchange:
    """One chat completion after the model check: faults, rule, reply, response."""

    def __init__(self, state: SimState, request: Request, chat: ChatRequest, record: LlmRequest) -> None:
        self.state = state
        self.request = request
        self.chat = chat
        self.record = record

    async def respond(self) -> Response:
        store = self.state.llm
        fault = store.take_fault(_EARLY_FAULTS)
        if fault is not None:
            self.record.fault = fault.kind
            early = await self._early(fault)
            if early is not None:
                return early
        rule = store.rules.match(_context(self.chat, self.record.call_id))
        respond = rule.respond if rule is not None else None
        if rule is not None:
            self.record.rule_id = rule.id
        if respond is not None and respond.error is not None:
            await self.state.clock.sleep(respond.delay_ms / 1000)
            self.record.status = respond.error.status
            return _injected(respond.error, rule.id if rule is not None else "")
        reply = self._reply(respond)
        self.record.status = 200
        self.record.finish_reason = reply.finish_reason
        return await self._answer(reply, respond, fault)

    async def _early(self, fault: LlmFault) -> Response | None:
        if fault.kind in {"rate_limit", "server_error"}:
            response = _fault_response(fault)
            self.record.status = response.status_code
            return response
        if fault.kind == "timeout" and (fault.after_tokens is None or not self.chat.stream):
            await _hold(self.state.llm, self.request)
            self.record.status = _TIMEOUT_STATUS
            return openai_error(_TIMEOUT_STATUS, "The simulated timeout was released.", code="timeout")
        return None

    def _reply(self, respond: RuleResponse | None) -> Reply:
        ids = self.state.ids
        reply = truncate(
            plan_reply(self.chat, respond, new_call_id=lambda: f"call_{ids.alnum(_CALL_ID_LENGTH)}"),
            self.chat.token_limit,
        )
        if reply.tool_calls and self.state.llm.take_fault(frozenset({"malformed_tool_args"})) is not None:
            self.record.fault = "malformed_tool_args"
            return malformed(reply)
        return reply

    def _pacing(self, respond: RuleResponse | None) -> Pacing:
        latencies = self.state.latencies
        ttft_ms = (
            respond.ttft_ms if respond is not None and respond.ttft_ms is not None else latencies.ttft_ms
        )
        delay_ms = respond.delay_ms if respond is not None else 0
        return Pacing(lead_s=(ttft_ms + delay_ms) / 1000, interval_s=1 / latencies.tokens_per_s)

    async def _answer(self, reply: Reply, respond: RuleResponse | None, fault: LlmFault | None) -> Response:
        state = self.state
        meta = Meta(
            id=f"chatcmpl-{state.ids.alnum(_CHAT_ID_LENGTH)}",
            created=int(state.clock.now().timestamp()),
            model=self.chat.model,
        )
        counted = usage(self.chat, reply)
        pacing = self._pacing(respond)
        disconnect = fault is not None and fault.kind == "disconnect"
        if self.chat.stream:
            emissions = stream_emissions(meta, reply, counted, include_usage=self.chat.include_usage)
            body = _stream(
                emissions,
                pacing=pacing,
                sleep=state.clock.sleep,
                hold=lambda: _hold(state.llm, self.request),
                stall_after=fault.after_tokens if fault is not None and fault.kind == "timeout" else None,
                disconnect=disconnect,
            )
            return StreamingResponse(body, media_type="text/event-stream", headers=_SSE_HEADERS)
        await state.clock.sleep(pacing.total(paced_count(reply)))
        if disconnect:
            return StreamingResponse(_cut_off(None), media_type="application/json")
        return JSONResponse(completion_body(meta, reply, counted))


async def chat_completions(state: SimState, request: Request) -> Response:
    store = state.llm
    denied = auth_error(request, store)
    if denied is not None:
        return denied
    chat = parse_chat(await request.body())
    if isinstance(chat, JSONResponse):
        return chat
    record = _record(state, chat, request.headers.get(CALL_ID_HEADER))
    if chat.model not in store.models:
        record.status = 404
        return openai_error(
            404,
            f"The model `{chat.model}` does not exist or you do not have access to it.",
            error_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )
    return await _Exchange(state, request, chat, record).respond()


async def list_models(state: SimState, request: Request) -> Response:
    denied = auth_error(request, state.llm)
    if denied is not None:
        return denied
    created = int(state.clock.now().timestamp())
    data: list[JsonValue] = [
        {"id": model_id, "object": "model", "created": created, "owned_by": "sim"}
        for model_id in state.llm.models
    ]
    return JSONResponse({"object": "list", "data": data})
