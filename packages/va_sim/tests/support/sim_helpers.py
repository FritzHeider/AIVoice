"""Shared fixtures and builders for the va_sim and apps/sim tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_helpers import isolate_env

from va_sim.clock import AcceleratedClock
from va_sim.scenario import Scenario, load_scenario
from va_sim.settings import Latencies, SimSettings
from va_sim.state import SimState


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Re-exported by each conftest: no ``SIM_*`` variable or developer ``.env`` leaks into a test."""
    isolate_env(monkeypatch, tmp_path, SimSettings)


LATENCIES = Latencies(stt_final_ms=60, tts_ttfa_ms=80, ttft_ms=150, tokens_per_s=80.0)


def new_state(seed: int = 0) -> SimState:
    return SimState(clock=AcceleratedClock(), latencies=LATENCIES, seed=seed)


def scenario_yaml(
    scenario_id: str, *, to: str | None = "+14155550101", inbound_from: str | None = None
) -> str:
    binding = f'to: "{to}"' if inbound_from is None else f'inbound_from: "{inbound_from}"'
    answer = "answer: {after_ms: 1000}\n" if inbound_from is None else ""
    return f"id: {scenario_id}\nmatch: {{{binding}}}\n{answer}steps:\n  - hangup: {{}}\n"


def scenario(scenario_id: str, **binding: str | None) -> Scenario:
    return load_scenario(scenario_yaml(scenario_id, **binding))
