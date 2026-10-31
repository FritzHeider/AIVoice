"""Simulator settings (specs/30-testing-and-evals.md §3 table; specs/02-repo-layout-and-conventions.md §6).

``SimSettings`` reads every ``SIM_*`` variable. Vendor latencies come either from these variables
(realtime-mode defaults) or from a named profile (``fast``, ``realistic``) selected by scenarios or the
control API.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Final, Literal

from pydantic import Field, SecretStr

from va_common.settings import RedisSettings

__all__ = [
    "DEFAULT_EDGE_WEBHOOK_URL",
    "DEFAULT_LLM_MODELS",
    "DEFAULT_MESSAGING_WEBHOOK_URL",
    "PROFILES",
    "SIMULATORS",
    "ClockMode",
    "Latencies",
    "ProfileName",
    "SimName",
    "SimSettings",
    "WebhookDefaults",
]

type ClockMode = Literal["realtime", "accelerated"]
type ProfileName = Literal["fast", "realistic"]
type SimName = Literal["telnyx", "elevenlabs", "llm", "stripe"]

# Port order (specs/01 §1): fake Telnyx 9100, ElevenLabs 9200, Telnyx Inference 9300, Stripe 9400.
SIMULATORS: Final[tuple[SimName, ...]] = ("telnyx", "elevenlabs", "llm", "stripe")

# specs/02 §6 defaults of SIM_EDGE_WEBHOOK_URL and SIM_MESSAGING_WEBHOOK_URL.
DEFAULT_EDGE_WEBHOOK_URL: Final = "http://localhost:8100/telnyx/voice"
DEFAULT_MESSAGING_WEBHOOK_URL: Final = "http://localhost:8100/telnyx/messaging"

# specs/02 §6 defaults: LLM_DIALOGUE_MODEL (= LLM_ROUTER_MODEL), LLM_ANALYSIS_MODEL, LLM_FALLBACK_MODEL,
# EMBEDDINGS_MODEL.
DEFAULT_LLM_MODELS: Final[tuple[str, ...]] = (
    "zai-org/GLM-5.3-Flash",
    "zai-org/GLM-5.3",
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3-Embedding-8B",
)


@dataclass(frozen=True, slots=True)
class Latencies:
    """Vendor latency knobs shared by every simulator (control-API names of specs/30 §3)."""

    stt_final_ms: int
    tts_ttfa_ms: int
    ttft_ms: int
    tokens_per_s: float


PROFILES: Final[Mapping[ProfileName, Latencies]] = MappingProxyType(
    {
        "fast": Latencies(stt_final_ms=10, tts_ttfa_ms=20, ttft_ms=20, tokens_per_s=1000.0),
        "realistic": Latencies(stt_final_ms=40, tts_ttfa_ms=250, ttft_ms=250, tokens_per_s=80.0),
    }
)


@dataclass(frozen=True, slots=True)
class WebhookDefaults:
    """Webhook targets FakeTelnyx falls back to when neither the request nor a resource names one."""

    voice_url: str = DEFAULT_EDGE_WEBHOOK_URL
    messaging_url: str = DEFAULT_MESSAGING_WEBHOOK_URL


class SimSettings(RedisSettings):
    """Simulator process settings; the defaults are the realtime-mode values of specs/02 §6."""

    service_name: ClassVar[str] = "sim"

    sim_clock: ClockMode = "realtime"
    sim_stt_commit_latency_ms: int = Field(default=60, ge=0)
    sim_tts_ttfa_ms: int = Field(default=80, ge=0)
    sim_llm_ttft_ms: int = Field(default=150, ge=0)
    sim_llm_tokens_per_s: float = Field(default=80.0, gt=0)
    sim_llm_rules: str = ""
    # Empty → a fresh keypair is generated when FakeTelnyx starts.
    sim_ed25519_private_key: SecretStr | None = None
    sim_edge_webhook_url: str = DEFAULT_EDGE_WEBHOOK_URL
    sim_messaging_webhook_url: str = DEFAULT_MESSAGING_WEBHOOK_URL
    sim_stripe_webhook_url: str = "http://localhost:8000/v1/billing/stripe/webhook"
    # Empty → in-process state only, and no readiness check.
    sim_state_redis_url: str = ""
    sim_workers: int = Field(default=1, ge=1)
    # Model ids FakeLLM lists at GET /v1/models (shared specs/02 §6 variables, specs/30 §3.4).
    llm_dialogue_model: str = DEFAULT_LLM_MODELS[0]
    llm_router_model: str = DEFAULT_LLM_MODELS[0]
    llm_analysis_model: str = DEFAULT_LLM_MODELS[1]
    llm_fallback_model: str = DEFAULT_LLM_MODELS[2]
    embeddings_model: str = DEFAULT_LLM_MODELS[3]

    @property
    def llm_rule_paths(self) -> tuple[Path, ...]:
        """``SIM_LLM_RULES`` split on commas, blanks dropped."""
        return tuple(Path(part.strip()) for part in self.sim_llm_rules.split(",") if part.strip())

    def llm_model_ids(self) -> tuple[str, ...]:
        """The dialogue, router, analysis, fallback, and embeddings model ids (blanks, duplicates dropped)."""
        ids = (
            self.llm_dialogue_model,
            self.llm_router_model,
            self.llm_analysis_model,
            self.llm_fallback_model,
            self.embeddings_model,
        )
        return tuple(dict.fromkeys(model.strip() for model in ids if model.strip()))

    def latencies(self) -> Latencies:
        """The latencies configured through the environment."""
        return Latencies(
            stt_final_ms=self.sim_stt_commit_latency_ms,
            tts_ttfa_ms=self.sim_tts_ttfa_ms,
            ttft_ms=self.sim_llm_ttft_ms,
            tokens_per_s=self.sim_llm_tokens_per_s,
        )

    def webhook_defaults(self) -> WebhookDefaults:
        """The FakeTelnyx webhook fallbacks configured through the environment."""
        return WebhookDefaults(
            voice_url=self.sim_edge_webhook_url, messaging_url=self.sim_messaging_webhook_url
        )
