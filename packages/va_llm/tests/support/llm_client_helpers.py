"""va_llm test helpers: providers, completion bodies, a usage recorder, scripted answers, and gateway rigs.

:func:`gateway_rig` builds an :class:`LlmGateway` on a :class:`ManualClock` (deadlines and retry waits
fire only when the test runs the clock) with a fresh metrics registry, an in-memory span exporter, and a
recording usage recorder; its httpx client uses the default transport, so the ``llm_api`` respx fixture
intercepts it. :func:`sim_gateway` points the gateway at FakeLLM in-process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import orjson
from clock_helpers import ManualClock
from obs_helpers import memory_provider
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from starlette.applications import Starlette

from va_common.metrics import Service, build_metrics, new_registry
from va_llm import (
    ChatMessage,
    DialogueRequest,
    DialogueStream,
    LlmGateway,
    LlmMetrics,
    LlmProvider,
    ProviderRegistry,
    UsageRecord,
)
from va_llm.pricing import CostTable, ModelCost
from va_llm.providers import OrgProviderSource, org_provider_id
from va_sim.server import running_sims, simulator_routes
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

__all__ = [
    "BASE_URL",
    "COMPLETIONS",
    "ORG_BASE_URL",
    "ORG_COMPLETIONS",
    "PLATFORM_KEY",
    "SIM_BASE_URL",
    "BodyPart",
    "GatewayRig",
    "Hanging",
    "RecordingUsage",
    "StaticOrgProviders",
    "StreamingBody",
    "arm_fault",
    "chunk",
    "collect",
    "completion_body",
    "content_stream",
    "dialogue",
    "gateway_rig",
    "hanging",
    "make_provider",
    "open_breaker",
    "org_provider",
    "scripted",
    "sent_json",
    "served_gateway",
    "served_requests",
    "sim_gateway",
    "sse",
    "streamed",
    "user",
]

BASE_URL: Final = "http://llm.test/v1"
COMPLETIONS: Final = f"{BASE_URL}/chat/completions"
ORG_BASE_URL: Final = "http://org-llm.test/v1"
ORG_COMPLETIONS: Final = f"{ORG_BASE_URL}/chat/completions"
PLATFORM_KEY: Final = "platform-secret-key"
SIM_KEY: Final = "sim-llm-key"
SIM_BASE_URL: Final = "http://sim/v1"
MODELS: Final = {
    "dialogue": "dlg-model",
    "router": "rtr-model",
    "analysis": "ana-model",
    "fallback": "fb-model",
}
COSTS: Final = {"dlg-model": ModelCost(100, 400), "fb-model": ModelCost(1000, 3000)}


def make_provider(
    provider_id: str = "platform", base_url: str = BASE_URL, key: str = PLATFORM_KEY, **overrides: Any
) -> LlmProvider:
    """A provider with the test models (``models=`` replaces them)."""
    values: dict[str, Any] = {"models": MODELS, **overrides}
    return LlmProvider(id=provider_id, base_url=base_url, api_key=SecretStr(key), **values)


def org_provider(org_id: uuid.UUID, key: str = "org-secret-key") -> LlmProvider:
    models = {role: f"org-{name}" for role, name in MODELS.items()}
    return make_provider(org_provider_id(org_id), ORG_BASE_URL, key, models=models)


def user(text: str = "Hello there") -> ChatMessage:
    return ChatMessage(role="user", content=text)


def completion_body(
    content: str | None = "Hello.",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: tuple[int, int] | None = (12, 5),
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """A chat-completion body in the OpenAI shape."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return body


def chunk(
    delta: dict[str, Any] | None = None,
    *,
    finish_reason: str | None = None,
    usage: tuple[int, int] | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    """One ``chat.completion.chunk``; ``choices=False`` gives the trailing usage chunk (``choices: []``)."""
    body: dict[str, Any] = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    body["choices"] = (
        [{"index": 0, "delta": delta if delta is not None else {}, "finish_reason": finish_reason}]
        if choices
        else []
    )
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return body


def sse(*chunks: dict[str, Any], done: bool = True) -> list[bytes]:
    """The SSE events of ``chunks`` (one body part each), then ``data: [DONE]`` unless ``done`` is false."""
    parts = [b"data: " + orjson.dumps(item) + b"\n\n" for item in chunks]
    return [*parts, b"data: [DONE]\n\n"] if done else parts


def content_stream(
    *tokens: str, usage: tuple[int, int] | None = (12, 5), finish_reason: str = "stop"
) -> list[dict[str, Any]]:
    """The chunks of a content reply: a role chunk, one chunk per token, the finish chunk, the usage chunk."""
    chunks = [chunk({"role": "assistant", "content": ""})]
    chunks.extend(chunk({"content": token}) for token in tokens)
    chunks.append(chunk(finish_reason=finish_reason))
    if usage is not None:
        chunks.append(chunk(usage=usage, choices=False))
    return chunks


type BodyPart = bytes | Exception | asyncio.Event


@dataclass
class StreamingBody(httpx.AsyncByteStream):
    """A response body yielding ``parts`` in order: bytes are sent, an exception is raised, an event is waited
    for (the body hangs until the test sets it). Records whether it was closed and whether a wait was
    cancelled."""

    parts: list[BodyPart]
    closed: bool = False
    cancelled: bool = False
    sent: int = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            if isinstance(part, asyncio.Event):
                try:
                    await part.wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            elif isinstance(part, Exception):
                raise part
            else:
                self.sent += 1
                yield part

    async def aclose(self) -> None:
        self.closed = True


def streamed(*parts: BodyPart, status: int = 200) -> tuple[httpx.Response, StreamingBody]:
    """An SSE response over a :class:`StreamingBody` of ``parts``."""
    body = StreamingBody(list(parts))
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=body), body


def dialogue(org_id: uuid.UUID, *messages: ChatMessage, **fields: Any) -> DialogueRequest:
    """A dialogue request (one user message unless ``messages`` are given)."""
    return DialogueRequest(org_id=org_id, messages=messages or (user(),), **fields)


async def collect(stream: DialogueStream) -> list[str]:
    """Every delta of ``stream.text()``."""
    return [delta async for delta in stream.text()]


def open_breaker(rig: GatewayRig, model: str) -> None:
    """Open the platform breaker of ``model`` with five failures."""
    breaker = rig.gateway.breaker("platform", model)
    for _ in range(5):
        breaker.allow()
        breaker.record(success=False)


async def arm_fault(control: httpx.AsyncClient, **body: Any) -> None:
    """Arm a FakeLLM fault (``PUT /sim/llm/fault``)."""
    response = await control.put("/sim/llm/fault", json=body)
    assert response.status_code < 300, response.text


async def served_requests(control: httpx.AsyncClient) -> list[dict[str, Any]]:
    """FakeLLM's chat request log."""
    response = await control.get("/sim/llm/requests")
    requests: list[dict[str, Any]] = response.json()["requests"]
    return requests


def sent_json(request: httpx.Request) -> dict[str, Any]:
    document: dict[str, Any] = orjson.loads(request.content)
    return document


@dataclass
class RecordingUsage:
    """A usage recorder that keeps every record (or raises ``failure`` when set)."""

    records: list[UsageRecord] = field(default_factory=list)
    failure: Exception | None = None

    async def record(self, record: UsageRecord) -> None:
        if self.failure is not None:
            raise self.failure
        self.records.append(record)


@dataclass
class StaticOrgProviders:
    """An :class:`OrgProviderSource` over a dict."""

    providers: dict[uuid.UUID, LlmProvider] = field(default_factory=dict)

    async def get(self, org_id: uuid.UUID) -> LlmProvider | None:
        return self.providers.get(org_id)


@dataclass
class Hanging:
    """An async respx side effect that never answers until ``release`` is set; records cancellations."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    response: httpx.Response = field(default_factory=lambda: httpx.Response(200, json=completion_body()))
    requests: list[httpx.Request] = field(default_factory=list)
    cancelled: int = 0

    @property
    def started(self) -> int:
        return len(self.requests)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.response


def hanging(response: httpx.Response | None = None) -> Hanging:
    return Hanging() if response is None else Hanging(response=response)


type Step = httpx.Response | Exception | Hanging


def scripted(*steps: Step) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """A respx side effect answering one step per request: a response, a raised exception, or a hang."""
    queue = list(steps)

    async def side_effect(request: httpx.Request) -> httpx.Response:
        step = queue.pop(0)
        if isinstance(step, Hanging):
            return await step(request)
        if isinstance(step, Exception):
            raise step
        return step

    return side_effect


@dataclass
class GatewayRig:
    gateway: LlmGateway
    clock: ManualClock
    registry: CollectorRegistry
    exporter: InMemorySpanExporter
    usage: RecordingUsage
    org_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())


@asynccontextmanager
async def gateway_rig(  # noqa: PLR0913 - every knob is keyword-only with a default
    *,
    platform: LlmProvider | None = None,
    org_providers: OrgProviderSource | None = None,
    catalog: bool = True,
    costs: Mapping[str, ModelCost] = COSTS,
    transport: httpx.AsyncBaseTransport | None = None,
    record_usage: bool = True,
) -> AsyncIterator[GatewayRig]:
    """A gateway on a manual clock; ``catalog`` passes a media-worker ``ServiceMetrics`` too."""
    registry = new_registry(process_collectors=False)
    service_metrics = build_metrics(Service.MEDIA_WORKER, registry) if catalog else None
    tracer_provider, exporter = memory_provider()
    clock = ManualClock()
    usage = RecordingUsage()
    async with httpx.AsyncClient(transport=transport) as http:
        gateway = LlmGateway(
            ProviderRegistry(platform or make_provider(), org_providers),
            http,
            clock=clock,
            metrics=LlmMetrics(registry, service_metrics),
            recorder=usage if record_usage else None,
            costs=CostTable(costs),
            tracer_provider=tracer_provider,
        )
        yield GatewayRig(gateway, clock, registry, exporter, usage)


@asynccontextmanager
async def sim_gateway(state: SimState) -> AsyncIterator[tuple[GatewayRig, httpx.AsyncClient]]:
    """A gateway whose platform provider is FakeLLM (in-process ASGI, the simulator's default models), and a
    client of the same app for the ``/sim/llm`` control API."""
    models = dict(zip(("dialogue", "analysis", "fallback"), DEFAULT_LLM_MODELS, strict=False))
    models["router"] = DEFAULT_LLM_MODELS[0]
    platform = make_provider(base_url=SIM_BASE_URL, key=SIM_KEY, models=models)
    transport = httpx.ASGITransport(app=Starlette(routes=simulator_routes("llm", state)))
    try:
        async with (
            gateway_rig(platform=platform, transport=transport, costs={}) as rig,
            httpx.AsyncClient(transport=transport, base_url="http://sim") as control,
        ):
            yield rig, control
    finally:
        await state.aclose()


@asynccontextmanager
async def served_gateway(state: SimState) -> AsyncIterator[tuple[GatewayRig, httpx.AsyncClient]]:
    """Like :func:`sim_gateway`, but FakeLLM runs on a real socket (httpx's ASGI transport buffers whole
    response bodies, so streams need a server)."""
    models = dict(zip(("dialogue", "analysis", "fallback"), DEFAULT_LLM_MODELS, strict=False))
    models["router"] = DEFAULT_LLM_MODELS[0]
    async with running_sims(state, ports={"llm": 0}) as urls:
        platform = make_provider(base_url=f"{urls['llm']}/v1", key=SIM_KEY, models=models)
        async with (
            gateway_rig(platform=platform, costs={}) as rig,
            httpx.AsyncClient(base_url=urls["llm"]) as control,
        ):
            yield rig, control
