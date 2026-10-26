from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from va_common.metrics import (
    CATALOG,
    LAT_MS,
    MetricKind,
    MetricSpec,
    Service,
    build_metrics,
    metrics_app,
    new_registry,
    render_latest,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "27-observability.md"
_SECTION = re.compile(r"^### 3\.(?P<n>[1-5]) ")
_TICKED = re.compile(r"`([^`]+)`")
_SCOPED = re.compile(r"\((?P<which>\w+) only\)")
_SECTION_SERVICES = {
    "1": Service.API,
    "2": Service.TELEPHONY_EDGE,
    "3": Service.MEDIA_WORKER,
    "4": Service.WORKER,
}


def _service_for(section: str, name: str) -> Service:
    if section != "5":
        return _SECTION_SERVICES[section]
    return Service.WEBHOOK_DISPATCHER if name.startswith("va_webhook_") else Service.EVENTS_SINK


def _labels(cell: str, name: str, names: list[str]) -> tuple[str, ...]:
    if cell.startswith("—"):
        return ()
    scoped = _SCOPED.search(cell)
    if scoped and len(names) > 1 and scoped.group("which") not in name:
        return ()
    return tuple(_TICKED.findall(cell))


def _kind(cell: str) -> MetricKind:
    return MetricKind(cell.split(maxsplit=1)[0])


def parse_spec_catalog() -> dict[Service, set[MetricSpec]]:
    catalog: dict[Service, set[MetricSpec]] = {}
    section = ""
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        heading = _SECTION.match(line)
        if heading:
            section = heading.group("n")
            continue
        if line.startswith("## ") or not section or not line.startswith("| `va_"):
            if line.startswith("## "):
                section = ""
            continue
        metric_cell, type_cell, label_cell = (cell.strip() for cell in line.strip("|").split("|"))
        names = _TICKED.findall(metric_cell)
        for name in names:
            spec = MetricSpec(name, _kind(type_cell), "", _labels(label_cell, name, names))
            catalog.setdefault(_service_for(section, name), set()).add(spec)
    return catalog


def _shape(specs: tuple[MetricSpec, ...] | set[MetricSpec]) -> set[tuple[str, MetricKind, tuple[str, ...]]]:
    return {(spec.name, spec.kind, spec.labels) for spec in specs}


def test_spec_parser_sees_every_section() -> None:
    parsed = parse_spec_catalog()
    assert set(parsed) == set(Service)
    assert sum(len(specs) for specs in parsed.values()) == 56


@pytest.mark.parametrize("service", list(Service))
def test_catalog_matches_spec_tables_exactly(service: Service) -> None:
    assert _shape(CATALOG[service]) == _shape(parse_spec_catalog()[service])


def test_scoped_and_combined_rows() -> None:
    media = {spec.name: spec for spec in CATALOG[Service.MEDIA_WORKER]}
    assert media["va_media_calls_started_total"].labels == ()
    assert media["va_media_calls_ended_total"].labels == ("end_reason",)
    worker = {spec.name: spec for spec in CATALOG[Service.WORKER]}
    assert worker["va_workflow_failed_total"].labels == ("workflow",)


def test_catalog_names_unique_and_documented() -> None:
    names = [spec.name for specs in CATALOG.values() for spec in specs]
    assert len(names) == len(set(names))
    assert all(spec.doc for specs in CATALOG.values() for spec in specs)


@pytest.mark.parametrize("service", list(Service))
def test_build_metrics_registers_whole_catalog(service: Service) -> None:
    metrics = build_metrics(service, new_registry(process_collectors=False))
    body, _ = render_latest(metrics.registry)
    text = body.decode()
    for spec in CATALOG[service]:
        assert f"# TYPE {spec.name} {spec.kind.value}" in text


def test_histograms_use_lat_ms_buckets() -> None:
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    metrics.histogram("va_turn_eot_ms").observe(42)
    samples = metrics.registry.collect()
    buckets = [
        float(sample.labels["le"])
        for family in samples
        if family.name == "va_turn_eot_ms"
        for sample in family.samples
        if sample.name.endswith("_bucket") and sample.labels["le"] != "+Inf"
    ]
    assert tuple(buckets) == tuple(float(b) for b in LAT_MS)


def test_accessors_return_typed_metrics() -> None:
    metrics = build_metrics(Service.API, new_registry(process_collectors=False))
    assert isinstance(metrics.counter("va_http_requests_total"), Counter)
    assert isinstance(metrics.gauge("va_sse_connections"), Gauge)
    assert isinstance(metrics.histogram("va_http_request_duration_ms"), Histogram)


def test_accessor_errors_name_the_metric() -> None:
    metrics = build_metrics(Service.API, new_registry(process_collectors=False))
    with pytest.raises(KeyError, match="va_turn_e2e_ms"):
        metrics.histogram("va_turn_e2e_ms")
    with pytest.raises(TypeError, match=r"va_sse_connections.*gauge, not a counter"):
        metrics.counter("va_sse_connections")


def test_services_get_independent_registries() -> None:
    first = build_metrics(Service.API)
    second = build_metrics(Service.API)
    assert first.registry is not second.registry
    first.counter("va_idempotent_replays_total").inc()
    assert first.registry.get_sample_value("va_idempotent_replays_total") == 1.0
    assert second.registry.get_sample_value("va_idempotent_replays_total") == 0.0


def test_new_registry_includes_process_collectors() -> None:
    text = render_latest(new_registry())[0].decode()
    assert "python_info" in text
    assert "python_gc_objects_collected_total" in text
    assert "python_info" not in render_latest(new_registry(process_collectors=False))[0].decode()


def test_explicit_registry_is_used() -> None:
    registry = CollectorRegistry()
    metrics = build_metrics(Service.EVENTS_SINK, registry)
    assert metrics.registry is registry
    assert metrics.service is Service.EVENTS_SINK


async def test_metrics_app_serves_observed_values() -> None:
    metrics = build_metrics(Service.WEBHOOK_DISPATCHER, new_registry(process_collectors=False))
    metrics.counter("va_webhook_deliveries_total").labels(result="succeeded", format="json").inc(3)
    transport = httpx.ASGITransport(app=metrics_app(metrics.registry))
    async with httpx.AsyncClient(transport=transport, base_url="http://metrics") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'va_webhook_deliveries_total{format="json",result="succeeded"} 3.0' in response.text


def test_render_latest_content_type() -> None:
    _, content_type = render_latest(new_registry(process_collectors=False))
    assert content_type.startswith("text/plain; version=")
