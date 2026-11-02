"""Single-pod concurrency check (specs/30 §6 Decision P05-T17e; specs/00 §5).

50 simultaneous task calls (``E2E_CONCURRENT_CALLS`` overrides the count) on the stack's one media-worker
process: zero dropped calls, all of them attached at once, zero platform underruns, and platform overhead p50
within budget. Marked ``capacity``: deselected by ``make e2e``, run by ``make e2e-capacity``.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import pytest
from e2e_assertions import TurnOverhead, nearest_rank, turn_overheads
from e2e_capacity import concurrent_calls
from e2e_harness import E2eOrg, FinishedCall, Harness, RegisteredScenario
from e2e_metrics import Scrape, histogram_quantile, sample

from va_voice.outbound import LATE_THRESHOLD_MS

pytestmark = [pytest.mark.e2e, pytest.mark.capacity, pytest.mark.timeout(900)]

CALLER_TURNS: Final = 3
OVERHEAD_P50_MS: Final = 150
"""specs/00 §5: platform overhead p50 in simulator runs (gated)."""
OVERHEAD_P95_MS: Final = 250
"""specs/30 §5 per-flow budget (reported here, not gated: the acceptance target under load is the p50)."""
LOOP_LAG_P99_MS: Final = 10
"""specs/07 §21 event-loop lag p99 (reported; the gated swarm check is phase 20)."""
PLAN_ID: Final = "scale"
"""Concurrency 100 and a `calls_send` bucket of 2 x 25/s: 50 calls dispatched at once are never refused."""
CREDIT_DOLLARS: Final = "500"
"""50 call holds (10 min cap x connected rate + attempt fee each) fit with room to spare."""
COMPLETE_POLL_S: Final = 1.0
"""50 pollers at 1/s stay well inside the plan's 100 req/s `default` bucket (the 0.25 s default would not)."""
UNDERRUNS: Final = "va_audio_underruns_total"
LOOP_LAG: Final = "va_event_loop_lag_ms"
FIRST_SENTENCE: Final = "Hello, this is Sam from Bright Smile Dental calling about your appointment."
TASK: Final = (
    "You are Sam from Bright Smile Dental. Confirm or reschedule the caller's cleaning, then end the call."
)


@dataclass(frozen=True, slots=True)
class Run:
    finished: list[FinishedCall]
    peak_active: int
    dispatch_s: float
    wall_s: float
    before: Scrape
    after: Scrape


async def _run_calls(harness: Harness, org: E2eOrg, scenarios: Sequence[RegisteredScenario]) -> Run:
    stop = asyncio.Event()
    before = await harness.media_metrics()
    watcher = asyncio.create_task(harness.watch_active_calls(stop))
    started = time.monotonic()
    try:
        call_ids = await asyncio.gather(
            *(harness.send_task_call(org, s, TASK, first_sentence=FIRST_SENTENCE) for s in scenarios)
        )
        dispatch_s = time.monotonic() - started
        finished = await asyncio.gather(
            *(
                harness.finished(org, c, number=s.number, poll_s=COMPLETE_POLL_S)
                for c, s in zip(call_ids, scenarios, strict=True)
            )
        )
    finally:
        stop.set()
        peak = await watcher
    wall_s = time.monotonic() - started
    after = await harness.media_metrics()
    return Run(list(finished), peak, dispatch_s, wall_s, before, after)


def _lag(run: Run, q: float) -> str:
    try:
        bound = histogram_quantile(run.before, run.after, LOOP_LAG, q)
    except ValueError:
        return "n/a"
    return "> largest bucket" if math.isinf(bound) else f"<= {bound:g} ms"


def _report(run: Run, overheads: Sequence[TurnOverhead], failures: Sequence[str], underruns: float) -> str:
    platform = [turn.overhead_ms for turn in overheads]
    sim = [turn.sim_ms for turn in overheads]
    lines = [
        "| metric | value |",
        "|---|---|",
        f"| calls | {len(run.finished)} (failed scenarios: {len(failures)}) |",
        f"| peak attached to the media worker | {run.peak_active} |",
        f"| dispatch of all calls | {run.dispatch_s:.2f} s |",
        f"| caller turns measured | {len(overheads)} |",
    ]
    if overheads:
        p50, p95 = nearest_rank(platform, 50), nearest_rank(platform, 95)
        verdict = "PASS" if p95 <= OVERHEAD_P95_MS else "WARN"
        lines += [
            f"| platform overhead p50 / p95 / max | {p50:g} / {p95:g} ({verdict}) / {max(platform)} ms |",
            f"| sim voice-to-voice p50 / p95 | {nearest_rank(sim, 50):g} / {nearest_rank(sim, 95):g} ms |",
        ]
    lines += [
        f"| platform underruns | {underruns:g} |",
        f"| event-loop lag p50 / p99 (target p99 <= {LOOP_LAG_P99_MS} ms) "
        f"| {_lag(run, 0.5)} / {_lag(run, 0.99)} |",
        f"| late frame threshold | {LATE_THRESHOLD_MS} ms |",
        f"| wall time | {run.wall_s:.1f} s |",
    ]
    return "\n".join(lines)


def _call_count() -> int:
    try:
        return concurrent_calls(os.environ)
    except ValueError as error:
        pytest.fail(str(error))


async def test_concurrent_calls_on_one_media_worker(harness: Harness) -> None:
    calls = _call_count()
    org = await harness.create_org(plan_id=PLAN_ID, dollars=CREDIT_DOLLARS)
    scenarios = [await harness.load_scenario("concurrent_call", instance=n) for n in range(calls)]
    run = await _run_calls(harness, org, scenarios)

    failures = [
        f"{finished.call_id}: {problems}"
        for finished, scenario in zip(run.finished, scenarios, strict=True)
        if (problems := finished.failures(scenario))
    ]
    overheads = [
        turn for finished in run.finished for turn in turn_overheads(finished.timeline, finished.events)
    ]
    underruns = sample(run.after, UNDERRUNS) - sample(run.before, UNDERRUNS)
    print(f"\nconcurrent_calls:\n{_report(run, overheads, failures, underruns)}")  # noqa: T201

    assert failures == []
    assert run.peak_active == calls
    assert underruns == 0
    assert len(overheads) == calls * CALLER_TURNS
    assert nearest_rank([turn.overhead_ms for turn in overheads], 50) <= OVERHEAD_P50_MS
