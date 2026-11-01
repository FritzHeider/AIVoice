"""Latency tracer: turn marks, metrics, spans, and latency events (specs/07 §20, specs/27 §2.1, §3.3, §4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from obs_helpers import memory_provider
from opentelemetry import trace
from prometheus_client import CollectorRegistry
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, build_metrics, new_registry
from va_common.time import FrozenClock
from va_voice.latency import (
    METRIC_KEYS,
    LatencyEvent,
    LatencyTracer,
    TurnLatency,
    TurnMark,
    TurnMetrics,
    TurnTimer,
    TurnTracer,
    latency_events,
)

M = TurnMark

# A full turn on the ms timeline: user stops at 1000, reply audio leaves at 1742.
FULL_TURN: dict[TurnMark, float] = {
    M.USER_SPEECH_START: 200.0,
    M.USER_SPEECH_END: 1000.0,
    M.EOT: 1180.0,
    M.STT_COMMIT: 1180.0,
    M.STT_FINAL: 1243.0,
    M.ROUTE_START: 1243.0,
    M.ROUTE_END: 1300.0,
    M.LLM_REQUEST: 1250.0,
    M.LLM_FIRST_TOKEN: 1516.0,
    M.TTS_FIRST_TEXT: 1520.0,
    M.TTS_FIRST_AUDIO: 1738.0,
    M.FIRST_AUDIO_OUT: 1742.0,
}
FULL_LATENCY = TurnLatency(
    eot_ms=180, stt_final_ms=63, llm_ttft_ms=266, tts_ttfa_ms=218, e2e_ms=742, route_ms=57, tool_ms=None
)


def full_timer(clock: FrozenClock | None = None, *, turn_index: int = 3) -> TurnTimer:
    timer = TurnTimer(turn_index, clock or FrozenClock(), node_id="node-1")
    for mark, at in FULL_TURN.items():
        timer.mark(mark, at)
    return timer


def metrics() -> tuple[TurnMetrics, CollectorRegistry]:
    service = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    return TurnMetrics(service, org_tier="build"), service.registry


def sample(registry: CollectorRegistry, name: str, labels: dict[str, str] | None = None) -> float | None:
    return registry.get_sample_value(name, labels or {})


# --- TurnTimer / TurnLatency --------------------------------------------------------------------------------


def test_full_turn_measures_every_metric() -> None:
    assert full_timer().latency() == FULL_LATENCY


def test_values_round_half_up() -> None:
    timer = TurnTimer(0, FrozenClock())
    timer.mark(M.USER_SPEECH_END, 100.0)
    timer.mark(M.EOT, 112.5)
    timer.mark(M.FIRST_AUDIO_OUT, 112.49)
    latency = timer.latency()
    assert (latency.eot_ms, latency.e2e_ms) == (13, 12)


@pytest.mark.parametrize(
    ("missing", "metric"),
    [
        (M.EOT, "eot_ms"),
        (M.STT_COMMIT, "stt_final_ms"),
        (M.ROUTE_END, "route_ms"),
        (M.LLM_FIRST_TOKEN, "llm_ttft_ms"),
        (M.TTS_FIRST_TEXT, "tts_ttfa_ms"),
        (M.FIRST_AUDIO_OUT, "e2e_ms"),
    ],
)
def test_missing_mark_leaves_its_metric_unmeasured(missing: TurnMark, metric: str) -> None:
    timer = TurnTimer(0, FrozenClock())
    for mark, at in FULL_TURN.items():
        if mark is not missing:
            timer.mark(mark, at)
    values = timer.latency().as_dict()
    assert metric not in values
    assert set(values) == {"eot_ms", "stt_final_ms", "llm_ttft_ms", "tts_ttfa_ms", "e2e_ms", "route_ms"} - {
        metric
    }


def test_misordered_marks_are_unmeasured_and_logged_once() -> None:
    stream = json_log_stream()
    timer = TurnTimer(4, FrozenClock())
    timer.mark(M.LLM_REQUEST, 500.0)
    timer.mark(M.LLM_FIRST_TOKEN, 400.0)
    timer.mark(M.USER_SPEECH_END, 100.0)
    timer.mark(M.EOT, 150.0)
    report = LatencyTracer(FrozenClock()).finish(timer)
    assert report.latency == TurnLatency(eot_ms=50)
    warnings = [e for e in log_events(stream) if e["event"] == "turn_latency_misordered"]
    assert [(e["metric"], e["turn_index"], e["start_ms"], e["end_ms"]) for e in warnings] == [
        ("llm_ttft_ms", 4, 500.0, 400.0)
    ]


def test_user_speech_end_last_wins_other_marks_first_win() -> None:
    timer = TurnTimer(0, FrozenClock())
    timer.mark(M.USER_SPEECH_START, 10.0)
    timer.mark(M.USER_SPEECH_START, 50.0)
    timer.mark(M.USER_SPEECH_END, 100.0)
    timer.mark(M.USER_SPEECH_END, 300.0)
    timer.mark(M.EOT, 400.0)
    timer.mark(M.EOT, 900.0)
    assert (timer.at(M.USER_SPEECH_START), timer.at(M.USER_SPEECH_END), timer.at(M.EOT)) == (
        10.0,
        300.0,
        400.0,
    )
    assert timer.latency().eot_ms == 100


def test_mark_defaults_to_clock_monotonic_ms() -> None:
    clock = FrozenClock()
    clock.advance(2.5)
    timer = TurnTimer(0, clock)
    timer.mark(M.USER_SPEECH_END)
    clock.advance(0.25)
    timer.mark(M.EOT)
    assert (timer.at(M.USER_SPEECH_END), timer.at(M.EOT)) == (2500.0, 2750.0)
    assert timer.at(M.STT_FINAL) is None
    assert timer.latency().eot_ms == 250


def test_tool_ms_sums_tool_durations() -> None:
    timer = TurnTimer(0, FrozenClock())
    assert timer.latency().tool_ms is None
    timer.add_tool("lookup", 100.0, 220.5, 200)
    timer.add_tool("book", 300.0, 380.25)
    assert timer.latency().tool_ms == 201
    assert [(t.name, t.duration_ms, t.status_code) for t in timer.tools] == [
        ("lookup", 120.5, 200),
        ("book", 80.25, None),
    ]


def test_tool_ending_before_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        TurnTimer(0, FrozenClock()).add_tool("x", 10.0, 9.0)


def test_timer_bounds_and_state() -> None:
    timer = TurnTimer(7, FrozenClock())
    assert (timer.started_ms, timer.ended_ms, timer.node_id, timer.interrupted) == (None, None, None, False)
    timer.mark(M.EOT, 500.0)
    timer.add_tool("t", 100.0, 900.0)
    timer.set_node("n2")
    timer.mark_interrupted()
    assert (timer.turn_index, timer.started_ms, timer.ended_ms, timer.node_id, timer.interrupted) == (
        7,
        100.0,
        900.0,
        "n2",
        True,
    )


def test_as_dict_keeps_spec_key_order_and_omits_unmeasured() -> None:
    assert list(TurnLatency(**dict.fromkeys(METRIC_KEYS, 1)).as_dict()) == [
        "eot_ms",
        "stt_final_ms",
        "llm_ttft_ms",
        "tts_ttfa_ms",
        "e2e_ms",
        "route_ms",
        "tool_ms",
    ]
    assert TurnLatency(tool_ms=5, eot_ms=2).as_dict() == {"eot_ms": 2, "tool_ms": 5}
    assert TurnLatency().as_dict() == {}


# --- latency events -----------------------------------------------------------------------------------------


def test_latency_events_messages_and_data() -> None:
    events = latency_events(FULL_LATENCY, 3)
    assert [e.message for e in events] == ["E2E: 742ms", "STT: 63ms", "LLM: 266ms", "TTS: 218ms"]
    assert events[0].data == {
        "eot_ms": 180,
        "stt_final_ms": 63,
        "llm_ttft_ms": 266,
        "tts_ttfa_ms": 218,
        "e2e_ms": 742,
        "route_ms": 57,
        "turn_index": 3,
    }
    assert events[1:] == [
        LatencyEvent("STT: 63ms", {"stt_final_ms": 63, "turn_index": 3}),
        LatencyEvent("LLM: 266ms", {"llm_ttft_ms": 266, "turn_index": 3}),
        LatencyEvent("TTS: 218ms", {"tts_ttfa_ms": 218, "turn_index": 3}),
    ]


def test_no_summary_without_e2e_and_unmeasured_stages_omitted() -> None:
    assert latency_events(TurnLatency(stt_final_ms=70, eot_ms=100), 1) == [
        LatencyEvent("STT: 70ms", {"stt_final_ms": 70, "turn_index": 1})
    ]
    assert latency_events(TurnLatency(), 1) == []


# --- metrics ------------------------------------------------------------------------------------------------


def test_turn_metrics_observe_e2e_and_eot_only() -> None:
    turn_metrics, registry = metrics()
    turn_metrics.observe(FULL_LATENCY)
    turn_metrics.observe(TurnLatency(eot_ms=40))
    turn_metrics.observe(TurnLatency(e2e_ms=600))
    assert sample(registry, "va_turn_e2e_ms_count", {"org_tier": "build"}) == 2
    assert sample(registry, "va_turn_e2e_ms_sum", {"org_tier": "build"}) == 1342
    assert sample(registry, "va_turn_eot_ms_count") == 2
    assert sample(registry, "va_turn_eot_ms_sum") == 220
    # Vendor clients own these histograms; the tracer must not observe them a second time.
    assert sample(registry, "va_stt_final_ms_count") == 0
    assert sample(registry, "va_llm_ttft_ms_count", {"model": "m", "purpose": "dialogue"}) is None
    assert sample(registry, "va_tts_ttfa_ms_count", {"model": "m"}) is None


def test_barge_in_counts_and_reaction() -> None:
    turn_metrics, registry = metrics()
    tracer = LatencyTracer(FrozenClock(), metrics=turn_metrics)
    tracer.barge_in(honored=True, reaction_ms=95.0)
    tracer.barge_in(honored=True)
    tracer.barge_in(honored=False, reaction_ms=50.0)
    assert sample(registry, "va_barge_in_total", {"honored": "true"}) == 2
    assert sample(registry, "va_barge_in_total", {"honored": "false"}) == 1
    assert sample(registry, "va_barge_in_reaction_ms_count") == 1
    assert sample(registry, "va_barge_in_reaction_ms_sum") == 95


# --- spans --------------------------------------------------------------------------------------------------


START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
START_NS = int(START.timestamp()) * 1_000_000_000


def test_turn_span_tree_with_exact_timestamps() -> None:
    provider, exporter = memory_provider()
    clock = FrozenClock(START)
    clock.advance(1.0)  # anchor: monotonic 1000 ms ↔ START + 1 s
    turn_tracer = TurnTracer(clock, call_id="call-1", org_id="org-1", tracer_provider=provider)
    timer = full_timer(clock)
    timer.add_tool("lookup", 1300.0, 1420.25, 200)
    timer.add_tool("notify", 1310.0, 1320.0)
    timer.annotate("turn.stt_final", {"va.chars": 42})
    timer.annotate("turn.eot", {"va.silence_threshold_ms": 400})
    timer.annotate("turn.eot", {"va.silence_threshold_ms": 180})
    timer.annotate("turn", {"va.hd_audio": False})
    timer.annotate("turn.kb_retrieve", {"va.hits": 2})

    def ns(ms: float) -> int:
        return START_NS + 1_000_000_000 + round((ms - 1000.0) * 1_000_000)

    report = LatencyTracer(clock, tracer=turn_tracer).finish(timer)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {
        "turn",
        "turn.eot",
        "turn.stt_final",
        "turn.route",
        "turn.llm",
        "turn.tts_first_audio",
        "turn.tool lookup",
        "turn.tool notify",
    }
    turn = spans["turn"]
    assert (turn.start_time, turn.end_time) == (ns(200.0), ns(1742.0))
    assert dict(turn.attributes or {}) == {
        "va.call_id": "call-1",
        "va.org_id": "org-1",
        "va.turn_index": 3,
        "va.interrupted": False,
        "va.node_id": "node-1",
        "va.e2e_ms": 742,
        "va.hd_audio": False,
    }
    assert report.latency.e2e_ms == 742
    expected = {
        "turn.eot": (1000.0, 1180.0, {"va.eot_ms": 180, "va.silence_threshold_ms": 180}),
        "turn.stt_final": (1180.0, 1243.0, {"va.stt_final_ms": 63, "va.chars": 42}),
        "turn.route": (1243.0, 1300.0, {"va.route_ms": 57}),
        "turn.llm": (1250.0, 1516.0, {"va.llm_ttft_ms": 266}),
        "turn.tts_first_audio": (1520.0, 1738.0, {"va.tts_ttfa_ms": 218}),
        "turn.tool lookup": (1300.0, 1420.25, {"va.tool.duration_ms": 120, "http.status_code": 200}),
        "turn.tool notify": (1310.0, 1320.0, {"va.tool.duration_ms": 10}),
    }
    for name, (start, end, attributes) in expected.items():
        span = spans[name]
        assert (span.start_time, span.end_time) == (ns(start), ns(end)), name
        assert dict(span.attributes or {}) == attributes, name
        assert span.parent is not None
        assert span.parent.span_id == turn.context.span_id
        assert span.context.trace_id == turn.context.trace_id


def test_turn_span_is_a_child_of_the_given_parent() -> None:
    provider, exporter = memory_provider()
    session = provider.get_tracer("test").start_span("media.session")
    parent = trace.set_span_in_context(session)
    clock = FrozenClock(START)
    tracer = TurnTracer(clock, call_id="c", org_id="o", tracer_provider=provider, parent=parent)
    timer = TurnTimer(0, clock)
    timer.mark(M.USER_SPEECH_START, 5.0)
    timer.mark_interrupted()
    tracer.emit(timer)
    session.end()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    turn = spans["turn"]
    assert turn.parent is not None
    assert turn.parent.span_id == spans["media.session"].context.span_id
    assert (turn.start_time, turn.end_time) == (START_NS + 5_000_000, START_NS + 5_000_000)
    assert dict(turn.attributes or {}) == {
        "va.call_id": "c",
        "va.org_id": "o",
        "va.turn_index": 0,
        "va.interrupted": True,
    }
    assert set(spans) == {"media.session", "turn"}


def test_no_marks_no_spans() -> None:
    provider, exporter = memory_provider()
    clock = FrozenClock(START)
    LatencyTracer(clock, tracer=TurnTracer(clock, call_id="c", org_id="o", tracer_provider=provider)).finish(
        TurnTimer(0, clock)
    )
    assert exporter.get_finished_spans() == ()


def test_misordered_metric_gets_no_child_span() -> None:
    provider, exporter = memory_provider()
    clock = FrozenClock(START)
    timer = TurnTimer(0, clock)
    timer.mark(M.TTS_FIRST_TEXT, 50.0)
    timer.mark(M.TTS_FIRST_AUDIO, 20.0)
    TurnTracer(clock, call_id="c", org_id="o", tracer_provider=provider).emit(timer)
    assert [span.name for span in exporter.get_finished_spans()] == ["turn"]


# --- LatencyTracer ------------------------------------------------------------------------------------------


def test_finish_reports_latency_and_events_and_observes_metrics() -> None:
    turn_metrics, registry = metrics()
    clock = FrozenClock()
    tracer = LatencyTracer(clock, metrics=turn_metrics)
    timer = tracer.start_turn(3, node_id="node-1")
    for mark, at in FULL_TURN.items():
        timer.mark(mark, at)
    report = tracer.finish(timer)
    assert report.latency == FULL_LATENCY
    assert report.events == latency_events(FULL_LATENCY, 3)
    assert timer.node_id == "node-1"
    assert sample(registry, "va_turn_e2e_ms_count", {"org_tier": "build"}) == 1


def test_finish_twice_raises_and_works_without_sinks() -> None:
    tracer = LatencyTracer(FrozenClock())
    timer = tracer.start_turn(9)
    timer.mark(M.USER_SPEECH_END, 0.0)
    assert tracer.finish(timer).events == []
    with pytest.raises(RuntimeError, match="turn 9 was already finished"):
        tracer.finish(timer)
    tracer.barge_in(honored=True, reaction_ms=10.0)
