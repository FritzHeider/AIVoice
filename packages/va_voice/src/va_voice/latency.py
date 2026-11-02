"""Per-turn latency tracer (specs/07-media-pipeline.md §20, specs/27-observability.md §2.1, §3.3, §4).

The pipeline records named instants (:class:`TurnMark`) on one monotonic millisecond timeline (the one the
endpointer and the outbound pacer use) into a :class:`TurnTimer`. When the turn ends, :class:`LatencyTracer`
turns the marks into a :class:`TurnLatency` (the ``call_turns.latency`` values), observes the turn histograms,
emits the ``turn`` span tree retroactively with exact start/end timestamps, and builds the ``call.latency``
event messages.

Stage histograms that a vendor client already observes with its own labels (``va_stt_final_ms``,
``va_tts_ttfa_ms``, ``va_llm_ttft_ms``) are not observed here, so nothing is counted twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from opentelemetry import trace

from va_common.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.context import Context
    from opentelemetry.trace import TracerProvider

    from va_common.metrics import ServiceMetrics
    from va_common.time import Clock

__all__ = [
    "METRIC_KEYS",
    "METRIC_SPANS",
    "AttributeValue",
    "LatencyEvent",
    "LatencyTracer",
    "MetricSpan",
    "ToolTiming",
    "TurnLatency",
    "TurnMark",
    "TurnMetrics",
    "TurnReport",
    "TurnTimer",
    "TurnTracer",
    "latency_events",
]

type AttributeValue = str | int | float | bool

_log = get_logger(__name__)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class TurnMark(StrEnum):
    """Instants of one turn (specs/07 §20 start/end columns)."""

    USER_SPEECH_START = "user_speech_start"
    USER_SPEECH_END = "user_speech_end"
    EOT = "eot"
    STT_COMMIT = "stt_commit"
    STT_FINAL = "stt_final"
    ROUTE_START = "route_start"
    ROUTE_END = "route_end"
    LLM_REQUEST = "llm_request"
    LLM_FIRST_TOKEN = "llm_first_token"  # noqa: S105 (a mark name, not a credential)
    TTS_FIRST_TEXT = "tts_first_text"
    TTS_FIRST_AUDIO = "tts_first_audio"
    FIRST_AUDIO_OUT = "first_audio_out"


# The last voiced window end moves when the caller resumes speaking; other marks keep their first value.
_LAST_WINS: Final = frozenset({TurnMark.USER_SPEECH_END})


@dataclass(frozen=True, slots=True)
class MetricSpan:
    """One measured interval: its marks, child span (``None`` = attribute on ``turn`` only), and attribute."""

    start: TurnMark
    end: TurnMark
    span: str | None
    attribute: str


METRIC_SPANS: Final[dict[str, MetricSpan]] = {
    "eot_ms": MetricSpan(TurnMark.USER_SPEECH_END, TurnMark.EOT, "turn.eot", "va.eot_ms"),
    "stt_final_ms": MetricSpan(TurnMark.STT_COMMIT, TurnMark.STT_FINAL, "turn.stt_final", "va.stt_final_ms"),
    "llm_ttft_ms": MetricSpan(TurnMark.LLM_REQUEST, TurnMark.LLM_FIRST_TOKEN, "turn.llm", "va.llm_ttft_ms"),
    "tts_ttfa_ms": MetricSpan(
        TurnMark.TTS_FIRST_TEXT, TurnMark.TTS_FIRST_AUDIO, "turn.tts_first_audio", "va.tts_ttfa_ms"
    ),
    "e2e_ms": MetricSpan(TurnMark.USER_SPEECH_END, TurnMark.FIRST_AUDIO_OUT, None, "va.e2e_ms"),
    "route_ms": MetricSpan(TurnMark.ROUTE_START, TurnMark.ROUTE_END, "turn.route", "va.route_ms"),
}

# ``call_turns.latency`` key order (specs/03 call_turns, specs/27 §4).
METRIC_KEYS: Final = ("eot_ms", "stt_final_ms", "llm_ttft_ms", "tts_ttfa_ms", "e2e_ms", "route_ms", "tool_ms")

# ``call.latency`` stage events (specs/19 §2): message label per metric, in emission order.
_STAGE_LABELS: Final = (("stt_final_ms", "STT"), ("llm_ttft_ms", "LLM"), ("tts_ttfa_ms", "TTS"))


def _round_ms(value: float) -> int:
    """Round half up to whole milliseconds."""
    return math.floor(value + 0.5)


@dataclass(frozen=True, slots=True)
class ToolTiming:
    """One tool/webhook request of the turn on the turn's ms timeline."""

    name: str
    start_ms: float
    end_ms: float
    status_code: int | None = None

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class TurnLatency:
    """The measured latencies of one turn in whole ms; ``None`` = not measured."""

    eot_ms: int | None = None
    stt_final_ms: int | None = None
    llm_ttft_ms: int | None = None
    tts_ttfa_ms: int | None = None
    e2e_ms: int | None = None
    route_ms: int | None = None
    tool_ms: int | None = None

    def as_dict(self) -> dict[str, int]:
        """Measured values only, in ``call_turns.latency`` key order."""
        values = {key: getattr(self, key) for key in METRIC_KEYS}
        return {key: value for key, value in values.items() if value is not None}


class TurnTimer:
    """Marks of one turn; ``mark`` defaults to the clock's monotonic time in ms."""

    def __init__(self, turn_index: int, clock: Clock, *, node_id: str | None = None) -> None:
        self._turn_index = turn_index
        self._clock = clock
        self._node_id = node_id
        self._marks: dict[TurnMark, float] = {}
        self._tools: list[ToolTiming] = []
        self._interrupted = False
        self._annotations: dict[str, dict[str, AttributeValue]] = {}
        self.finished = False

    @property
    def turn_index(self) -> int:
        return self._turn_index

    @property
    def node_id(self) -> str | None:
        return self._node_id

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    @property
    def tools(self) -> tuple[ToolTiming, ...]:
        return tuple(self._tools)

    def now_ms(self) -> float:
        return self._clock.monotonic() * 1000.0

    def mark(self, mark: TurnMark, at_ms: float | None = None) -> None:
        """Record ``mark``; the first value wins, except for ``user_speech_end`` (the last value wins)."""
        if mark in self._marks and mark not in _LAST_WINS:
            return
        self._marks[mark] = self.now_ms() if at_ms is None else at_ms

    def at(self, mark: TurnMark) -> float | None:
        return self._marks.get(mark)

    def add_tool(self, name: str, start_ms: float, end_ms: float, status_code: int | None = None) -> None:
        if end_ms < start_ms:
            msg = f"tool {name!r} ends before it starts: {start_ms} > {end_ms}"
            raise ValueError(msg)
        self._tools.append(ToolTiming(name, start_ms, end_ms, status_code))

    def set_node(self, node_id: str | None) -> None:
        self._node_id = node_id

    def mark_interrupted(self) -> None:
        self._interrupted = True

    def annotate(self, span: str, attributes: Mapping[str, AttributeValue]) -> None:
        """Extra attributes for ``span``: ``turn`` or a child (``turn.stt_final`` → ``{"va.chars": 42}``).

        Later values replace earlier ones; they are applied only when that span is emitted.
        """
        self._annotations.setdefault(span, {}).update(attributes)

    def annotations(self, span: str) -> dict[str, AttributeValue]:
        return dict(self._annotations.get(span, {}))

    def instants(self) -> list[float]:
        """Every recorded instant (marks and tool bounds)."""
        times = list(self._marks.values())
        for tool in self._tools:
            times.extend((tool.start_ms, tool.end_ms))
        return times

    @property
    def started_ms(self) -> float | None:
        times = self.instants()
        return min(times) if times else None

    @property
    def ended_ms(self) -> float | None:
        times = self.instants()
        return max(times) if times else None

    def intervals(self) -> dict[str, tuple[float, float]]:
        """``(start, end)`` per measured metric: both marks recorded and in order.

        A metric whose end mark precedes its start mark is left out and logged (``turn_latency_misordered``).
        """
        measured: dict[str, tuple[float, float]] = {}
        for metric, spec in METRIC_SPANS.items():
            start, end = self._marks.get(spec.start), self._marks.get(spec.end)
            if start is None or end is None:
                continue
            if end < start:
                _log.warning(
                    "turn_latency_misordered",
                    metric=metric,
                    turn_index=self._turn_index,
                    start_ms=start,
                    end_ms=end,
                )
                continue
            measured[metric] = (start, end)
        return measured

    def latency(self, intervals: dict[str, tuple[float, float]] | None = None) -> TurnLatency:
        """The turn's latencies from ``intervals`` (default: :meth:`intervals`)."""
        measured = self.intervals() if intervals is None else intervals
        values = {metric: _round_ms(end - start) for metric, (start, end) in measured.items()}
        tool_ms = _round_ms(sum(tool.duration_ms for tool in self._tools)) if self._tools else None
        return TurnLatency(tool_ms=tool_ms, **values)


@dataclass(frozen=True, slots=True)
class LatencyEvent:
    """One ``call.latency`` event: Bland-style message and data."""

    message: str
    data: dict[str, int]


def latency_events(latency: TurnLatency, turn_index: int) -> list[LatencyEvent]:
    """Summary ``"E2E: {n}ms"`` (only when e2e was measured), then ``STT``/``LLM``/``TTS`` stage events."""
    events: list[LatencyEvent] = []
    if latency.e2e_ms is not None:
        summary = {**latency.as_dict(), "turn_index": turn_index}
        events.append(LatencyEvent(f"E2E: {latency.e2e_ms}ms", summary))
    for metric, label in _STAGE_LABELS:
        value: int | None = getattr(latency, metric)
        if value is not None:
            events.append(LatencyEvent(f"{label}: {value}ms", {metric: value, "turn_index": turn_index}))
    return events


class TurnMetrics:
    """Turn-level histograms of the media-worker catalog (specs/27 §3.3)."""

    def __init__(self, metrics: ServiceMetrics, *, org_tier: str) -> None:
        self._e2e = metrics.histogram("va_turn_e2e_ms").labels(org_tier=org_tier)
        self._eot = metrics.histogram("va_turn_eot_ms")
        self._barge_ins = metrics.counter("va_barge_in_total")
        self._reaction = metrics.histogram("va_barge_in_reaction_ms")

    def observe(self, latency: TurnLatency) -> None:
        if latency.e2e_ms is not None:
            self._e2e.observe(latency.e2e_ms)
        if latency.eot_ms is not None:
            self._eot.observe(latency.eot_ms)

    def barge_in(self, *, honored: bool, reaction_ms: float | None = None) -> None:
        """Count a barge-in; honored ones also observe the reaction (speech start → ``clear`` sent)."""
        self._barge_ins.labels(honored="true" if honored else "false").inc()
        if honored and reaction_ms is not None:
            self._reaction.observe(reaction_ms)


class TurnTracer:
    """Emits the ``turn`` span tree after the fact; monotonic ms map to epoch ns through a fixed anchor."""

    def __init__(
        self,
        clock: Clock,
        *,
        call_id: str,
        org_id: str,
        tracer_provider: TracerProvider | None = None,
        parent: Context | None = None,
    ) -> None:
        self._tracer = trace.get_tracer("va_voice", tracer_provider=tracer_provider)
        self._call_id = call_id
        self._org_id = org_id
        self._parent = parent
        self._anchor_ns = (clock.now() - _EPOCH) // timedelta(microseconds=1) * 1000
        self._anchor_ms = clock.monotonic() * 1000.0

    def to_ns(self, at_ms: float) -> int:
        return self._anchor_ns + round((at_ms - self._anchor_ms) * 1_000_000)

    def emit(self, timer: TurnTimer, intervals: dict[str, tuple[float, float]] | None = None) -> None:
        """Emit ``turn`` and its children; nothing when the timer holds no instant."""
        measured = timer.intervals() if intervals is None else intervals
        latency = timer.latency(measured)
        started, ended = timer.started_ms, timer.ended_ms
        if started is None or ended is None:
            return
        turn = self._tracer.start_span(
            "turn",
            context=self._parent,
            start_time=self.to_ns(started),
            attributes=self._turn_attributes(timer, latency),
        )
        context = trace.set_span_in_context(turn)
        self._emit_stage_spans(context, timer, latency, measured)
        self._emit_tool_spans(context, timer)
        turn.end(end_time=self.to_ns(ended))

    def _turn_attributes(self, timer: TurnTimer, latency: TurnLatency) -> dict[str, AttributeValue]:
        attributes: dict[str, AttributeValue] = {
            "va.call_id": self._call_id,
            "va.org_id": self._org_id,
            "va.turn_index": timer.turn_index,
            "va.interrupted": timer.interrupted,
        }
        if timer.node_id is not None:
            attributes["va.node_id"] = timer.node_id
        if latency.e2e_ms is not None:
            attributes["va.e2e_ms"] = latency.e2e_ms
        attributes.update(timer.annotations("turn"))
        return attributes

    def _emit_stage_spans(
        self,
        context: Context,
        timer: TurnTimer,
        latency: TurnLatency,
        measured: dict[str, tuple[float, float]],
    ) -> None:
        for metric, spec in METRIC_SPANS.items():
            bounds = measured.get(metric)
            if spec.span is None or bounds is None:
                continue
            attrs: dict[str, AttributeValue] = {spec.attribute: getattr(latency, metric)}
            attrs.update(timer.annotations(spec.span))
            self._child(context, spec.span, bounds, attrs)

    def _emit_tool_spans(self, context: Context, timer: TurnTimer) -> None:
        for tool in timer.tools:
            tool_attributes: dict[str, AttributeValue] = {"va.tool.duration_ms": _round_ms(tool.duration_ms)}
            if tool.status_code is not None:
                tool_attributes["http.status_code"] = tool.status_code
            self._child(context, f"turn.tool {tool.name}", (tool.start_ms, tool.end_ms), tool_attributes)

    def _child(
        self, context: Context, name: str, bounds: tuple[float, float], attributes: dict[str, AttributeValue]
    ) -> None:
        span = self._tracer.start_span(
            name, context=context, start_time=self.to_ns(bounds[0]), attributes=attributes
        )
        span.end(end_time=self.to_ns(bounds[1]))


@dataclass(frozen=True, slots=True)
class TurnReport:
    """What a finished turn produced: the ``call_turns.latency`` values and the ``call.latency`` events of the
    user turn ``turn_index``."""

    latency: TurnLatency
    events: list[LatencyEvent]
    turn_index: int


class LatencyTracer:
    """Starts turn timers and finishes them into metrics, spans, and latency events."""

    def __init__(
        self, clock: Clock, *, metrics: TurnMetrics | None = None, tracer: TurnTracer | None = None
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._tracer = tracer

    def start_turn(self, turn_index: int, node_id: str | None = None) -> TurnTimer:
        return TurnTimer(turn_index, self._clock, node_id=node_id)

    def finish(self, timer: TurnTimer) -> TurnReport:
        if timer.finished:
            msg = f"turn {timer.turn_index} was already finished"
            raise RuntimeError(msg)
        timer.finished = True
        intervals = timer.intervals()
        latency = timer.latency(intervals)
        if self._metrics is not None:
            self._metrics.observe(latency)
        if self._tracer is not None:
            self._tracer.emit(timer, intervals)
        return TurnReport(latency, latency_events(latency, timer.turn_index), timer.turn_index)

    def barge_in(self, *, honored: bool, reaction_ms: float | None = None) -> None:
        if self._metrics is not None:
            self._metrics.barge_in(honored=honored, reaction_ms=reaction_ms)
