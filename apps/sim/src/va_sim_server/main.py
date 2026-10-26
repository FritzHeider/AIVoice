"""Simulator runner (specs/02-repo-layout-and-conventions.md §1: ``python -m va_sim_server.main``).

One process serves the four simulators of specs/01-architecture.md §1 on consecutive ports (fake Telnyx 9100,
fake ElevenLabs 9200, fake Telnyx Inference 9300, fake Stripe 9400), each with ``/healthz``, ``/readyz``, and
``/metrics``. Readiness checks the shared state Redis only when ``SIM_STATE_REDIS_URL`` is set. The vendor
routes come from ``packages/va_sim`` (specs/30-testing-and-evals.md).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Final

from starlette.applications import Starlette

from va_common.checks import Dependencies, open_dependencies
from va_common.metrics import new_registry
from va_common.service import (
    ContainerFactory,
    ProbeState,
    ReadinessSource,
    StopController,
    observed,
    parse_bind,
    probe_app,
    run_main,
    run_server,
    shutdown_signals,
)
from va_sim_server import __version__
from va_sim_server.settings import SimSettings

__all__ = ["BASE_PORT", "SIMULATORS", "build_container", "create_app", "main", "ports_from", "run"]

SIMULATORS: Final = ("telnyx", "elevenlabs", "llm", "stripe")
BASE_PORT: Final = 9100
PORT_STEP: Final = 100


def ports_from(base: int) -> dict[str, int]:
    """Simulator name → port: ``base``, ``base+100``, ``base+200``, ``base+300``."""
    return {name: base + index * PORT_STEP for index, name in enumerate(SIMULATORS)}


@asynccontextmanager
async def build_container(settings: SimSettings) -> AsyncIterator[Dependencies]:
    """The shared state Redis client when ``SIM_STATE_REDIS_URL`` is set, else no dependency."""
    async with open_dependencies() as deps:
        if settings.sim_state_redis_url.strip():
            deps.redis(settings, url=settings.sim_state_redis_url)
        yield deps


def create_app(source: ReadinessSource, *, readiness_timeout_s: float = 1.0) -> Starlette:
    """One simulator's app; ``source`` provides the shared readiness checks."""
    app = probe_app()
    app.state.probes = ProbeState(
        __version__,
        new_registry(),
        checks=source.readiness_checks,
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


async def run(  # noqa: PLR0913 - keyword-only knobs and test seams
    settings: SimSettings,
    *,
    stop: StopController,
    host: str,
    ports: Mapping[str, int],
    container_factory: ContainerFactory[SimSettings] = build_container,
    init_observability: bool = True,
    install_signals: bool = True,
) -> None:
    """Serve every simulator until ``stop`` is requested; signals are routed once for all servers."""
    with observed(settings.service_name, settings, version=__version__, enabled=init_observability):
        async with container_factory(settings) as source:
            signals = shutdown_signals(stop) if install_signals else contextlib.nullcontext()
            with signals:
                async with asyncio.TaskGroup() as group:
                    for port in ports.values():
                        app = create_app(source)
                        group.create_task(
                            run_server(app, host=host, port=port, stop=stop, install_signals=False)
                        )


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(
        argv, default_port=BASE_PORT, description="VoiceAgents simulators (--port is the first)"
    )
    settings = SimSettings()

    async def serve_all() -> None:
        await run(settings, stop=StopController(), host=bind.host, ports=ports_from(bind.port))

    return run_main(serve_all)


if __name__ == "__main__":
    raise SystemExit(main())
