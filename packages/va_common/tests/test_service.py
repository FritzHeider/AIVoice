"""Shared service runtime: probe routes, supervision, stop control, and the uvicorn runner."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from service_helpers import (
    FakeContainer,
    RecordingServer,
    failing,
    json_log_stream,
    log_events,
    make_client,
    ok_check,
    settings_of,
)
from starlette.applications import Starlette
from starlette.datastructures import State

from va_common import observability, service
from va_common.metrics import Service, build_metrics, new_registry
from va_common.service import (
    EXIT_CRASHED,
    EXIT_INTERRUPTED,
    Bind,
    ProbeState,
    ServerStartupError,
    StopController,
    Supervisor,
    container_checks,
    observed,
    parse_bind,
    probe_app,
    run_main,
    run_server,
    serve,
    service_scope,
    shutdown_signals,
)
from va_common.settings import BaseServiceSettings

type Lifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def no_lifespan(_app: Starlette) -> AsyncIterator[None]:
    yield


def app_with(probes: ProbeState, lifespan: Lifespan = no_lifespan) -> Starlette:
    app = probe_app(lifespan)
    app.state.probes = probes
    return app


def probes(**kwargs: Any) -> ProbeState:
    return ProbeState("9.9.9", new_registry(process_collectors=False), **kwargs)


async def test_liveness_ok() -> None:
    async with make_client(app_with(probes())) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "9.9.9"}


async def test_liveness_failure_503_with_reason() -> None:
    async with make_client(app_with(probes(liveness=lambda: "event_loop_lag"))) as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "reason": "event_loop_lag", "version": "9.9.9"}


async def test_readiness_without_checks_is_ok() -> None:
    async with make_client(app_with(probes())) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {}, "version": "9.9.9"}


async def test_readiness_ok_and_503() -> None:
    checks = {"redis": ok_check, "postgres": failing(ConnectionError("down"))}
    async with make_client(app_with(probes(checks=lambda: checks))) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"redis": "ok", "postgres": "error"},
        "failing": ["postgres"],
        "version": "9.9.9",
    }


async def test_readiness_timeout_budget() -> None:
    async def hangs() -> None:
        await asyncio.Event().wait()

    state = probes(checks=lambda: {"clickhouse": hangs}, readiness_timeout_s=0.01)
    async with make_client(app_with(state)) as client:
        response = await client.get("/readyz")
    assert response.json()["checks"] == {"clickhouse": "timeout"}


async def test_metrics_route_renders_registry() -> None:
    metrics = build_metrics(Service.EVENTS_SINK, new_registry(process_collectors=False))
    metrics.counter("va_events_sink_inserted_total").labels(table="call_events").inc(3)
    async with make_client(app_with(ProbeState("1", metrics.registry))) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'va_events_sink_inserted_total{table="call_events"} 3.0' in response.text


async def test_unknown_path_404_and_post_405() -> None:
    async with make_client(app_with(probes())) as client:
        assert (await client.get("/nope")).status_code == 404
        assert (await client.post("/healthz")).status_code == 405


def test_container_checks_reads_state_container() -> None:
    state = State()
    state.container = FakeContainer({"redis": ok_check})
    assert container_checks(state)() == {"redis": ok_check}


class FakeObservability:
    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_observed_initialises_and_shuts_down(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    obs = FakeObservability()

    def fake_init(name: str, _settings: object, **kwargs: Any) -> FakeObservability:
        calls.append((name, kwargs))
        return obs

    monkeypatch.setattr(observability, "init", fake_init)
    settings = settings_of(BaseServiceSettings)
    with pytest.raises(RuntimeError, match="boom"), observed("svc", settings, version="1.0"):
        assert calls == [("svc", {"version": "1.0"})]
        raise RuntimeError("boom")
    assert obs.shutdowns == 1


def test_observed_disabled_skips_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "init", failing(AssertionError("must not init")))
    with observed("svc", settings_of(BaseServiceSettings), version="1", enabled=False):
        pass


async def test_service_scope_stores_and_closes_container() -> None:
    state, container = State(), FakeContainer()
    settings = settings_of(BaseServiceSettings)
    async with service_scope(state, settings, container, version="1", init_observability=False) as opened:
        assert opened is container
        assert state.container is container
        assert (container.opened, container.closed) == (1, 0)
    assert container.closed == 1


class FailureLog:
    def __init__(self) -> None:
        self.failures: list[tuple[str, BaseException]] = []

    def __call__(self, name: str, exc: BaseException) -> None:
        self.failures.append((name, exc))


async def test_supervisor_failure_calls_on_failure_and_logs(reset_logging: None) -> None:
    stream = json_log_stream()
    failures = FailureLog()
    supervisor = Supervisor(failures)
    error = RuntimeError("worker died")

    async def dies() -> None:
        raise error

    task = supervisor.start("temporal", dies)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration
    assert failures.failures == [("temporal", error)]
    assert not supervisor.running("temporal")
    event = next(e for e in log_events(stream) if e["event"] == "service.task_failed")
    assert event["task"] == "temporal"
    assert "worker died" in event["exception"]
    await supervisor.aclose()


async def test_supervisor_normal_exit_logged_without_failure(reset_logging: None) -> None:
    stream = json_log_stream()
    failures = FailureLog()
    supervisor = Supervisor(failures)
    await supervisor.start("short", ok_check)
    await asyncio.sleep(0)
    assert failures.failures == []
    assert [e["task"] for e in log_events(stream) if e["event"] == "service.task_exited"] == ["short"]


async def test_supervisor_aclose_cancels_running_tasks() -> None:
    failures = FailureLog()
    supervisor = Supervisor(failures)
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = supervisor.start("loop", forever)
    await started.wait()
    assert supervisor.running("loop")
    with pytest.raises(ValueError, match="already running"):
        supervisor.start("loop", forever)
    await supervisor.aclose()
    assert task.cancelled()
    assert failures.failures == []


def test_stop_controller_keeps_first_reason_and_second_forces() -> None:
    stop = StopController()
    assert not stop.requested
    stop.request("SIGTERM")
    assert (stop.requested, stop.reason, stop.forced, stop.failed) == (True, "SIGTERM", False, False)
    stop.request("SIGINT")
    assert (stop.reason, stop.forced) == ("SIGTERM", True)


def test_stop_controller_task_failed_marks_failure() -> None:
    stop = StopController()
    stop.task_failed("temporal", RuntimeError("x"))
    assert (stop.reason, stop.failed) == ("task_failed:temporal", True)


async def test_shutdown_signals_route_sigterm_and_are_removed() -> None:
    stop = StopController()
    with shutdown_signals(stop):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
    assert stop.reason == "SIGTERM"
    loop = asyncio.get_running_loop()
    assert not loop.remove_signal_handler(signal.SIGTERM)
    assert not loop.remove_signal_handler(signal.SIGINT)


class Harness:
    """Runs :func:`run_server` on an ephemeral port and records lifespan events."""

    def __init__(self, probes_state: ProbeState | None = None) -> None:
        self.events: list[str] = []
        self.server: RecordingServer | None = None
        self.stop = StopController()
        self.created = asyncio.Event()

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            self.events.append("startup")
            yield
            self.events.append("shutdown")

        self.app = app_with(probes_state or probes(), lifespan)

    def factory(self, config: uvicorn.Config) -> RecordingServer:
        self.server = RecordingServer(config)
        self.created.set()
        return self.server

    def start(self, **kwargs: Any) -> asyncio.Task[None]:
        coro = run_server(
            self.app, host="127.0.0.1", port=0, stop=self.stop, server_factory=self.factory, **kwargs
        )
        return asyncio.ensure_future(coro)

    async def ready(self) -> RecordingServer:
        await asyncio.wait_for(self.created.wait(), timeout=10)
        assert self.server is not None
        await asyncio.wait_for(self.server.ready.wait(), timeout=10)
        return self.server


async def get(url: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url)


async def test_run_server_serves_and_stops_on_request() -> None:
    harness = Harness()
    task = harness.start(install_signals=False)
    server = await harness.ready()
    assert (await get(f"{server.url}/healthz")).json() == {"status": "ok", "version": "9.9.9"}
    harness.stop.request("test")
    await asyncio.wait_for(task, timeout=10)
    assert harness.events == ["startup", "shutdown"]


async def test_run_server_runs_drain_with_listener_open_before_exit() -> None:
    harness = Harness()

    async def drain() -> None:
        assert harness.server is not None
        response = await get(f"{harness.server.url}/healthz")
        harness.events.append(f"drain:{response.status_code}")

    task = harness.start(install_signals=False, drain=drain)
    await harness.ready()
    harness.stop.request("test")
    await asyncio.wait_for(task, timeout=10)
    assert harness.events == ["startup", "drain:200", "shutdown"]


async def test_run_server_second_request_skips_a_hanging_drain() -> None:
    harness = Harness()
    drain_started = asyncio.Event()

    async def drain() -> None:
        drain_started.set()
        await asyncio.Event().wait()

    task = harness.start(install_signals=False, drain=drain)
    await harness.ready()
    harness.stop.request("SIGTERM")
    await drain_started.wait()
    harness.stop.request("SIGINT")
    await asyncio.wait_for(task, timeout=10)
    assert harness.stop.forced


async def test_run_server_logs_failed_drain_and_still_stops(reset_logging: None) -> None:
    stream = json_log_stream()
    harness = Harness()
    task = harness.start(install_signals=False, drain=failing(RuntimeError("drain broke")))
    await harness.ready()
    harness.stop.request("test")
    await asyncio.wait_for(task, timeout=10)
    assert harness.events == ["startup", "shutdown"]
    assert any(e["event"] == "service.drain_failed" for e in log_events(stream))


async def test_run_server_signal_handler_requests_stop() -> None:
    harness = Harness()
    task = harness.start()
    await harness.ready()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10)
    assert harness.stop.reason == "SIGTERM"
    assert not asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


async def test_run_server_startup_failure_raises() -> None:
    @asynccontextmanager
    async def broken(_app: Starlette) -> AsyncIterator[None]:
        await failing(RuntimeError("lifespan cannot start"))()
        yield

    with pytest.raises(ServerStartupError, match="failed to start"):
        await run_server(
            app_with(probes(), broken), host="127.0.0.1", port=0, stop=StopController(), install_signals=False
        )


async def test_run_server_cancellation_stops_serving() -> None:
    harness = Harness()
    task = harness.start(install_signals=False)
    await harness.ready()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_run_main_exit_codes(reset_logging: None) -> None:
    stream = json_log_stream()

    async def crashes() -> None:
        raise RuntimeError("fatal")

    async def interrupted() -> None:
        raise KeyboardInterrupt

    assert run_main(ok_check) == 0
    assert run_main(crashes) == EXIT_CRASHED
    assert run_main(interrupted) == EXIT_INTERRUPTED
    assert [e["event"] for e in log_events(stream)].count("service.crashed") == 1


def test_parse_bind_defaults_and_overrides() -> None:
    assert parse_bind([], default_port=8100, description="d") == Bind("0.0.0.0", 8100)  # noqa: S104
    assert parse_bind(["--host", "127.0.0.1", "--port", "9"], default_port=1, description="d") == Bind(
        "127.0.0.1", 9
    )


@pytest.mark.parametrize(("failure", "expected"), [(False, 0), (True, EXIT_CRASHED)])
def test_serve_runs_server_and_reports_task_failure(
    monkeypatch: pytest.MonkeyPatch, failure: bool, expected: int
) -> None:
    seen: list[tuple[object, str, int]] = []

    async def fake_run_server(app: object, *, host: str, port: int, stop: StopController, **_: Any) -> None:
        seen.append((app, host, port))
        stop.request("done", failure=failure)

    monkeypatch.setattr(service, "run_server", fake_run_server)
    app = object()
    assert serve(app, Bind("127.0.0.1", 8123)) == expected
    assert seen == [(app, "127.0.0.1", 8123)]
