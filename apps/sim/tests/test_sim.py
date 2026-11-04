"""Simulator runner: settings, per-simulator probes, and the four-server run loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fakeredis import FakeAsyncRedis, FakeServer
from service_helpers import UNREACHABLE, FakeContainer, RecordingServer, make_client, ok_check, settings_of

from va_common.checks import open_dependencies
from va_common.service import ContainerFactory, StopController, run_server
from va_sim.scenario import load_scenario
from va_sim.server import build_state
from va_sim.settings import SimName, SimSettings
from va_sim.shared import SharedState
from va_sim.state import SimState
from va_sim_server import __version__
from va_sim_server import main as sim_main
from va_sim_server.main import SIMULATORS, SimContainer, build_container, create_app, ports_from, run

SCENARIO = b"id: shared\nmatch: {to: '+14155550101'}\nanswer: {after_ms: 0}\n"
YAML = {"content-type": "application/yaml"}


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


@asynccontextmanager
async def serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    container: ContainerFactory[SimSettings],
    settings: SimSettings | None = None,
) -> AsyncIterator[list[RecordingServer]]:
    """``run`` on ephemeral ports until the block ends; yields the four ready servers (SIMULATORS order)."""
    servers: list[RecordingServer] = []
    all_created = asyncio.Event()

    def factory(config: uvicorn.Config) -> RecordingServer:
        servers.append(RecordingServer(config))
        if len(servers) == len(SIMULATORS):
            all_created.set()
        return servers[-1]

    monkeypatch.setattr(sim_main, "run_server", partial(run_server, server_factory=factory))
    stop = StopController()
    task = asyncio.ensure_future(
        run(
            settings if settings is not None else settings_of(SimSettings),
            stop=stop,
            host="127.0.0.1",
            ports=dict.fromkeys(SIMULATORS, 0),
            container_factory=container,
            init_observability=False,
            install_signals=False,
            public_key_file=tmp_path / ".sim" / "telnyx_public_key",
        )
    )
    await asyncio.wait_for(all_created.wait(), timeout=10)
    for server in servers:
        await asyncio.wait_for(server.ready.wait(), timeout=10)
    yield servers
    stop.request("test")
    await asyncio.wait_for(task, timeout=10)


async def test_run_serves_all_simulators_until_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    container = FakeContainer({"redis": ok_check})
    key_file = tmp_path / ".sim" / "telnyx_public_key"
    async with serving(monkeypatch, tmp_path, container) as servers, httpx.AsyncClient() as client:
        for server in servers:
            response = await client.get(f"{server.url}/readyz")
            assert response.json()["checks"] == {"redis": "ok"}
        registered = await client.post(
            f"{servers[0].url}/sim/telnyx/scenarios", content=SCENARIO, headers=YAML
        )
        assert registered.status_code == 201
        # All four servers share one SimState.
        snapshot = await client.get(f"{servers[-1].url}/sim/state")
        assert snapshot.json()["scenarios"] == ["shared"]
        # FakeTelnyx serves its REST API with the simulator key and signs with the key written at startup.
        balance = await client.get(
            f"{servers[0].url}/v2/balance", headers={"authorization": "Bearer sim-telnyx-key"}
        )
        assert balance.json()["data"]["record_type"] == "balance"
        public_key = await client.get(f"{servers[0].url}/sim/telnyx/public-key")
        assert key_file.read_text(encoding="utf-8") == f"{public_key.json()['public_key']}\n"
    assert container.closed == 1


async def test_each_run_starts_from_a_fresh_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seeds: list[int] = []
    for _ in range(2):
        container = FakeContainer({"redis": ok_check})
        async with serving(monkeypatch, tmp_path, container) as servers, httpx.AsyncClient() as client:
            seeds.append((await client.get(f"{servers[0].url}/sim/state")).json()["seed"])
    assert seeds[0] != seeds[1]


async def test_run_shares_the_control_plane_through_the_state_redis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    other = FakeAsyncRedis(server=server)
    peer = _state()

    @asynccontextmanager
    async def container(_settings: SimSettings) -> AsyncIterator[SimContainer]:
        async with open_dependencies() as deps:
            yield SimContainer(deps, redis)

    try:
        await peer.attach_shared(SharedState(other))
        await peer.add_scenario(load_scenario(SCENARIO.decode()))
        async with serving(monkeypatch, tmp_path, container) as servers, httpx.AsyncClient() as client:
            # The process adopted the scenario another process registered before it started.
            listing = await client.get(f"{servers[0].url}/sim/telnyx/scenarios")
            assert [item["id"] for item in listing.json()["scenarios"]] == ["shared"]
            reset = await client.post(f"{servers[1].url}/sim/reset", json={"seed": 9})
            assert reset.status_code == 204
            await peer.sync()
            assert (peer.seed, peer.scenarios) == (9, {})
    finally:
        await peer.aclose()
        await redis.aclose()
        await other.aclose()


async def test_build_container_exposes_the_state_redis() -> None:
    async with build_container(settings_of(SimSettings)) as plain:
        assert plain.state_redis is None
    settings = settings_of(SimSettings, sim_state_redis_url="redis://localhost:6379/15")
    async with build_container(settings) as shared:
        assert shared.state_redis is not None
        assert set(shared.readiness_checks()) == {"redis"}


def test_main_refuses_several_workers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SIM_WORKERS", "2")
    assert sim_main.main(["--port", "19100"]) == sim_main.EXIT_UNSUPPORTED == 2
    assert "SIM_WORKERS > 1 is not supported yet" in capsys.readouterr().err


def test_main_runs_with_base_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_run(settings: SimSettings, **kwargs: Any) -> None:
        seen.append({"settings": settings, "loop": type(asyncio.get_running_loop()).__module__, **kwargs})

    tuned: list[tuple[int, int, int]] = []
    monkeypatch.setattr(sim_main, "run", fake_run)
    monkeypatch.setattr(sim_main, "tune_gc", tuned.append)
    assert sim_main.main(["--port", "19100", "--host", "127.0.0.1"]) == 0
    assert tuned == [sim_main.GC_THRESHOLDS]
    [call] = seen
    assert call["loop"] == "uvloop"
    assert call["host"] == "127.0.0.1"
    assert call["ports"] == ports_from(19100)
    assert isinstance(call["stop"], StopController)
