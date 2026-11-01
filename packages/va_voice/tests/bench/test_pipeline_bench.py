"""Pipeline benchmark (P04-T13, specs/07 §21): platform overhead per turn and 50 concurrent pipelines.

Run with ``uv run pytest -m bench packages/va_voice``; it rewrites ``packages/va_voice/BENCHMARKS.md``.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pipeline_bench import (
    BENCH_CONCURRENT_CALLS,
    BENCH_CONCURRENT_TURNS,
    BENCH_REALTIME_TURNS,
    BENCH_TURNS,
    OVERHEAD_P50_LIMIT_MS,
    SECTION_CONCURRENCY,
    SECTION_HARNESS,
    SECTION_WALL,
    BenchSection,
    ConversationResult,
    LoopLagProbe,
    percentile,
    run_conversation,
    summary_rows,
)

from va_sim.clock import AcceleratedClock, RealtimeClock
from va_voice.outbound import LATE_THRESHOLD_MS

pytestmark = pytest.mark.bench


def _within_gate(result: ConversationResult, turns: int) -> bool:
    return (
        len(result.overheads) == len(result.gross_overheads) == turns
        and percentile(result.overheads, 50) <= OVERHEAD_P50_LIMIT_MS
        and percentile(result.gross_overheads, 50) <= OVERHEAD_P50_LIMIT_MS
    )


def _overhead_section(title: str, result: ConversationResult, turns: int) -> BenchSection:
    rows = [
        ("turns", str(turns)),
        ("turns measured", str(len(result.overheads))),
        *summary_rows("overhead", result.overheads),
        *summary_rows("gross overhead", result.gross_overheads),
        (f"outbound late frames (> {LATE_THRESHOLD_MS} ms)", str(result.late_frames)),
        ("outbound max lateness", f"{result.max_lateness_ms:.1f} ms"),
    ]
    return BenchSection(title, tuple(rows), _within_gate(result, turns))


async def test_turn_overhead_harness_time(bench_report: dict[str, BenchSection]) -> None:
    result = await run_conversation(clock=AcceleratedClock(), turns=BENCH_TURNS)
    section = _overhead_section(SECTION_HARNESS, result, BENCH_TURNS)
    bench_report[section.title] = section
    assert result.reports == BENCH_TURNS
    assert section.passed, section.rows


@pytest.mark.timeout(300)
async def test_turn_overhead_wall_clock(bench_report: dict[str, BenchSection]) -> None:
    result = await run_conversation(clock=RealtimeClock(), turns=BENCH_REALTIME_TURNS)
    section = _overhead_section(SECTION_WALL, result, BENCH_REALTIME_TURNS)
    bench_report[section.title] = section
    assert result.reports == BENCH_REALTIME_TURNS
    assert section.passed, section.rows


@pytest.mark.timeout(300)
async def test_fifty_concurrent_pipelines_without_late_frames(bench_report: dict[str, BenchSection]) -> None:
    probe = LoopLagProbe()
    stop = asyncio.Event()
    started = time.perf_counter()
    async with asyncio.TaskGroup() as group:
        lag = group.create_task(probe.run(stop))
        calls = [
            group.create_task(run_conversation(clock=RealtimeClock(), turns=BENCH_CONCURRENT_TURNS))
            for _ in range(BENCH_CONCURRENT_CALLS)
        ]
        await asyncio.gather(*calls)
        stop.set()
        await lag
    wall_s = time.perf_counter() - started
    results = [call.result() for call in calls]
    overheads = [value for result in results for value in result.overheads]
    gross = [value for result in results for value in result.gross_overheads]
    late = sum(result.late_frames for result in results)
    complete = all(
        (result.user_turns, result.agent_turns, result.reports) == (BENCH_CONCURRENT_TURNS,) * 3
        for result in results
    )
    rows = [
        ("pipelines", str(BENCH_CONCURRENT_CALLS)),
        ("turns per pipeline", str(BENCH_CONCURRENT_TURNS)),
        ("wall duration", f"{wall_s:.1f} s"),
        ("outbound frames sent", str(sum(result.frames_sent for result in results))),
        (f"outbound late frames (> {LATE_THRESHOLD_MS} ms)", str(late)),
        ("outbound max lateness", f"{max(result.max_lateness_ms for result in results):.1f} ms"),
        *summary_rows("overhead", overheads),
        *summary_rows("gross overhead", gross),
        *summary_rows("event loop lag", probe.samples_ms),
        ("event loop lag p99", f"{percentile(probe.samples_ms, 99):.1f} ms"),
    ]
    bench_report[SECTION_CONCURRENCY] = BenchSection(SECTION_CONCURRENCY, tuple(rows), complete and late == 0)
    assert complete
    assert late == 0
