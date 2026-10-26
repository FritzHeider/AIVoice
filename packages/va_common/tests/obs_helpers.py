"""Shared test helpers for tracing-aware tests: settings subclasses and an in-memory tracer provider."""

from __future__ import annotations

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
