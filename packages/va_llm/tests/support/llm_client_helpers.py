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
from va_llm import ChatMessage, LlmGateway, LlmMetrics, LlmProvider, ProviderRegistry, UsageRecord
from va_llm.pricing import CostTable, ModelCost
from va_llm.providers import OrgProviderSource, org_provider_id
from va_sim.server import simulator_routes
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

__all__ = [
    "BASE_URL",
    "COMPLETIONS",
    "ORG_BASE_URL",
    "ORG_COMPLETIONS",
    "PLATFORM_KEY",
    "SIM_BASE_URL",
    "GatewayRig",
    "Hanging",
    "RecordingUsage",
    "StaticOrgProviders",
    "completion_body",
    "gateway_rig",
    "hanging",
    "make_provider",
    "org_provider",
    "scripted",
    "sent_json",
    "sim_gateway",
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
