"""SimSettings: specs/02 §6 defaults, rule path parsing, latency profiles, validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from service_helpers import settings_of

from va_sim.settings import PROFILES, SIMULATORS, Latencies, SimSettings


def test_defaults_match_specs_02_section_6() -> None:
    settings = settings_of(SimSettings)
    assert settings.service_name == "sim"
    assert settings.sim_clock == "realtime"
    assert settings.latencies() == Latencies(stt_final_ms=60, tts_ttfa_ms=80, ttft_ms=150, tokens_per_s=80.0)
    assert settings.sim_llm_rules == ""
    assert settings.llm_rule_paths == ()
    assert settings.sim_ed25519_private_key is None
    assert settings.sim_edge_webhook_url == "http://localhost:8100/telnyx/voice"
    assert settings.sim_messaging_webhook_url == "http://localhost:8100/telnyx/messaging"
    assert settings.sim_stripe_webhook_url == "http://localhost:8000/v1/billing/stripe/webhook"
    assert (settings.sim_state_redis_url, settings.sim_workers) == ("", 1)


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_CLOCK", "accelerated")
    monkeypatch.setenv("SIM_LLM_RULES", "a.yaml, ,b.yaml,")
    monkeypatch.setenv("SIM_LLM_TOKENS_PER_S", "12.5")
    monkeypatch.setenv("SIM_ED25519_PRIVATE_KEY", "c2ltLWtleQ==")
    settings = settings_of(SimSettings)
    assert settings.sim_clock == "accelerated"
    assert settings.llm_rule_paths == (Path("a.yaml"), Path("b.yaml"))
    assert settings.latencies().tokens_per_s == 12.5
    assert settings.sim_ed25519_private_key is not None
    assert "c2ltLWtleQ" not in repr(settings)


def test_profiles_match_spec() -> None:
    assert PROFILES["fast"] == Latencies(stt_final_ms=10, tts_ttfa_ms=20, ttft_ms=20, tokens_per_s=1000.0)
    assert PROFILES["realistic"] == Latencies(
        stt_final_ms=40, tts_ttfa_ms=250, ttft_ms=250, tokens_per_s=80.0
    )
    assert SIMULATORS == ("telnyx", "elevenlabs", "llm", "stripe")


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SIM_CLOCK", "warp"),
        ("SIM_WORKERS", "0"),
        ("SIM_LLM_TOKENS_PER_S", "0"),
        ("SIM_TTS_TTFA_MS", "-1"),
    ],
)
def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError, match=variable.lower()):
        settings_of(SimSettings)
