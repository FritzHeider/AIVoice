"""Shared simulator state (specs/30-testing-and-evals.md §3): one ``SimState`` serves all four simulators.

The state is seeded so every random choice a simulator makes (ids, jitter, synthetic audio) is reproducible:
two states built with the same seed and driven by the same requests behave identically.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Final

from pydantic import JsonValue

from va_sim.clock import SimClock
from va_sim.scenario import Scenario
from va_sim.settings import PROFILES, Latencies, ProfileName

__all__ = ["DEFAULT_SEED", "SimState", "Utterance"]

DEFAULT_SEED: Final = 0


@dataclass(frozen=True, slots=True)
class Utterance:
    """A synthetic caller utterance (specs/30 §3.1 registry entry)."""

    text: str
    language: str
    words: tuple[str, ...]


class SimState:
    """Scenarios, recordings, utterances, latencies, clock, and the seeded RNG shared by the simulators."""

    def __init__(self, *, clock: SimClock, latencies: Latencies, seed: int = DEFAULT_SEED) -> None:
        self.clock = clock
        self._base_latencies = latencies
        self.seed = seed
        self.rng = random.Random(seed)  # noqa: S311 - deterministic simulation, not cryptography
        self.latencies = latencies
        self.scenarios: dict[str, Scenario] = {}
        self.recordings: dict[str, bytes] = {}
        self.utterances: dict[int, Utterance] = {}
        self._by_destination: dict[str, str] = {}
        self._by_inbound: dict[str, str] = {}

    def reset(self, seed: int | None = None) -> None:
        """Forget scenarios, recordings, utterances; restore latencies; reseed (same seed when None)."""
        if seed is not None:
            self.seed = seed
        self.rng.seed(self.seed)
        self.latencies = self._base_latencies
        self.scenarios.clear()
        self.recordings.clear()
        self.utterances.clear()
        self._by_destination.clear()
        self._by_inbound.clear()

    def register_scenario(self, scenario: Scenario) -> None:
        """Add ``scenario``, replacing any scenario with the same id (and its number binding)."""
        previous = self.scenarios.pop(scenario.id, None)
        if previous is not None:
            self._unbind(previous)
        self.scenarios[scenario.id] = scenario
        if scenario.match.to is not None:
            self._by_destination[scenario.match.to] = scenario.id
        if scenario.match.inbound_from is not None:
            self._by_inbound[scenario.match.inbound_from] = scenario.id

    def _unbind(self, scenario: Scenario) -> None:
        for index, number in (
            (self._by_destination, scenario.match.to),
            (self._by_inbound, scenario.match.inbound_from),
        ):
            if number is not None and index.get(number) == scenario.id:
                del index[number]

    def scenario_for_destination(self, to: str) -> Scenario | None:
        """The outbound scenario bound to destination ``to``."""
        scenario_id = self._by_destination.get(to)
        return self.scenarios[scenario_id] if scenario_id is not None else None

    def scenario_for_inbound(self, from_: str) -> Scenario | None:
        """The inbound scenario bound to caller number ``from_``."""
        scenario_id = self._by_inbound.get(from_)
        return self.scenarios[scenario_id] if scenario_id is not None else None

    def store_recording(self, recording_id: str, wav: bytes) -> None:
        self.recordings[recording_id] = wav

    def register_utterance(self, text: str, language: str, words: tuple[str, ...]) -> int:
        """Allocate the next utterance id (sequential from 1 per sim run)."""
        utterance_id = len(self.utterances) + 1
        self.utterances[utterance_id] = Utterance(text=text, language=language, words=words)
        return utterance_id

    def apply_profile(self, profile: ProfileName) -> None:
        self.latencies = PROFILES[profile]

    def update_latencies(
        self,
        *,
        stt_final_ms: int | None = None,
        tts_ttfa_ms: int | None = None,
        ttft_ms: int | None = None,
        tokens_per_s: float | None = None,
    ) -> None:
        """Override latencies (control API ``PATCH /sim/{sim}/config``); None keeps the current value."""
        current = self.latencies
        self.latencies = Latencies(
            stt_final_ms=current.stt_final_ms if stt_final_ms is None else stt_final_ms,
            tts_ttfa_ms=current.tts_ttfa_ms if tts_ttfa_ms is None else tts_ttfa_ms,
            ttft_ms=current.ttft_ms if ttft_ms is None else ttft_ms,
            tokens_per_s=current.tokens_per_s if tokens_per_s is None else tokens_per_s,
        )

    def snapshot(self) -> dict[str, JsonValue]:
        """Summary served at ``GET /sim/state``."""
        latencies: dict[str, JsonValue] = dict(asdict(self.latencies))
        scenarios: list[JsonValue] = [*sorted(self.scenarios)]
        return {
            "seed": self.seed,
            "clock": {"mode": self.clock.mode, "virtual_ms": self.clock.virtual_ms()},
            "latencies": latencies,
            "scenarios": scenarios,
            "recordings": len(self.recordings),
            "utterances": len(self.utterances),
        }
