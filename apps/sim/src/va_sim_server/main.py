"""Simulator runner (specs/02-repo-layout-and-conventions.md §1: ``python -m va_sim_server.main``).

One process serves the four simulators of specs/01-architecture.md §1 on consecutive ports (fake Telnyx 9100,
fake ElevenLabs 9200, fake Telnyx Inference 9300, fake Stripe 9400), each with ``/healthz``, ``/readyz``, and
``/metrics``. Readiness checks the shared state Redis only when ``SIM_STATE_REDIS_URL`` is set. The simulator
routes (control API under ``/sim/*`` and the vendor APIs) come from ``packages/va_sim``
(specs/30-testing-and-evals.md §3); all four apps share one ``SimState``. Before serving, the FakeTelnyx
public key is written to ``.sim/telnyx_public_key`` (MASTER_PROMPT §9) so local services can verify its
webhooks. With ``SIM_STATE_REDIS_URL`` set the state's control plane (scenarios, latencies, reset + seed,
utterances) is shared with every other sim process on that Redis and a subscriber task follows its changes
(specs/30 §3 Decision P03-T15c). ``SIM_WORKERS`` other than 1 is refused (exit 2) until the load-test phase.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import uvloop
from redis.asyncio import Redis
from starlette.applications import Starlette

from va_common.checks import Dependencies, open_dependencies
from va_common.health import CheckFn
from va_common.logging import get_logger
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
from va_sim.server import BASE_PORT, SIMULATORS, SimName, build_state, ports_from, simulator_routes
from va_sim.settings import SimSettings
from va_sim.shared import SharedState
from va_sim.state import SimState
from va_sim.telnyx.keys import PUBLIC_KEY_FILE
from va_sim_server import __version__

__all__ = [
    "BASE_PORT",
    "EXIT_UNSUPPORTED",
    "SIMULATORS",
    "SimContainer",
    "build_container",
    "create_app",
    "main",
    "ports_from",
    "run",
    "startup_seed",
]

EXIT_UNSUPPORTED: Final = 2
_WORKERS_UNSUPPORTED: Final = "SIM_WORKERS > 1 is not supported yet (specs/30 §3 Decision P03-T15c)"

_log: Final = get_logger(__name__)
_SEED_BITS: Final = 32


def startup_seed() -> int:
    """A fresh simulator seed for each start of the served simulators."""
    return secrets.randbits(_SEED_BITS)


@dataclass(frozen=True, slots=True)
class SimContainer:
    """The opened dependencies and the shared state Redis client (None without ``SIM_STATE_REDIS_URL``)."""

    deps: Dependencies
    state_redis: Redis | None

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.deps.readiness_checks()


@asynccontextmanager
async def build_container(settings: SimSettings) -> AsyncIterator[SimContainer]:
    """The shared state Redis client when ``SIM_STATE_REDIS_URL`` is set, else no dependency."""
    async with open_dependencies() as deps:
        state_redis = None
        if settings.sim_state_redis_url.strip():
            state_redis = deps.redis(settings, url=settings.sim_state_redis_url)
        yield SimContainer(deps, state_redis)


def create_app(
    source: ReadinessSource, sim: SimName, state: SimState, *, readiness_timeout_s: float = 1.0
) -> Starlette:
    """Simulator ``sim``'s app over the shared ``state``; ``source`` provides the shared readiness checks."""
    app = probe_app()
    app.router.routes.extend(simulator_routes(sim, state))
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
    ports: Mapping[SimName, int],
    container_factory: ContainerFactory[SimSettings] = build_container,
    init_observability: bool = True,
    install_signals: bool = True,
    public_key_file: Path = PUBLIC_KEY_FILE,
) -> None:
    """Serve every simulator until ``stop`` is requested; signals are routed once for all servers.

    A :class:`SimContainer` with a state Redis attaches the shared control plane before serving; its
    subscriber runs next to the servers and is cancelled once they have all returned.
    """
    # A served simulator starts from a fresh seed: the platform persists simulator ids (webhook event ids are
    # its dedup key, call control ids key call legs), so a restarted simulator must not hand out the ids of
    # its previous run. Reproducible runs pass a seed to `POST /sim/reset` (specs/30 §5 Decision P05-T17b).
    state = build_state(settings, seed=startup_seed())
    try:
        with observed(settings.service_name, settings, version=__version__, enabled=init_observability):
            await asyncio.to_thread(state.signing_key.write_public_key, public_key_file)
            _log.info("sim.public_key_written", path=str(public_key_file))
            async with container_factory(settings) as source:
                if isinstance(source, SimContainer) and source.state_redis is not None:
                    await state.attach_shared(SharedState(source.state_redis))
                    _log.info("sim.shared_state_attached")
                signals = shutdown_signals(stop) if install_signals else contextlib.nullcontext()
                with signals:
                    async with asyncio.TaskGroup() as group:
                        subscriber = group.create_task(state.run_subscriber(), name="sim-shared-state")
                        async with asyncio.TaskGroup() as servers:
                            for sim, port in ports.items():
                                app = create_app(source, sim, state)
                                servers.create_task(
                                    run_server(app, host=host, port=port, stop=stop, install_signals=False)
                                )
                        subscriber.cancel()
    finally:
        await state.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(
        argv, default_port=BASE_PORT, description="VoiceAgents simulators (--port is the first)"
    )
    settings = SimSettings()
    if settings.sim_workers != 1:
        _log.error("sim.workers_unsupported", sim_workers=settings.sim_workers)
        sys.stderr.write(f"{_WORKERS_UNSUPPORTED}\n")
        return EXIT_UNSUPPORTED

    async def serve_all() -> None:
        await run(settings, stop=StopController(), host=bind.host, ports=ports_from(bind.port))

    return run_main(serve_all, loop_factory=uvloop.new_event_loop)


if __name__ == "__main__":
    raise SystemExit(main())
