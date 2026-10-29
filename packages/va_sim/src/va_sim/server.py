"""Simulator servers (specs/30-testing-and-evals.md §2 ``sims`` fixture, §3).

``simulator_routes`` is everything one simulator serves; ``apps/sim`` mounts it next to the service probes,
and :func:`running_sims` serves all four in-process on real sockets (ephemeral ports by default) for tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Final

import uvicorn
from starlette.applications import Starlette
from starlette.routing import BaseRoute

from va_common.service import ReadyServer, ServerStartupError, StopController, run_server
from va_sim.clock import clock_for
from va_sim.control import control_routes
from va_sim.settings import SIMULATORS, SimName, SimSettings
from va_sim.state import DEFAULT_SEED, SimState

__all__ = [
    "BASE_PORT",
    "PORT_STEP",
    "SIMULATORS",
    "SimName",
    "build_state",
    "ports_from",
    "running_sims",
    "simulator_routes",
]

BASE_PORT: Final = 9100
PORT_STEP: Final = 100


def ports_from(base: int) -> dict[SimName, int]:
    """Simulator name → port: ``base``, ``base+100``, ``base+200``, ``base+300``."""
    return {name: base + index * PORT_STEP for index, name in enumerate(SIMULATORS)}


def simulator_routes(sim: SimName, state: SimState) -> list[BaseRoute]:
    """Every route simulator ``sim`` serves over the shared ``state``."""
    return control_routes(sim, state)


def build_state(settings: SimSettings, *, seed: int = DEFAULT_SEED) -> SimState:
    """A fresh state with the clock mode and latencies of ``settings``."""
    return SimState(clock=clock_for(settings.sim_clock), latencies=settings.latencies(), seed=seed)


async def _await_ready(sim: SimName, ready: asyncio.Event, serving: asyncio.Task[None]) -> None:
    waiting = asyncio.ensure_future(ready.wait())
    try:
        await asyncio.wait({waiting, serving}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiting.cancel()
    if not ready.is_set():
        serving.result()
        msg = f"simulator {sim} stopped before it was ready"
        raise ServerStartupError(msg)


@asynccontextmanager
async def running_sims(
    state: SimState, *, host: str = "127.0.0.1", ports: Mapping[SimName, int] | None = None
) -> AsyncIterator[dict[SimName, str]]:
    """Serve the simulators in ``ports`` (default: all four on ephemeral ports); yields name → base URL."""
    chosen: dict[SimName, int] = dict(ports) if ports is not None else dict.fromkeys(SIMULATORS, 0)
    stop = StopController()
    servers: dict[SimName, ReadyServer] = {}
    readiness: dict[SimName, asyncio.Event] = {}
    tasks: dict[SimName, asyncio.Task[None]] = {}

    def factory_for(sim: SimName) -> Callable[[uvicorn.Config], ReadyServer]:
        def factory(config: uvicorn.Config) -> ReadyServer:
            servers[sim] = ReadyServer(config, ready=readiness[sim])
            return servers[sim]

        return factory

    for sim, port in chosen.items():
        readiness[sim] = asyncio.Event()
        app = Starlette(routes=simulator_routes(sim, state))
        tasks[sim] = asyncio.create_task(
            run_server(
                app, host=host, port=port, stop=stop, install_signals=False, server_factory=factory_for(sim)
            ),
            name=f"sim-{sim}",
        )
    try:
        for sim, task in tasks.items():
            await _await_ready(sim, readiness[sim], task)
        yield {sim: servers[sim].url for sim in chosen}
    finally:
        stop.request("running_sims.exit")
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    # Reached only when neither startup nor the caller's block raised: report a server that crashed meanwhile.
    for result in results:
        if isinstance(result, Exception):
            raise result
