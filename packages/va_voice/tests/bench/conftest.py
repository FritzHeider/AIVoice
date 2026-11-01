"""Benchmark plumbing (Decision P04-T13 in specs/07 §21).

Tests marked ``bench`` run only when the ``-m`` expression mentions ``bench``
(``uv run pytest -m bench packages/va_voice``): timing asserts do not belong in the parallel
``make test`` run.
Each bench test stores a :class:`pipeline_bench.BenchSection` in the session ``bench_report``; when every
section of :data:`pipeline_bench.SECTION_TITLES` was recorded (and the run is not an xdist worker) the session
writes ``packages/va_voice/BENCHMARKS.md`` (or ``$VA_BENCH_REPORT``).
"""

from __future__ import annotations

import os
import platform
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pipeline_bench import SECTION_TITLES, BenchSection, Machine, render_report

DEFAULT_REPORT = Path(__file__).resolve().parents[2] / "BENCHMARKS.md"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "bench" in (config.option.markexpr or ""):
        return
    benches = [item for item in items if item.get_closest_marker("bench") is not None]
    if benches:
        config.hook.pytest_deselected(items=benches)
        items[:] = [item for item in items if item.get_closest_marker("bench") is None]


def machine() -> Machine:
    return Machine(
        platform=platform.platform(),
        python=platform.python_version(),
        cpus=os.cpu_count() or 0,
        processor=platform.processor() or platform.machine(),
        date=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
    )


@pytest.fixture(scope="session")
def bench_report() -> Iterator[dict[str, BenchSection]]:
    sections: dict[str, BenchSection] = {}
    yield sections
    if set(sections) != set(SECTION_TITLES) or "PYTEST_XDIST_WORKER" in os.environ:
        return
    target = Path(os.environ.get("VA_BENCH_REPORT") or DEFAULT_REPORT)
    target.write_text(render_report(sections.values(), machine()), encoding="utf-8")
