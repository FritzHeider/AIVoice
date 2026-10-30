"""Shared simulator state (specs/30-testing-and-evals.md §3): one ``SimState`` serves all four simulators.

The state is seeded so every random choice a simulator makes (ids, jitter, synthetic audio) is reproducible:
two states built with the same seed and driven by the same requests behave identically.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Final

import httpx
from pydantic import JsonValue

from va_sim.clock import SimClock
from va_sim.ids import SimIds
from va_sim.scenario import Scenario
from va_sim.settings import PROFILES, Latencies, ProfileName, WebhookDefaults
from va_sim.tasks import Supervisor
from va_sim.telnyx.keys import SigningKey
from va_sim.telnyx.store import TelnyxStore
from va_sim.telnyx.webhooks import WebhookEmitter

__all__ = ["DEFAULT_SEED", "SimState", "Utterance"]

DEFAULT_SEED: Final = 0


@dataclass(frozen=True, slots=True)
class Utterance:
    """A synthetic caller utterance (specs/30 §3.1 registry entry)."""

    text: str
    language: str
    words: tuple[str, ...]


class SimState:
    """Scenarios, recordings, utterances, latencies, clock, and the seeded RNG shared by the simulators.

    FakeTelnyx adds its resource store, the webhook signing key (kept across resets), the webhook emitter, and
    the supervisor owning every simulator background task. ``webhook_transport`` lets tests receive webhooks
    in-process (an ``httpx`` transport) instead of over the network.
    """

    def __init__(  # noqa: PLR0913 - keyword-only collaborators and test seams
        self,
        *,
        clock: SimClock,
        latencies: Latencies,
        seed: int = DEFAULT_SEED,
        signing_key: SigningKey | None = None,
        webhook_defaults: WebhookDefaults | None = None,
        webhook_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.clock = clock
        self._base_latencies = latencies
        self.seed = seed
        self.rng = random.Random(seed)  # noqa: S311 - deterministic simulation, not cryptography
        self.ids = SimIds(self.rng)
        self.tasks = Supervisor()
        self.signing_key = signing_key if signing_key is not None else SigningKey.generate()
        self.webhook_defaults = webhook_defaults if webhook_defaults is not None else WebhookDefaults()
        self.telnyx = TelnyxStore()
        self.webhooks = WebhookEmitter(
            signing_key=self.signing_key,
            clock=clock,
            ids=self.ids,
            tasks=self.tasks,
            config=lambda: self.telnyx.config,
            transport=webhook_transport,
        )
        self.latencies = latencies
        self.scenarios: dict[str, Scenario] = {}
        self.recordings: dict[str, bytes] = {}
        self.utterances: dict[int, Utterance] = {}
        self._by_destination: dict[str, str] = {}
        self._by_inbound: dict[str, str] = {}

    def reset(self, seed: int | None = None) -> None:
        """Forget everything but the signing key; restore latencies; reseed (same seed when None).

        Scenarios, recordings, utterances, FakeTelnyx resources, and webhook deliveries are dropped and every
        pending background task is cancelled.
        """
        self.tasks.cancel_all()
        self.telnyx.reset()
        self.webhooks.clear()
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
            "telnyx": {
                "accounts": len(self.telnyx.accounts),
                "messages": sum(len(account.messages) for account in self.telnyx.accounts.values()),
                "phone_numbers": sum(len(account.numbers) for account in self.telnyx.accounts.values()),
                "webhook_deliveries": len(self.webhooks.deliveries),
            },
        }

    async def aclose(self) -> None:
        """Cancel background tasks and close the webhook HTTP client (simulator shutdown)."""
        await self.tasks.aclose()
        await self.webhooks.aclose()
