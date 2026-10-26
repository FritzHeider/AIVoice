"""OpenTelemetry tracing setup (specs/27-observability.md §2).

:func:`setup_tracing` builds the service ``TracerProvider``: resource attributes, the OTLP gRPC exporter
behind a ``BatchSpanProcessor``, and the sampler ``ParentBased(ForceSample(TraceIdRatioBased(ratio)))``.
Calls are always sampled: their root span carries ``va.force_sample=True`` (see
:func:`force_sampled_attributes`).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping, Sequence
from typing import Final

from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes, AttributeValue

from va_common.settings import BaseServiceSettings

__all__ = [
    "FORCE_SAMPLE_ATTRIBUTE",
    "OTLP_EXPORT_TIMEOUT_S",
    "ForceSampleSampler",
    "build_resource",
    "build_sampler",
    "force_sampled_attributes",
    "resolve_pod",
    "setup_tracing",
    "shutdown_tracing",
]

FORCE_SAMPLE_ATTRIBUTE: Final = "va.force_sample"
OTLP_EXPORT_TIMEOUT_S: Final = 10


class ForceSampleSampler(Sampler):
    """Sample spans whose start attributes carry ``va.force_sample=True``; delegate everything else."""

    def __init__(self, delegate: Sampler) -> None:
        self.delegate = delegate

    def should_sample(  # noqa: PLR0913, PLR0917 - signature fixed by the OTel Sampler interface
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if attributes and attributes.get(FORCE_SAMPLE_ATTRIBUTE) is True:
            parent_state = trace.get_current_span(parent_context).get_span_context().trace_state
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes, trace_state or parent_state)
        return self.delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"ForceSample{{{self.delegate.get_description()}}}"


def build_sampler(ratio: float) -> Sampler:
    """``ParentBased(root=ForceSample(TraceIdRatioBased(ratio)))``; children follow their parent."""
    return ParentBased(root=ForceSampleSampler(TraceIdRatioBased(ratio)))


def resolve_pod(environ: Mapping[str, str] | None = None) -> str:
    """Pod identity for logs and traces: ``POD_ID`` when set, else the host name."""
    env = os.environ if environ is None else environ
    return env.get("POD_ID") or socket.gethostname()


def build_resource(service_name: str, settings: BaseServiceSettings, *, version: str, pod: str) -> Resource:
    """Resource with ``service.name``, ``service.version``, ``deployment.environment``, ``k8s.pod.name``."""
    return Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
            "deployment.environment": settings.va_env.value,
            "k8s.pod.name": pod,
        }
    )


def _default_processor(settings: BaseServiceSettings) -> SpanProcessor:
    endpoint = settings.otel_exporter_otlp_endpoint
    exporter = OTLPSpanExporter(
        endpoint=endpoint, insecure=endpoint.startswith("http://"), timeout=OTLP_EXPORT_TIMEOUT_S
    )
    return BatchSpanProcessor(exporter)


def setup_tracing(  # noqa: PLR0913 - test seams (processor, globals, environ) are keyword-only
    service_name: str,
    settings: BaseServiceSettings,
    *,
    version: str = "unknown",
    span_processor: SpanProcessor | None = None,
    set_global: bool = True,
    environ: Mapping[str, str] | None = None,
) -> TracerProvider:
    """Build the service ``TracerProvider``; with ``set_global`` also install it and the W3C propagators.

    ``span_processor`` defaults to a ``BatchSpanProcessor`` exporting over OTLP gRPC to
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (plaintext for ``http://`` endpoints). Tests pass a
    ``SimpleSpanProcessor(InMemorySpanExporter())`` and ``set_global=False``.
    """
    provider = TracerProvider(
        resource=build_resource(service_name, settings, version=version, pod=resolve_pod(environ)),
        sampler=build_sampler(settings.otel_traces_sampler_arg),
    )
    provider.add_span_processor(
        span_processor if span_processor is not None else _default_processor(settings)
    )
    if set_global:
        trace.set_tracer_provider(provider)
        propagate.set_global_textmap(
            CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
        )
    return provider


def force_sampled_attributes(**attrs: AttributeValue) -> dict[str, AttributeValue]:
    """Start attributes for a span that must always be sampled (the ``call`` root span)."""
    return {**attrs, FORCE_SAMPLE_ATTRIBUTE: True}


def shutdown_tracing(provider: TracerProvider) -> None:
    """Flush pending spans and shut the provider down (graceful service shutdown)."""
    provider.force_flush()
    provider.shutdown()
