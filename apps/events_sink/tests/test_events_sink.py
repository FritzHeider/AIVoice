"""Events sink settings, probes (specs/27-observability.md §9: Redis, ClickHouse), and entry point."""

from __future__ import annotations

import httpx
import pytest
import respx
from service_helpers import UNREACHABLE, FakeContainer, capture_serve, make_client, ok_check, settings_of

from va_common.service import Bind
from va_events_sink import __version__
from va_events_sink import main as sink_main
from va_events_sink.container import build_container
from va_events_sink.main import create_app
from va_events_sink.settings import EventsSinkSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(EventsSinkSettings)
    assert settings.service_name == "events-sink"
    assert settings.clickhouse_url == "http://default:@localhost:8123/va"
    assert settings.redis_url == "redis://localhost:6379/0"


async def test_probes_and_metrics() -> None:
    container = FakeContainer({"redis": ok_check, "clickhouse": ok_check})
    app = create_app(settings_of(EventsSinkSettings), container_factory=container, init_observability=False)
    async with make_client(app) as client:
        live, ready = await client.get("/healthz"), await client.get("/readyz")
        metrics = await client.get("/metrics")
    assert live.json() == {"status": "ok", "version": __version__}
    assert ready.json()["checks"] == {"redis": "ok", "clickhouse": "ok"}
    assert "# TYPE va_events_sink_inserted_total counter" in metrics.text


@respx.mock
async def test_readyz_real_container_clickhouse_ok_redis_unreachable() -> None:
    route = respx.get("http://clickhouse.test:8123/ping").mock(return_value=httpx.Response(200, text="Ok.\n"))
    settings = settings_of(
        EventsSinkSettings,
        redis_url=UNREACHABLE["redis_url"],
        clickhouse_url="http://default:secret@clickhouse.test:8123/va",
    )
    async with make_client(create_app(settings, init_observability=False)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"redis": "error", "clickhouse": "ok"}
    assert route.call_count == 1


async def test_readyz_real_container_clickhouse_unreachable() -> None:
    settings = settings_of(EventsSinkSettings, clickhouse_url=UNREACHABLE["clickhouse_url"])
    async with make_client(create_app(settings, init_observability=False)) as client:
        response = await client.get("/readyz")
    assert "clickhouse" in response.json()["failing"]


async def test_build_container_rejects_native_clickhouse_url() -> None:
    with pytest.raises(ValueError, match="http"):
        async with build_container(settings_of(EventsSinkSettings, clickhouse_url="clickhouse://ch:9000/va")):
            pass


def test_main_parses_args_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, sink_main)
    assert sink_main.main(["--port", "19500"]) == 0
    assert [bind for _, bind, _ in seen] == [Bind("0.0.0.0", 19500)]  # noqa: S104
