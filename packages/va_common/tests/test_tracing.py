from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from obs_helpers import make_settings, memory_provider
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from va_common.tracing import (
    FORCE_SAMPLE_ATTRIBUTE,
    ForceSampleSampler,
    build_resource,
    build_sampler,
    force_sampled_attributes,
    resolve_pod,
    setup_tracing,
    shutdown_tracing,
)


def test_resource_attributes() -> None:
    resource = build_resource("api", make_settings(va_env="staging"), version="1.4.0", pod="api-7f9")
    attrs = resource.attributes
    assert attrs["service.name"] == "api"
    assert attrs["service.version"] == "1.4.0"
    assert attrs["deployment.environment"] == "staging"
    assert attrs["k8s.pod.name"] == "api-7f9"


def test_resolve_pod_prefers_pod_id_then_hostname() -> None:
    assert resolve_pod({"POD_ID": "media-0"}) == "media-0"
    assert resolve_pod({"POD_ID": ""}) == socket.gethostname()
    assert resolve_pod({}) == socket.gethostname()


def test_resolve_pod_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_ID", "worker-3")
    assert resolve_pod() == "worker-3"


def test_setup_tracing_resource_uses_environ_pod() -> None:
    provider, _ = memory_provider(environ={"POD_ID": "edge-2"}, version="2.0.0")
    attrs = provider.resource.attributes
    assert attrs["k8s.pod.name"] == "edge-2"
    assert attrs["service.version"] == "2.0.0"
    provider.shutdown()


def test_ratio_zero_drops_normal_roots_but_keeps_forced() -> None:
    provider, exporter = memory_provider(ratio=0.0)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("http") as dropped:
        assert not dropped.get_span_context().trace_flags.sampled
    call_attributes = force_sampled_attributes(**{"va.call_id": "c1"})
    with (
        tracer.start_as_current_span("call", attributes=call_attributes),
        tracer.start_as_current_span("call.dial") as child,
    ):
        assert child.get_span_context().trace_flags.sampled
    names = [span.name for span in exporter.get_finished_spans()]
    assert names == ["call.dial", "call"]
    call = exporter.get_finished_spans()[1]
    assert call.attributes is not None
    assert call.attributes["va.call_id"] == "c1"
    assert call.attributes[FORCE_SAMPLE_ATTRIBUTE] is True
    provider.shutdown()


def test_child_of_sampled_remote_parent_is_sampled_under_ratio_zero() -> None:
    provider, exporter = memory_provider(ratio=0.0)
    parent = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    context = trace.set_span_in_context(NonRecordingSpan(parent))
    with provider.get_tracer("t").start_as_current_span("turn", context=context) as span:
        assert span.get_span_context().trace_flags.sampled
    assert len(exporter.get_finished_spans()) == 1
    provider.shutdown()


def test_ratio_one_samples_everything() -> None:
    provider, exporter = memory_provider(ratio=1.0)
    for name in ("a", "b", "c"):
        with provider.get_tracer("t").start_as_current_span(name):
            pass
    assert len(exporter.get_finished_spans()) == 3
    provider.shutdown()


def test_forced_sample_keeps_parent_trace_state() -> None:
    sampler = ForceSampleSampler(build_sampler(0.0))
    state = TraceState([("vendor", "x")])
    parent = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_state=state)
    result = sampler.should_sample(
        trace.set_span_in_context(NonRecordingSpan(parent)), 1, "call", attributes=force_sampled_attributes()
    )
    assert result.decision is Decision.RECORD_AND_SAMPLE
    assert result.trace_state == state


def test_forced_attribute_must_be_true() -> None:
    sampler = ForceSampleSampler(build_sampler(0.0))
    result = sampler.should_sample(Context(), 1, "call", attributes={FORCE_SAMPLE_ATTRIBUTE: "true"})
    assert result.decision is Decision.DROP


def test_sampler_description() -> None:
    description = build_sampler(0.25).get_description()
    assert description.startswith("ParentBased{root:ForceSample{TraceIdRatioBased{0.25}}")


def test_default_processor_is_batch_otlp_from_settings() -> None:
    settings = make_settings(otel_exporter_otlp_endpoint="http://collector.internal:4317")
    provider = setup_tracing("api", settings, set_global=False)
    processors = provider._active_span_processor._span_processors
    assert len(processors) == 1
    batch = processors[0]
    assert isinstance(batch, BatchSpanProcessor)
    exporter = batch._batch_processor._exporter
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == "collector.internal:4317"
    provider.shutdown()


def test_default_processor_secure_for_https() -> None:
    settings = make_settings(otel_exporter_otlp_endpoint="https://otel.example.test:4317")
    provider = setup_tracing("api", settings, set_global=False)
    exporter = provider._active_span_processor._span_processors[0]._batch_processor._exporter  # type: ignore[attr-defined]
    assert exporter._endpoint == "otel.example.test:4317"
    provider.shutdown()


@pytest.fixture
def restore_textmap() -> Iterator[None]:
    original: TextMapPropagator = propagate.get_global_textmap()
    yield
    propagate.set_global_textmap(original)


@pytest.mark.usefixtures("restore_textmap")
def test_set_global_installs_provider_and_w3c_propagators(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[TracerProvider] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    provider, _ = memory_provider()
    assert installed == []
    global_provider = setup_tracing(
        "api", make_settings(), span_processor=SimpleSpanProcessor(InMemorySpanExporter())
    )
    assert installed == [global_provider]
    assert propagate.get_global_textmap().fields == {"traceparent", "tracestate", "baggage"}
    provider.shutdown()
    global_provider.shutdown()


def test_shutdown_flushes_pending_spans() -> None:
    provider, exporter = memory_provider()
    with provider.get_tracer("t").start_as_current_span("last"):
        pass
    shutdown_tracing(provider)
    assert [span.name for span in exporter.get_finished_spans()] == ["last"]
    with provider.get_tracer("t").start_as_current_span("after"):
        pass
    assert len(exporter.get_finished_spans()) == 1
