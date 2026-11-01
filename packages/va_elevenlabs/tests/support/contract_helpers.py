"""Contract-test base: FakeElevenLabs on an ephemeral port (wall clock), its control API, and media-worker
metrics for the client under test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from tts_helpers import realtime_state, served

from va_common.metrics import Service, build_metrics, new_registry
from va_sim.state import SimState

__all__ = ["SimClient", "control_records", "sim_served"]


class SimClient:
    """The simulator ``state``, its ``ws_base``, a ``control`` HTTP client, and a metrics registry."""

    def __init__(self, state: SimState, ws_base: str, control: httpx.AsyncClient) -> None:
        self.state = state
        self.ws_base = ws_base
        self.control = control
        self.registry = new_registry(process_collectors=False)
        self.service_metrics = build_metrics(Service.MEDIA_WORKER, self.registry)

    async def records(self, path: str, key: str) -> list[dict[str, Any]]:
        return await control_records(self.control, path, key)

    async def put(self, path: str, body: dict[str, Any]) -> None:
        (await self.control.put(path, json=body)).raise_for_status()

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0


async def control_records(control: httpx.AsyncClient, path: str, key: str) -> list[dict[str, Any]]:
    """The ``key`` list of a simulator control ``GET path``."""
    response = await control.get(path)
    response.raise_for_status()
    records: list[dict[str, Any]] = response.json()[key]
    return records


@asynccontextmanager
async def sim_served() -> AsyncIterator[tuple[SimState, str, httpx.AsyncClient]]:
    """A realtime FakeElevenLabs: its state, ``ws://`` base, and a control client."""
    state = realtime_state()
    async with served(state) as ws_base:
        http_base = ws_base.replace("ws://", "http://", 1)
        async with httpx.AsyncClient(base_url=http_base, timeout=5.0) as control:
            yield state, ws_base, control
