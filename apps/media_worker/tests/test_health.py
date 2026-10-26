"""Media probes (specs/27-observability.md §9): lag liveness; draining, capacity, redis, vad readiness."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from media_helpers import FakeMediaContainer, media_app
from service_helpers import UNREACHABLE, failing, json_log_stream, log_events, make_client, settings_of

from va_common import observability
from va_media import __version__
from va_media.drain import DRAINING_PODS_KEY
from va_media.loop_lag import LoopLagMonitor
from va_media.main import MediaState, create_app
from va_media.settings import MediaSettings


def media_state(app: Any) -> MediaState:
    state: MediaState = app.state.media
    return state


async def test_healthz_ok_and_lag_failure(models_dir: Path) -> None:
    monitor = LoopLagMonitor(interval_s=3600)
    async with make_client(media_app(models_dir, monitor=monitor)) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok", "version": __version__}
        monitor.record(0.75)
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "reason": "event_loop_lag", "version": __version__}


async def test_readyz_all_ok_in_spec_order(models_dir: Path) -> None:
    async with make_client(media_app(models_dir)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert list(response.json()["checks"]) == ["draining", "capacity", "redis", "vad"]


async def test_readyz_vad_error_when_model_missing(tmp_path: Path) -> None:
    stream = json_log_stream()
    async with make_client(media_app(tmp_path / "absent")) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["vad"]
    assert any(e["event"] == "media.vad_model_unavailable" for e in log_events(stream))


async def test_readyz_capacity_fails_when_full(models_dir: Path) -> None:
    app = media_app(models_dir, media_max_calls_per_pod=1)
    async with make_client(app) as client:
        assert media_state(app).registry.admit("call-1")
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["capacity"]


async def test_readyz_draining_fails_after_begin(models_dir: Path) -> None:
    container = FakeMediaContainer()
    app = media_app(models_dir, container)
    async with make_client(app) as client:
        await media_state(app).drain.begin(container.redis, "pod-test")
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["draining"]


async def test_readyz_redis_failure(models_dir: Path) -> None:
    container = FakeMediaContainer({"redis": failing(ConnectionError("refused"))})
    async with make_client(media_app(models_dir, container)) as client:
        response = await client.get("/readyz")
    assert response.json()["failing"] == ["redis"]


async def test_readyz_real_container_redis_unreachable(models_dir: Path) -> None:
    settings = settings_of(MediaSettings, redis_url=UNREACHABLE["redis_url"])
    app = create_app(settings, init_observability=False, models_dir=models_dir)
    async with make_client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["redis"]


async def test_metrics_exposes_media_catalog(models_dir: Path) -> None:
    async with make_client(media_app(models_dir)) as client:
        text = (await client.get("/metrics")).text
    assert "# TYPE va_media_active_calls gauge" in text
    assert "va_media_draining 0.0" in text


async def test_shutdown_clears_drain_marker_and_closes_container(models_dir: Path) -> None:
    container = FakeMediaContainer()
    await container.redis.sadd(DRAINING_PODS_KEY, "pod-test")
    async with make_client(media_app(models_dir, container)):
        assert (container.opened, container.closed) == (1, 0)
    assert container.closed == 1
    assert await container.redis.smembers(DRAINING_PODS_KEY) == set()


class CrashingMonitor(LoopLagMonitor):
    async def run(self) -> None:
        msg = "ticker crashed"
        raise RuntimeError(msg)


async def test_crashed_lag_monitor_requests_failure_stop(models_dir: Path) -> None:
    app = media_app(models_dir, monitor=CrashingMonitor())
    async with make_client(app):
        await asyncio.wait_for(app.state.stop.wait(), timeout=5)
    assert (app.state.stop.reason, app.state.stop.failed) == ("task_failed:loop_lag", True)


async def test_observability_initialised_as_media_worker(
    models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []

    class Obs:
        def shutdown(self) -> None:
            names.append("shutdown")

    def fake_init(name: str, _settings: object, **_kwargs: Any) -> Obs:
        names.append(name)
        return Obs()

    monkeypatch.setattr(observability, "init", fake_init)
    app = create_app(
        settings_of(MediaSettings), container_factory=FakeMediaContainer(), models_dir=models_dir
    )
    async with make_client(app):
        pass
    assert names == ["media-worker", "shutdown"]
