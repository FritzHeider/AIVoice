"""Shared simulator state (specs/30-testing-and-evals.md §3): one ``SimState`` serves all four simulators.

The state is seeded so every random choice a simulator makes (ids, jitter, synthetic audio) is reproducible:
two states built with the same seed and driven by the same requests behave identically.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import asdict
from typing import Final

import httpx
from pydantic import JsonValue

from va_common.logging import get_logger
from va_sim.audio.marker import MAX_UTTERANCE_ID
from va_sim.clock import SimClock
from va_sim.elevenlabs.store import ElevenLabsStore
from va_sim.ids import SimIds
from va_sim.llm.store import LlmStore
from va_sim.scenario import Scenario
from va_sim.settings import (
    DEFAULT_LLM_MODELS,
    PROFILES,
    Latencies,
    ProfileName,
    StripeDefaults,
    WebhookDefaults,
)
from va_sim.shared import (
    SharedSnapshot,
    SharedState,
    SharedStateUnavailable,
    UtteranceLimitError,
)
from va_sim.stripe.store import StripeStore
from va_sim.stripe.webhooks import StripeWebhookEmitter
from va_sim.tasks import Supervisor
from va_sim.telnyx.call_engine import CallEngine
from va_sim.telnyx.keys import SigningKey
from va_sim.telnyx.store import TelnyxStore
from va_sim.telnyx.webhooks import WebhookEmitter
from va_sim.utterances import Utterance

__all__ = ["DEFAULT_SEED", "SimState", "Utterance"]

DEFAULT_SEED: Final = 0
_UTTERANCE_SEED_STRIDE: Final = MAX_UTTERANCE_ID + 1
_FETCH_TIMEOUT_S: Final = 1.0

_log: Final = get_logger(__name__)


class SimState:
    """Scenarios, recordings, utterances, latencies, clock, and the seeded RNG shared by the simulators.

    FakeTelnyx adds its resource store, the webhook signing key (kept across resets), the webhook emitter,
    the call engine, and the supervisor owning every simulator background task; FakeElevenLabs its store
    (accepted keys, TTS request records, the armed fault, live TTS sockets); FakeStripe its store and
    webhook emitter. ``webhook_transport`` lets tests receive webhooks (FakeTelnyx and FakeStripe) in-process
    (an ``httpx`` transport) instead of over the network.
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
        llm_models: Sequence[str] = DEFAULT_LLM_MODELS,
        stripe_defaults: StripeDefaults | None = None,
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
        self.elevenlabs = ElevenLabsStore()
        self.llm = LlmStore(llm_models)
        self.stripe = StripeStore(stripe_defaults)
        self.stripe_webhooks = StripeWebhookEmitter(
            clock=clock, tasks=self.tasks, config=lambda: self.stripe.config, transport=webhook_transport
        )
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
        self.call_engine = CallEngine(self)
        self._shared: SharedState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sync_lock = asyncio.Lock()
        self._applied = asyncio.Condition()
        self.applied_version: int | None = None
        self._applied_generation: int | None = None
        self._shared_utterances = 0

    def reset(self, seed: int | None = None) -> None:
        """Forget everything but the signing key; restore latencies; reseed (same seed when None).

        Scenarios, recordings, utterances, FakeTelnyx resources (calls and conferences included), and
        webhook deliveries are dropped and every pending background task (call progress, timers, deliveries)
        is cancelled. Live FakeElevenLabs TTS and STT sockets close with code 1012 and its records, keys,
        faults, and STT transcript script are dropped; the voice registry is reseeded. FakeLLM drops runtime
        rules, ``call_index`` counters, its request log, fault, and added keys and models, and releases every
        request a ``timeout`` fault holds (``SIM_LLM_RULES`` file rules stay). FakeStripe drops its objects,
        events, idempotency cache, added keys, and webhook deliveries and restores its config to the defaults.
        """
        self.tasks.cancel_all()
        self.call_engine.reset()
        self.telnyx.reset()
        self.webhooks.clear()
        self.elevenlabs.reset()
        self.llm.reset()
        self.stripe.reset()
        self.stripe_webhooks.clear()
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
        """Allocate the next utterance id (sequential from 1 per sim run, up to the 16-bit marker limit)."""
        if len(self.utterances) >= MAX_UTTERANCE_ID:
            raise UtteranceLimitError
        utterance_id = len(self.utterances) + 1
        self.utterances[utterance_id] = Utterance(text=text, language=language, words=words)
        return utterance_id

    def utterance_seed(self, utterance_id: int) -> int:
        """Synthesis seed of ``utterance_id``: ``seed · 65 536 + utterance_id`` (specs/30 §3.1)."""
        return self.seed * _UTTERANCE_SEED_STRIDE + utterance_id

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
        self.latencies = self._merged_latencies(
            stt_final_ms=stt_final_ms, tts_ttfa_ms=tts_ttfa_ms, ttft_ms=ttft_ms, tokens_per_s=tokens_per_s
        )

    def _merged_latencies(
        self,
        *,
        stt_final_ms: int | None,
        tts_ttfa_ms: int | None,
        ttft_ms: int | None,
        tokens_per_s: float | None,
    ) -> Latencies:
        current = self.latencies
        return Latencies(
            stt_final_ms=current.stt_final_ms if stt_final_ms is None else stt_final_ms,
            tts_ttfa_ms=current.tts_ttfa_ms if tts_ttfa_ms is None else tts_ttfa_ms,
            ttft_ms=current.ttft_ms if ttft_ms is None else ttft_ms,
            tokens_per_s=current.tokens_per_s if tokens_per_s is None else tokens_per_s,
        )

    # Shared control plane (specs/30 §3 Decision P03-T15c). Without a SharedState every method below acts on
    # this process only, exactly like the synchronous methods above.

    @property
    def shared(self) -> bool:
        return self._shared is not None

    async def attach_shared(self, shared: SharedState) -> None:
        """Share scenarios, latencies, reset + seed, and utterances through ``shared``; adopt what it holds
        already (attaching never wipes the shared records)."""
        self._shared = shared
        self._loop = asyncio.get_running_loop()
        await self.sync()

    async def sync(self) -> None:
        """Reload the shared records when another process changed them (no-op when unshared).

        A Redis failure is logged (``sim.shared_state.sync_failed``) and the local copy stays in use.
        """
        shared = self._shared
        if shared is None:
            return
        try:
            async with self._sync_lock:
                head = await shared.head()
                self._shared_utterances = head.utterances
                if head.version != self.applied_version:
                    self._apply(await shared.load())
        except SharedStateUnavailable as exc:
            _log.warning("sim.shared_state.sync_failed", error=str(exc))
            return
        await self._notify()

    def _apply(self, snapshot: SharedSnapshot) -> None:
        if snapshot.generation != self._applied_generation:
            self.reset(snapshot.seed)
            self._applied_generation = snapshot.generation
        self.scenarios.clear()
        self._by_destination.clear()
        self._by_inbound.clear()
        for scenario in snapshot.scenarios:
            self.register_scenario(scenario)
        self.latencies = snapshot.latencies if snapshot.latencies is not None else self._base_latencies
        self.applied_version = snapshot.version

    def _adopt(self, version: int) -> None:
        """After a local write: skip the reload when no other write happened since the last applied one."""
        if self.applied_version is not None and version == self.applied_version + 1:
            self.applied_version = version

    async def _notify(self) -> None:
        async with self._applied:
            self._applied.notify_all()

    async def wait_applied(self, version: int) -> None:
        """Wait until the local copy reflects shared ``version`` (or a later one)."""
        async with self._applied:
            await self._applied.wait_for(lambda: (self.applied_version or 0) >= version)

    async def run_subscriber(self) -> None:
        """Follow the shared change channel until cancelled (returns at once when unshared)."""
        if self._shared is not None:
            await self._shared.subscribe(self.sync)

    async def reset_all(self, seed: int | None = None) -> None:
        """:meth:`reset` here and, when shared, in every process (raises :class:`SharedStateUnavailable`)."""
        shared = self._shared
        if shared is None:
            self.reset(seed)
            return
        async with self._sync_lock:
            result = await shared.reset(seed)
            self.reset(result.seed)
            self._applied_generation = result.generation
            self.applied_version = result.version
            self._shared_utterances = 0
        await self._notify()

    async def add_scenario(self, scenario: Scenario) -> None:
        """:meth:`register_scenario`, shared with every process when shared."""
        shared = self._shared
        if shared is None:
            self.register_scenario(scenario)
            return
        async with self._sync_lock:
            version = await shared.put_scenario(scenario)
            self.register_scenario(scenario)
            self._adopt(version)
        await self._notify()

    async def set_latencies(
        self,
        *,
        stt_final_ms: int | None = None,
        tts_ttfa_ms: int | None = None,
        ttft_ms: int | None = None,
        tokens_per_s: float | None = None,
    ) -> None:
        """:meth:`update_latencies`, shared with every process when shared."""
        await self._store_latencies(
            self._merged_latencies(
                stt_final_ms=stt_final_ms, tts_ttfa_ms=tts_ttfa_ms, ttft_ms=ttft_ms, tokens_per_s=tokens_per_s
            )
        )

    async def set_profile(self, profile: ProfileName) -> None:
        """:meth:`apply_profile`, shared with every process when shared."""
        await self._store_latencies(PROFILES[profile])

    async def _store_latencies(self, latencies: Latencies) -> None:
        shared = self._shared
        if shared is None:
            self.latencies = latencies
            return
        async with self._sync_lock:
            version = await shared.put_latencies(latencies)
            self.latencies = latencies
            self._adopt(version)
        await self._notify()

    async def allocate_utterance(self, text: str, language: str, words: tuple[str, ...]) -> int:
        """:meth:`register_utterance`; when shared the id is unique across every process.

        Raises :class:`UtteranceLimitError` (a ``ValueError``) when the ids are exhausted and
        :class:`SharedStateUnavailable` when the shared Redis fails.
        """
        shared = self._shared
        if shared is None:
            return self.register_utterance(text, language, words)
        utterance = Utterance(text=text, language=language, words=words)
        utterance_id = await shared.allocate_utterance(utterance)
        self.utterances[utterance_id] = utterance
        self._shared_utterances = max(self._shared_utterances, utterance_id)
        return utterance_id

    def resolve_utterance(self, utterance_id: int) -> Utterance | None:
        """The utterance with ``utterance_id``: the local registry first, then (shared, called from a worker
        thread) the shared registry with a 1 s bound; None when unknown or the lookup fails."""
        utterance = self.utterances.get(utterance_id)
        shared, loop = self._shared, self._loop
        if utterance is not None or shared is None or loop is None or _on_loop_thread(loop):
            return utterance
        future = asyncio.run_coroutine_threadsafe(shared.fetch_utterance(utterance_id), loop)
        try:
            fetched = future.result(timeout=_FETCH_TIMEOUT_S)
        except (SharedStateUnavailable, TimeoutError) as exc:
            future.cancel()
            _log.warning(
                "sim.shared_state.utterance_lookup_failed", utterance_id=utterance_id, error=repr(exc)
            )
            return None
        if fetched is not None:
            self.utterances[utterance_id] = fetched
        return fetched

    async def known_utterances(self) -> dict[int, Utterance]:
        """Every utterance this process can resolve (when shared: the whole shared registry)."""
        shared = self._shared
        if shared is not None:
            try:
                self.utterances.update(await shared.utterances())
            except SharedStateUnavailable as exc:
                _log.warning("sim.shared_state.sync_failed", error=str(exc))
        return dict(self.utterances)

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
            "utterances": self._shared_utterances if self._shared is not None else len(self.utterances),
            "telnyx": {
                "accounts": len(self.telnyx.accounts),
                "messages": sum(len(account.messages) for account in self.telnyx.accounts.values()),
                "calls": sum(len(account.calls) for account in self.telnyx.accounts.values()),
                "conferences": sum(len(account.conferences) for account in self.telnyx.accounts.values()),
                "phone_numbers": sum(len(account.numbers) for account in self.telnyx.accounts.values()),
                "webhook_deliveries": len(self.webhooks.deliveries),
            },
            "elevenlabs": {
                "tts_requests": len(self.elevenlabs.tts_requests),
                "tts_sockets": len(self.elevenlabs.sockets),
                "stt_sessions": len(self.elevenlabs.stt.sessions),
                "stt_sockets": len(self.elevenlabs.stt.sockets),
                "stt_batch": len(self.elevenlabs.stt.batch),
                "voices": len(self.elevenlabs.voices),
            },
            "llm": {
                "requests": len(self.llm.requests),
                "embeddings": len(self.llm.embedding_requests),
                "rules": len(self.llm.rules),
                "models": len(self.llm.models),
                "held": self.llm.held,
            },
            "stripe": {
                "customers": len(self.stripe.customers),
                "payment_methods": len(self.stripe.payment_methods),
                "payment_intents": len(self.stripe.payment_intents),
                "products": len(self.stripe.products),
                "prices": len(self.stripe.prices),
                "checkout_sessions": len(self.stripe.checkout_sessions),
                "subscriptions": len(self.stripe.subscriptions),
                "invoices": len(self.stripe.invoices),
                "setup_intents": len(self.stripe.setup_intents),
                "events": len(self.stripe.events),
                "webhook_deliveries": len(self.stripe_webhooks.deliveries),
            },
        }

    async def aclose(self) -> None:
        """Release held FakeLLM requests, cancel background tasks, and close the webhook HTTP clients
        (simulator shutdown)."""
        self.llm.release_held()
        await self.tasks.aclose()
        await self.webhooks.aclose()
        await self.stripe_webhooks.aclose()


def _on_loop_thread(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False
