"""ElevenLabs REST client test helpers: a client on a recording clock with its own metrics registry and span
exporter, a FakeElevenLabs-backed client, and a multipart body decoder that does not use httpx."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any, Final

import httpx
from clock_helpers import RecordingClock
from contract_helpers import control_records
from obs_helpers import memory_provider
from starlette.applications import Starlette
from tts_helpers import API_KEY, realtime_state

from va_common.metrics import new_registry
from va_elevenlabs.rest import ElevenLabsRest
from va_elevenlabs.rest_metrics import REST_REQUESTS_METRIC, ElevenLabsRestMetrics
from va_sim.server import simulator_routes
from va_sim.state import SimState

__all__ = [
    "BASE_URL",
    "KEY",
    "Part",
    "RestRig",
    "SimRest",
    "form_parts",
    "rest_rig",
    "sim_rest",
]

BASE_URL: Final = "https://elevenlabs.invalid"
KEY: Final = "xi-test-key-0123456789"
_SIM_BASE: Final = "http://elevenlabs"


@dataclass(frozen=True, slots=True)
class Part:
    """One decoded multipart part."""

    name: str
    filename: str | None
    content_type: str | None
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")


def form_parts(request: httpx.Request) -> list[Part]:
    """The parts of a ``multipart/form-data`` request, in body order (stdlib ``email`` parser)."""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + request.content
    )
    parts: list[Part] = []
    for item in message.iter_parts():
        assert isinstance(item, Message)
        payload = item.get_payload(decode=True)
        assert isinstance(payload, bytes)
        name = item.get_param("name", header="content-disposition")
        filename = item.get_param("filename", header="content-disposition")
        assert isinstance(name, str)
        parts.append(
            Part(
                name,
                filename if isinstance(filename, str) else None,
                item.get("content-type"),
                payload,
            )
        )
    return parts


class _Observed:
    def __init__(self) -> None:
        self.clock = RecordingClock()
        self.registry = new_registry(process_collectors=False)
        self.metrics = ElevenLabsRestMetrics(self.registry)
        self.provider, self.exporter = memory_provider()

    def build(self, key: str, base_url: str, http: httpx.AsyncClient, seed: int) -> ElevenLabsRest:
        return ElevenLabsRest(
            key,
            base_url,
            http,
            clock=self.clock,
            rng=random.Random(seed),  # noqa: S311 - deterministic retry jitter
            metrics=self.metrics,
            tracer_provider=self.provider,
        )

    def count(self, operation: str, outcome: str) -> float:
        labels = {"operation": operation, "outcome": outcome}
        return self.registry.get_sample_value(REST_REQUESTS_METRIC, labels) or 0.0

    def spans(self, name: str) -> list[Any]:
        return [span for span in self.exporter.get_finished_spans() if span.name == name]


class RestRig(_Observed):
    """A client of ``BASE_URL`` (mock it with respx) on a :class:`RecordingClock`."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__()
        self.client = self.build(KEY, BASE_URL, http, 3)


@asynccontextmanager
async def rest_rig() -> AsyncIterator[RestRig]:
    async with httpx.AsyncClient() as http:
        yield RestRig(http)


class SimRest(_Observed):
    """A client of an in-process FakeElevenLabs (wall-clock state, first audio at once) and a control client
    for its ``/sim/*`` routes."""

    def __init__(self, state: SimState, http: httpx.AsyncClient, key: str) -> None:
        super().__init__()
        self.state = state
        self.control = http
        self.client = self.build(key, _SIM_BASE, http, 5)

    async def records(self, path: str, key: str) -> list[dict[str, Any]]:
        return await control_records(self.control, path, key)

    async def send(self, method: str, path: str, body: dict[str, Any]) -> None:
        (await self.control.request(method, path, json=body)).raise_for_status()


@asynccontextmanager
async def sim_rest(key: str = API_KEY) -> AsyncIterator[SimRest]:
    state = realtime_state()
    app = Starlette(routes=simulator_routes("elevenlabs", state))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_SIM_BASE) as http:
            yield SimRest(state, http, key)
    finally:
        await state.aclose()
