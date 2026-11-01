"""Pipeline benchmark support (Decision P04-T13 in specs/07 §21).

``run_conversation`` drives one harness call through ``turns`` scripted turns against zero-latency fakes and
returns the platform overhead of every turn plus the outbound clock's lateness counters. ``render_report``
turns the collected :class:`BenchSection` tables into ``packages/va_voice/BENCHMARKS.md``.

Platform overhead of a turn = ``e2e_ms - eot_ms - (stt_final_ms + llm_ttft_ms + tts_ttfa_ms)``: everything
between the end of the caller's speech and the first agent audio that is neither endpointing silence (policy)
nor a vendor latency. Unmeasured vendor values count as 0. With zero-latency fakes the "vendor" terms are the
fakes' in-process time, so the report also gates the gross ``e2e_ms - eot_ms`` (:func:`gross_overhead_ms`).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import partial

from pipeline_fakes import ScriptedBrain, tokens
from pipeline_harness import HarnessClock, harness

from va_voice.latency import LatencyTracer, TurnLatency
from va_voice.outbound import LATE_THRESHOLD_MS
from va_voice.pipeline import PipelineConfig, SpeakText
from va_voice.pipeline.config import NO_INPUT_TIMEOUT_MAX_MS

__all__ = [
    "BENCH_CONCURRENT_CALLS",
    "BENCH_CONCURRENT_TURNS",
    "BENCH_REALTIME_TURNS",
    "BENCH_TURNS",
    "OVERHEAD_P50_LIMIT_MS",
    "REPLY",
    "SECTION_CONCURRENCY",
    "SECTION_HARNESS",
    "SECTION_TITLES",
    "SECTION_WALL",
    "SPEECH_MS",
    "UTTERANCE",
    "BenchSection",
    "ConversationResult",
    "LoopLagProbe",
    "Machine",
    "bench_brain",
    "gross_overhead_ms",
    "percentile",
    "render_report",
    "run_conversation",
    "summary_rows",
    "turn_overhead_ms",
]

BENCH_TURNS = 200
"""Turns of the harness-time (accelerated clock) overhead run."""
BENCH_REALTIME_TURNS = 30
"""Turns of the wall-clock overhead run."""
BENCH_CONCURRENT_CALLS = 50
"""Pipelines sharing one event loop in the concurrency run (specs/07 §21: 50 calls per pod)."""
BENCH_CONCURRENT_TURNS = 3
"""Turns per pipeline in the concurrency run."""
OVERHEAD_P50_LIMIT_MS = 30.0
"""Phase 04 exit gate: platform overhead per turn, p50, zero-latency fakes."""
SPEECH_MS = 600
REPLY = "Sure, I can help with that."
UTTERANCE = "Can you help me with my order?"

SECTION_HARNESS = "Platform overhead per turn (harness time)"
SECTION_WALL = "Platform overhead per turn (wall clock)"
SECTION_CONCURRENCY = f"{BENCH_CONCURRENT_CALLS} concurrent pipelines on one event loop (wall clock)"
SECTION_TITLES = (SECTION_HARNESS, SECTION_WALL, SECTION_CONCURRENCY)
"""Every section of a complete report, in report order."""

_LAG_INTERVAL_S = 0.01


def turn_overhead_ms(latency: TurnLatency) -> float | None:
    """Platform overhead of one turn, or ``None`` when ``e2e_ms`` or ``eot_ms`` was not measured."""
    if latency.e2e_ms is None or latency.eot_ms is None:
        return None
    vendor = sum(value or 0 for value in (latency.stt_final_ms, latency.llm_ttft_ms, latency.tts_ttfa_ms))
    return float(latency.e2e_ms - latency.eot_ms - vendor)


def _reached(items: Sequence[object], count: int) -> bool:
    return len(items) >= count


def gross_overhead_ms(latency: TurnLatency) -> float | None:
    """``e2e_ms - eot_ms`` (vendor terms included), or ``None`` when either was not measured."""
    if latency.e2e_ms is None or latency.eot_ms is None:
        return None
    return float(latency.e2e_ms - latency.eot_ms)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100) of a non-empty sequence."""
    if not values:
        message = "percentile of an empty sequence"
        raise ValueError(message)
    if not 0 <= q <= 100:
        message = f"percentile q must be within 0..100, got {q}"
        raise ValueError(message)
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def bench_brain(turns: int) -> ScriptedBrain:
    """A Brain that answers each of ``turns`` user turns with :data:`REPLY` streamed instantly."""
    return ScriptedBrain(replies=[[SpeakText(tokens(REPLY))] for _ in range(turns)])


@dataclass(frozen=True, slots=True)
class ConversationResult:
    overheads: tuple[float, ...]
    gross_overheads: tuple[float, ...]
    reports: int
    user_turns: int
    agent_turns: int
    frames_sent: int
    late_frames: int
    max_lateness_ms: float


async def run_conversation(*, clock: HarnessClock, turns: int) -> ConversationResult:
    """One call of ``turns`` caller turns (``SPEECH_MS`` each) answered by :func:`bench_brain`."""
    config = PipelineConfig(no_input_timeout_ms=NO_INPUT_TIMEOUT_MAX_MS)

    def tracer(tracer_clock: HarnessClock) -> LatencyTracer:
        return LatencyTracer(tracer_clock)

    async with harness(
        config=config,
        brain=bench_brain(turns),
        utterances=[UTTERANCE] * turns,
        latency=tracer,
        clock=clock,
    ) as h:
        for i in range(turns):
            await h.turn(SPEECH_MS)
            await h.until(partial(_reached, h.observer.agent_turns, i + 1))
        await h.until(lambda: len(h.observer.reports) >= turns)
        stats = h.pipeline.speaker.outbound.stats
        latencies = [report.latency for report in h.observer.reports]
        overheads = (turn_overhead_ms(latency) for latency in latencies)
        gross = (gross_overhead_ms(latency) for latency in latencies)
        return ConversationResult(
            overheads=tuple(value for value in overheads if value is not None),
            gross_overheads=tuple(value for value in gross if value is not None),
            reports=len(h.observer.reports),
            user_turns=len(h.observer.user_turns),
            agent_turns=len(h.observer.agent_turns),
            frames_sent=stats.frames_sent,
            late_frames=stats.late_frames,
            max_lateness_ms=stats.max_lateness_ms,
        )


@dataclass(slots=True)
class LoopLagProbe:
    """Measures event-loop lag on the wall clock: how late a 10 ms sleep wakes up."""

    samples_ms: list[float] = field(default_factory=list)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(_LAG_INTERVAL_S)
            self.samples_ms.append(max(0.0, (time.perf_counter() - before - _LAG_INTERVAL_S) * 1000))


@dataclass(frozen=True, slots=True)
class BenchSection:
    title: str
    rows: tuple[tuple[str, str], ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class Machine:
    platform: str
    python: str
    cpus: int
    processor: str
    date: str


def summary_rows(label: str, values: Sequence[float]) -> list[tuple[str, str]]:
    """``label`` p50 / p95 / max rows (ms, one decimal)."""
    return [
        (f"{label} p50", f"{percentile(values, 50):.1f} ms"),
        (f"{label} p95", f"{percentile(values, 95):.1f} ms"),
        (f"{label} max", f"{max(values):.1f} ms"),
    ]


def render_report(sections: Iterable[BenchSection], machine: Machine) -> str:
    """The markdown of ``BENCHMARKS.md``; sections in :data:`SECTION_TITLES` order."""
    by_title = {section.title: section for section in sections}
    lines = [
        "# va_voice pipeline benchmarks",
        "",
        "Generated by `uv run pytest -m bench packages/va_voice`; do not edit by hand.",
        "",
        f"- Date (UTC): {machine.date}",
        f"- Platform: {machine.platform}",
        f"- Python: {machine.python}",
        f"- CPUs: {machine.cpus}",
        f"- Processor: {machine.processor or 'unknown'}",
        "",
        "Platform overhead per turn = `e2e_ms - eot_ms - (stt_final_ms + llm_ttft_ms + tts_ttfa_ms)` with",
        "zero-latency fakes (specs/07 §21, Decision P04-T13); 'gross' = `e2e_ms - eot_ms` (the fakes'",
        "in-process time included). Percentiles are nearest-rank.",
        f"Gate: overhead and gross p50 <= {OVERHEAD_P50_LIMIT_MS:.0f} ms; no outbound frame more than",
        f"{LATE_THRESHOLD_MS} ms past its target in the concurrency run.",
    ]
    for title in SECTION_TITLES:
        section = by_title.get(title)
        if section is None:
            continue
        lines += ["", f"## {title}", "", f"Result: {'PASS' if section.passed else 'FAIL'}", ""]
        lines += ["| Metric | Value |", "|---|---|"]
        lines += [f"| {name} | {value} |" for name, value in section.rows]
    return "\n".join(lines) + "\n"
