"""Simulator runner: settings, per-simulator probes, and the four-server run loop."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import httpx
import pytest
import uvicorn
from service_helpers import UNREACHABLE, FakeContainer, RecordingServer, make_client, ok_check, settings_of

from va_common.service import StopController, run_server
from va_sim.server import build_state
from va_sim.settings import SimName, SimSettings
from va_sim.state import SimState
from va_sim_server import __version__
from va_sim_server import main as sim_main
from va_sim_server.main import SIMULATORS, build_container, create_app, ports_from, run


def _state() -> SimState:
    return build_state(settings_of(SimSettings, sim_clock="accelerated"))


def test_defaults_match_spec() -> None:
    settings = settings_of(SimSettings)
    assert settings.service_name == "sim"
    assert (settings.sim_clock, settings.sim_state_redis_url, settings.sim_workers) == ("realtime", "", 1)


def test_ports_follow_spec_layout() -> None:
    assert ports_from(9100) == {"telnyx": 9100, "elevenlabs": 9200, "llm": 9300, "stripe": 9400}
    assert tuple(ports_from(19100)) == SIMULATORS


async def test_probe_app_without_state_redis_is_ready() -> None:
    async with (
        build_container(settings_of(SimSettings)) as deps,
        make_client(create_app(deps, "telnyx", _state())) as client,
    ):
        ready = await client.get("/readyz")
        live = await client.get("/healthz")
        metrics = await client.get("/metrics")
    assert ready.json() == {"status": "ok", "checks": {}, "version": __version__}
    assert live.status_code == 200
    assert "# TYPE python_info gauge" in metrics.text


async def test_probe_app_checks_state_redis_when_configured() -> None:
    settings = settings_of(SimSettings, sim_state_redis_url=UNREACHABLE["redis_url"])
    async with build_container(settings) as deps, make_client(create_app(deps, "llm", _state())) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["redis"]


@pytest.mark.parametrize("sim", SIMULATORS)
async def test_each_app_serves_the_control_api_next_to_probes(sim: SimName) -> None:
    async with build_container(settings_of(SimSettings)) as deps:
        app = create_app(deps, sim, _state())
        async with make_client(app) as client:
            clock = await client.get("/sim/clock")
            ready = await client.get("/readyz")
            scenarios = await client.get("/sim/telnyx/scenarios")
    assert clock.json()["mode"] == "accelerated"
    assert ready.status_code == 200
    assert scenarios.status_code == (200 if sim == "telnyx" else 404)


async def test_run_serves_all_simulators_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    servers: list[RecordingServer] = []
    all_created = asyncio.Event()

    def factory(config: uvicorn.Config) -> RecordingServer:
        servers.append(RecordingServer(config))
        if len(servers) == len(SIMULATORS):
            all_created.set()
        return servers[-1]

    monkeypatch.setattr(sim_main, "run_server", partial(run_server, server_factory=factory))
    stop = StopController()
    container = FakeContainer({"redis": ok_check})
    task = asyncio.ensure_future(
        run(
            settings_of(SimSettings),
            stop=stop,
            host="127.0.0.1",
            ports=dict.fromkeys(SIMULATORS, 0),
            container_factory=container,
            init_observability=False,
            install_signals=False,
        )
    )
    await asyncio.wait_for(all_created.wait(), timeout=10)
    async with httpx.AsyncClient() as client:
        for server in servers:
            await asyncio.wait_for(server.ready.wait(), timeout=10)
            response = await client.get(f"{server.url}/readyz")
            assert response.json()["checks"] == {"redis": "ok"}
        registered = await client.post(
            f"{servers[0].url}/sim/telnyx/scenarios",
            content=b"id: shared\nmatch: {to: '+14155550101'}\nanswer: {after_ms: 0}\n",
            headers={"content-type": "application/yaml"},
        )
        assert registered.status_code == 201
        # All four servers share one SimState.
        snapshot = await client.get(f"{servers[-1].url}/sim/state")
        assert snapshot.json()["scenarios"] == ["shared"]
    stop.request("test")
    await asyncio.wait_for(task, timeout=10)
    assert container.closed == 1


def test_main_runs_with_base_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_run(settings: SimSettings, **kwargs: Any) -> None:
        seen.append({"settings": settings, **kwargs})

    monkeypatch.setattr(sim_main, "run", fake_run)
    assert sim_main.main(["--port", "19100", "--host", "127.0.0.1"]) == 0
    [call] = seen
    assert call["host"] == "127.0.0.1"
    assert call["ports"] == ports_from(19100)
    assert isinstance(call["stop"], StopController)
