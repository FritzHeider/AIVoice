"""Shared service runtime: probes, background-task supervision, and a signal-owning uvicorn runner.

specs/27-observability.md §9 (``/healthz``, ``/readyz``, ``/metrics`` on every service), MASTER_PROMPT §4.7
(supervised background tasks, cancelled on shutdown), specs/01-architecture.md §3 (graceful drain on SIGTERM).

A service builds its ASGI app with :func:`probe_routes` (or :func:`probe_app` for metrics-port services),
sets ``app.state.probes`` to a :class:`ProbeState`, and runs it with :func:`serve`: SIGTERM/SIGINT request a
stop on the :class:`StopController`, the optional ``drain`` coroutine runs while the listener stays open, then
uvicorn shuts down (lifespan exit closes the dependencies). A second signal forces the exit without
waiting for the drain.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol

import uvicorn
from prometheus_client.registry import Collector
from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from va_common import observability
from va_common.health import CheckFn, liveness_body, run_checks
from va_common.logging import get_logger
from va_common.metrics import render_latest
from va_common.settings import BaseServiceSettings

__all__ = [
    "DEFAULT_HOST",
    "EXIT_CRASHED",
    "EXIT_INTERRUPTED",
    "GRACEFUL_SHUTDOWN_TIMEOUT_S",
    "SHUTDOWN_SIGNALS",
    "Bind",
    "ContainerFactory",
    "ManagedServer",
    "ProbeState",
    "ReadinessSource",
    "ServerStartupError",
    "StopController",
    "Supervisor",
    "container_checks",
    "liveness_response",
    "metrics_response",
    "observed",
    "parse_bind",
    "probe_app",
    "probe_routes",
    "probe_service_app",
    "readiness_response",
    "run_main",
    "run_server",
    "serve",
    "service_lifespan",
    "service_scope",
    "shutdown_signals",
]

# Services bind every interface inside their container; local runs may pass --host 127.0.0.1.
DEFAULT_HOST: Final = "0.0.0.0"  # noqa: S104
GRACEFUL_SHUTDOWN_TIMEOUT_S: Final = 10
SHUTDOWN_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)
EXIT_CRASHED: Final = 1
EXIT_INTERRUPTED: Final = 130
_HTTP_UNAVAILABLE: Final = 503

_log = get_logger(__name__)

type Checks = Callable[[], Mapping[str, CheckFn]]
type Liveness = Callable[[], str | None]
type Drain = Callable[[], Awaitable[None]]
type TaskFactory = Callable[[], Coroutine[Any, Any, None]]
type ServerFactory = Callable[[uvicorn.Config], uvicorn.Server]


class ReadinessSource(Protocol):
    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type ContainerFactory[S] = Callable[[S], AbstractAsyncContextManager[ReadinessSource]]


def _no_checks() -> Mapping[str, CheckFn]:
    return {}


def _alive() -> str | None:
    return None


@dataclass(slots=True)
class ProbeState:
    """What the probe routes report: ``liveness`` returns a failure reason or ``None``."""

    version: str
    registry: Collector
    checks: Checks = _no_checks
    liveness: Liveness = _alive
    readiness_timeout_s: float = 1.0


def container_checks(state: State) -> Checks:
    """Readiness checks of the container stored on ``state.container`` by the lifespan."""

    def checks() -> Mapping[str, CheckFn]:
        source: ReadinessSource = state.container
        return source.readiness_checks()

    return checks


def liveness_response(probes: ProbeState) -> JSONResponse:
    """200 ``{"status":"ok","version"}``, or 503 ``{"status":"unavailable","reason","version"}``."""
    reason = probes.liveness()
    if reason is None:
        return JSONResponse(liveness_body(probes.version))
    body = {"status": "unavailable", "reason": reason, "version": probes.version}
    return JSONResponse(body, status_code=_HTTP_UNAVAILABLE)


async def readiness_response(probes: ProbeState) -> JSONResponse:
    """The ``/readyz`` report of every check (200 when all pass, else 503)."""
    report = await run_checks(probes.checks(), timeout_s=probes.readiness_timeout_s, version=probes.version)
    return JSONResponse(report.body(), status_code=report.http_status)


def metrics_response(probes: ProbeState) -> Response:
    """The registry in the Prometheus text exposition format."""
    body, content_type = render_latest(probes.registry)
    return Response(content=body, media_type=content_type)


def _probes(request: Request) -> ProbeState:
    probes: ProbeState = request.app.state.probes
    return probes


async def _healthz(request: Request) -> Response:
    return liveness_response(_probes(request))


async def _readyz(request: Request) -> Response:
    return await readiness_response(_probes(request))


async def _metrics(request: Request) -> Response:
    return metrics_response(_probes(request))


def probe_routes() -> list[Route]:
    """``GET /healthz``, ``/readyz``, ``/metrics`` reading ``request.app.state.probes``."""
    return [
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/readyz", _readyz, methods=["GET"]),
        Route("/metrics", _metrics, methods=["GET"]),
    ]


def probe_app(lifespan: Callable[[Starlette], AbstractAsyncContextManager[None]] | None = None) -> Starlette:
    """A Starlette app serving only the probe routes (services whose port is a metrics port)."""
    return Starlette(routes=probe_routes(), lifespan=lifespan)


@contextlib.contextmanager
def observed(
    service_name: str, settings: BaseServiceSettings, *, version: str, enabled: bool = True
) -> Iterator[None]:
    """:func:`va_common.observability.init` on entry and ``shutdown()`` on exit (skipped when disabled)."""
    obs = observability.init(service_name, settings, version=version) if enabled else None
    try:
        yield
    finally:
        if obs is not None:
            obs.shutdown()


@contextlib.asynccontextmanager
async def service_scope[S: BaseServiceSettings, C](
    state: State,
    settings: S,
    container_factory: Callable[[S], AbstractAsyncContextManager[C]],
    *,
    version: str,
    init_observability: bool,
) -> AsyncIterator[C]:
    """Observability, then the dependency container stored on ``state.container``; both closed on exit."""
    with observed(settings.service_name, settings, version=version, enabled=init_observability):
        async with container_factory(settings) as container:
            state.container = container
            yield container


def service_lifespan[S: BaseServiceSettings, A: Starlette](
    settings: S,
    container_factory: Callable[[S], AbstractAsyncContextManager[object]],
    *,
    version: str,
    init_observability: bool,
) -> Callable[[A], AbstractAsyncContextManager[None]]:
    """A lifespan that only runs :func:`service_scope` (services without background tasks)."""

    @contextlib.asynccontextmanager
    async def lifespan(app: A) -> AsyncIterator[None]:
        async with service_scope(
            app.state, settings, container_factory, version=version, init_observability=init_observability
        ):
            yield

    return lifespan


def probe_service_app[S: BaseServiceSettings](  # noqa: PLR0913 - keyword-only wiring
    settings: S,
    container_factory: ContainerFactory[S],
    *,
    registry: Collector,
    version: str,
    init_observability: bool,
    readiness_timeout_s: float,
) -> Starlette:
    """Probe app of a service whose port is a metrics port and whose readiness is its container's checks."""
    app = probe_app(
        service_lifespan(settings, container_factory, version=version, init_observability=init_observability)
    )
    app.state.probes = ProbeState(
        version, registry, checks=container_checks(app.state), readiness_timeout_s=readiness_timeout_s
    )
    return app


class Supervisor:
    """Runs named background tasks; a task that raises is logged and reported to ``on_failure``."""

    def __init__(self, on_failure: Callable[[str, BaseException], None]) -> None:
        self._on_failure = on_failure
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, name: str, factory: TaskFactory) -> asyncio.Task[None]:
        if self.running(name):
            msg = f"task {name!r} is already running"
            raise ValueError(msg)
        task = asyncio.create_task(factory(), name=name)
        task.add_done_callback(self._finished)
        self._tasks[name] = task
        return task

    def running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            _log.info("service.task_exited", task=task.get_name())
            return
        _log.error("service.task_failed", task=task.get_name(), exc_info=exc)
        self._on_failure(task.get_name(), exc)

    async def aclose(self) -> None:
        """Cancel every task and wait for them to finish."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class StopController:
    """Process stop request: the first reason wins; a second request forces the exit."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._forced = asyncio.Event()
        self.reason: str | None = None
        self.failed = False

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

    @property
    def forced(self) -> bool:
        return self._forced.is_set()

    def request(self, reason: str, *, failure: bool = False) -> None:
        """Ask the process to stop; ``failure`` makes :func:`serve` exit non-zero."""
        self.failed = self.failed or failure
        if self.reason is None:
            self.reason = reason
            _log.info("service.stop_requested", reason=reason)
            self._stop.set()
            return
        _log.warning("service.stop_forced", reason=reason)
        self._forced.set()

    def task_failed(self, name: str, _exc: BaseException) -> None:
        """:class:`Supervisor` ``on_failure`` callback: stop the process with a failure exit code."""
        self.request(f"task_failed:{name}", failure=True)

    async def wait(self) -> None:
        await self._stop.wait()

    async def wait_forced(self) -> None:
        await self._forced.wait()


@contextlib.contextmanager
def shutdown_signals(stop: StopController) -> Iterator[None]:
    """Route SIGTERM/SIGINT to ``stop.request(<signal name>)`` for the duration of the block."""
    loop = asyncio.get_running_loop()
    for sig in SHUTDOWN_SIGNALS:
        loop.add_signal_handler(sig, stop.request, sig.name)
    try:
        yield
    finally:
        for sig in SHUTDOWN_SIGNALS:
            loop.remove_signal_handler(sig)


class ServerStartupError(RuntimeError):
    """uvicorn could not start (port in use, lifespan startup failure)."""


class ManagedServer(uvicorn.Server):
    """uvicorn server whose signals are owned by :func:`run_server` (no handler install, no re-raise)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _serve(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit as exc:  # uvicorn calls sys.exit() when startup fails
        msg = "server failed to start"
        raise ServerStartupError(msg) from exc


async def _drain_unless_forced(drain: Drain, stop: StopController) -> None:
    draining = asyncio.ensure_future(drain())
    forced = asyncio.ensure_future(stop.wait_forced())
    try:
        await asyncio.wait({draining, forced}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        draining.cancel()
        forced.cancel()
        await asyncio.gather(draining, forced, return_exceptions=True)
    if not draining.cancelled() and draining.exception() is not None:
        _log.error("service.drain_failed", exc_info=draining.exception())


async def _abort(server: uvicorn.Server, serving: asyncio.Future[None]) -> None:
    """Cancelled from outside: let uvicorn close its sockets (bounded) instead of abandoning them."""
    server.should_exit = server.force_exit = True
    await asyncio.wait({serving}, timeout=GRACEFUL_SHUTDOWN_TIMEOUT_S)


async def run_server(  # noqa: PLR0913 - keyword-only knobs and test seams
    app: Any,
    *,
    host: str,
    port: int,
    stop: StopController,
    drain: Drain | None = None,
    install_signals: bool = True,
    server_factory: ServerFactory = ManagedServer,
) -> None:
    """Serve ``app`` until ``stop`` is requested, run ``drain`` with the listener open, then shut down."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="on",
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )
    server = server_factory(config)
    with shutdown_signals(stop) if install_signals else contextlib.nullcontext():
        serving = asyncio.ensure_future(_serve(server))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if not serving.done():
                if drain is not None:
                    await _drain_unless_forced(drain, stop)
                server.force_exit = stop.forced
                server.should_exit = True
            await serving
        except asyncio.CancelledError:
            await _abort(server, serving)
            raise
        finally:
            serving.cancel()
            stopping.cancel()
            await asyncio.gather(serving, stopping, return_exceptions=True)


def run_main(main: TaskFactory) -> int:
    """``asyncio.run(main())``: 0 on a clean exit, 130 on ``KeyboardInterrupt``, 1 after logging a crash."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception:
        _log.exception("service.crashed")
        return EXIT_CRASHED
    return 0


@dataclass(frozen=True, slots=True)
class Bind:
    host: str
    port: int


def parse_bind(argv: Sequence[str] | None, *, default_port: int, description: str) -> Bind:
    """``--host`` (default ``0.0.0.0``) and ``--port`` (default ``default_port``) from ``argv``."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default=DEFAULT_HOST, help="interface to bind")
    parser.add_argument("--port", type=int, default=default_port, help="TCP port to bind")
    args = parser.parse_args(argv)
    return Bind(host=str(args.host), port=int(args.port))


def serve(app: Any, bind: Bind, *, stop: StopController | None = None, drain: Drain | None = None) -> int:
    """Run ``app`` on ``bind`` until SIGTERM/SIGINT; returns the exit code (1 after a task failure)."""
    controller = stop if stop is not None else StopController()

    async def main() -> None:
        await run_server(app, host=bind.host, port=bind.port, stop=controller, drain=drain)

    code = run_main(main)
    return EXIT_CRASHED if code == 0 and controller.failed else code
