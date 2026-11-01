"""Shared test helpers for tracing-aware tests: settings subclasses and an in-memory tracer provider."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from va_common.settings import RedisSettings
from va_common.tracing import setup_tracing


class EdgeSettings(RedisSettings):
    service_name: ClassVar[str] = "telephony_edge"


def make_settings(**overrides: Any) -> EdgeSettings:
    values: dict[str, Any] = {"_env_file": None, "log_format": "json", **overrides}
    return EdgeSettings(**values)


def memory_provider(ratio: float = 1.0, **kwargs: Any) -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = setup_tracing(
        "telephony_edge",
        make_settings(otel_traces_sampler_arg=ratio),
        span_processor=SimpleSpanProcessor(exporter),
        set_global=False,
        **kwargs,
    )
    return provider, exporter


def vendor_metric_rows(prefix: str) -> set[tuple[str, str, tuple[str, ...]]]:
    """``(metric, type, labels)`` of the specs/27 §3.6 table rows whose metric name starts with ``prefix``."""
    spec = Path(__file__).resolve().parents[3] / "specs" / "27-observability.md"
    section = spec.read_text(encoding="utf-8").split("### 3.6 ", 1)[1].split("\n## ", 1)[0]
    rows = set()
    for line in section.splitlines():
        if line.startswith(f"| `{prefix}"):
            metric, kind, labels = (cell.strip() for cell in line.strip("|").split("|"))
            rows.add((metric.strip("`"), kind.split()[0], tuple(re.findall(r"`([^`]+)`", labels))))
    return rows
