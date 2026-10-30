"""running_sims: four real servers on ephemeral ports over one shared state; build_state from settings."""

from __future__ import annotations

import asyncio
import base64
import socket

import httpx
import pytest
from nacl.signing import SigningKey as NaclSigningKey
from service_helpers import settings_of
from sim_helpers import new_state, scenario_yaml

from va_common.service import ServerStartupError
from va_sim.clock import AcceleratedClock, RealtimeClock
from va_sim.server import SIMULATORS, build_state, ports_from, running_sims
from va_sim.settings import SimSettings, WebhookDefaults


def test_ports_follow_the_spec_layout() -> None:
    assert ports_from(9100) == {"telnyx": 9100, "elevenlabs": 9200, "llm": 9300, "stripe": 9400}


def test_build_state_uses_settings() -> None:
    accelerated = build_state(settings_of(SimSettings, sim_clock="accelerated", sim_llm_ttft_ms=7), seed=4)
    assert isinstance(accelerated.clock, AcceleratedClock)
    assert accelerated.latencies.ttft_ms == 7
    assert accelerated.seed == 4
    assert isinstance(build_state(settings_of(SimSettings)).clock, RealtimeClock)


def test_build_state_signing_key_and_webhook_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    seed = base64.b64encode(bytes(range(32))).decode()
    expected = base64.b64encode(bytes(NaclSigningKey(bytes(range(32))).verify_key)).decode()
    monkeypatch.setenv("SIM_ED25519_PRIVATE_KEY", seed)
    monkeypatch.setenv("SIM_MESSAGING_WEBHOOK_URL", "http://edge:8100/telnyx/messaging")
    configured = build_state(SimSettings())
    assert configured.signing_key.public_key_b64 == expected
    assert configured.webhook_defaults == WebhookDefaults(
        voice_url="http://localhost:8100/telnyx/voice", messaging_url="http://edge:8100/telnyx/messaging"
    )
    monkeypatch.setenv("SIM_ED25519_PRIVATE_KEY", "")
    blank = build_state(SimSettings())
    assert blank.signing_key.public_key_b64 != expected
    monkeypatch.delenv("SIM_MESSAGING_WEBHOOK_URL")
    assert build_state(settings_of(SimSettings)).webhook_defaults == WebhookDefaults()


async def _block_forever() -> None:
    await asyncio.Event().wait()


async def test_running_sims_share_state_and_stop_on_exit() -> None:
    state = new_state()
    async with running_sims(state, ports=dict.fromkeys(SIMULATORS, 0)) as urls, httpx.AsyncClient() as client:
        pending = state.tasks.spawn(_block_forever(), name="pending-at-exit")
        assert list(urls) == list(SIMULATORS)
        assert len(set(urls.values())) == 4
        created = await client.post(
            f"{urls['telnyx']}/sim/telnyx/scenarios",
            content=scenario_yaml("shared"),
            headers={"content-type": "application/yaml"},
        )
        snapshot = await client.get(f"{urls['llm']}/sim/state")
    assert created.status_code == 201
    assert snapshot.json()["scenarios"] == ["shared"]
    # Leaving running_sims closes the state: pending background tasks are cancelled.
    assert pending.cancelled()
    assert state.tasks.pending == 0
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"{urls['telnyx']}/sim/clock")


async def test_running_a_subset_with_defaults() -> None:
    async with running_sims(new_state(), ports={"llm": 0}) as urls, httpx.AsyncClient() as client:
        response = await client.get(f"{urls['llm']}/sim/clock")
    assert list(urls) == ["llm"]
    assert response.json()["mode"] == "accelerated"
    async with running_sims(new_state()) as urls:
        assert list(urls) == list(SIMULATORS)


async def test_a_port_in_use_fails_startup() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(ServerStartupError):
            async with running_sims(new_state(), ports={"telnyx": 0, "stripe": port}):
                pytest.fail("startup should have failed")
