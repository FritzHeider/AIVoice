"""SimState: scenario indexes, utterance ids, seeding, reset, latencies, snapshot."""

from __future__ import annotations

import pytest
from sim_helpers import LATENCIES, new_state, scenario

from va_sim.audio import MAX_UTTERANCE_ID
from va_sim.settings import PROFILES


def test_register_indexes_outbound_and_inbound_scenarios() -> None:
    state = new_state()
    outbound = scenario("out", to="+14155550101")
    inbound = scenario("in", inbound_from="+14155550199")
    state.register_scenario(outbound)
    state.register_scenario(inbound)
    assert state.scenario_for_destination("+14155550101") == outbound
    assert state.scenario_for_inbound("+14155550199") == inbound
    assert state.scenario_for_destination("+14155550199") is None
    assert state.scenario_for_inbound("+14155550101") is None


def test_re_registering_an_id_replaces_its_binding() -> None:
    state = new_state()
    state.register_scenario(scenario("same", to="+14155550101"))
    replacement = scenario("same", to="+14155550102")
    state.register_scenario(replacement)
    assert state.scenarios == {"same": replacement}
    assert state.scenario_for_destination("+14155550101") is None
    assert state.scenario_for_destination("+14155550102") == replacement


def test_a_newer_scenario_takes_over_a_number_without_losing_it_on_replacement() -> None:
    state = new_state()
    state.register_scenario(scenario("first", to="+14155550101"))
    second = scenario("second", to="+14155550101")
    state.register_scenario(second)
    state.register_scenario(scenario("first", to="+14155550103"))
    assert state.scenario_for_destination("+14155550101") == second


def test_utterance_ids_are_sequential_from_one() -> None:
    state = new_state()
    first = state.register_utterance("Hi there.", "en", ("Hi", "there."))
    second = state.register_utterance("Hola.", "es", ("Hola.",))
    assert (first, second) == (1, 2)
    assert state.utterances[2].language == "es"
    assert state.utterances[1].words == ("Hi", "there.")


def test_utterance_ids_stop_at_the_16_bit_marker_limit() -> None:
    state = new_state()
    for _ in range(MAX_UTTERANCE_ID - 1):
        state.register_utterance("x", "en", ("x",))
    assert state.register_utterance("last", "en", ("last",)) == MAX_UTTERANCE_ID
    with pytest.raises(ValueError, match="16-bit"):
        state.register_utterance("one too many", "en", ("one", "too", "many"))
    state.reset()
    assert state.register_utterance("again", "en", ("again",)) == 1


def test_utterance_seed_combines_run_seed_and_id() -> None:
    state = new_state(seed=3)
    assert state.utterance_seed(7) == 3 * 65_536 + 7
    assert state.utterance_seed(8) != state.utterance_seed(7)
    state.reset(seed=4)
    assert state.utterance_seed(7) == 4 * 65_536 + 7
    assert new_state().utterance_seed(MAX_UTTERANCE_ID) == MAX_UTTERANCE_ID


def test_same_seed_same_random_sequence() -> None:
    one, two = new_state(seed=7), new_state(seed=7)
    assert [one.rng.random() for _ in range(5)] == [two.rng.random() for _ in range(5)]
    assert new_state(seed=8).rng.random() != new_state(seed=7).rng.random()


def test_reset_clears_everything_and_reseeds() -> None:
    state = new_state(seed=3)
    expected = [state.rng.random() for _ in range(3)]
    state.register_scenario(scenario("gone"))
    state.store_recording("rec", b"RIFF")
    state.register_utterance("x", "en", ("x",))
    state.apply_profile("realistic")
    state.reset()
    assert (state.scenarios, state.recordings, state.utterances) == ({}, {}, {})
    assert state.scenario_for_destination("+14155550101") is None
    assert state.latencies == LATENCIES
    assert [state.rng.random() for _ in range(3)] == expected
    assert state.register_utterance("again", "en", ("again",)) == 1
    state.reset(seed=7)
    assert state.seed == 7
    assert state.rng.random() == new_state(seed=7).rng.random()


def test_latency_updates_and_profiles() -> None:
    state = new_state()
    state.update_latencies(ttft_ms=5, tokens_per_s=500.0)
    assert (state.latencies.ttft_ms, state.latencies.tokens_per_s) == (5, 500.0)
    assert state.latencies.tts_ttfa_ms == LATENCIES.tts_ttfa_ms
    state.apply_profile("fast")
    assert state.latencies == PROFILES["fast"]
    state.update_latencies()
    assert state.latencies == PROFILES["fast"]


def test_snapshot_shape() -> None:
    state = new_state(seed=11)
    state.register_scenario(scenario("b"))
    state.register_scenario(scenario("a", to="+14155550102"))
    state.store_recording("r1", b"RIFF")
    assert state.snapshot() == {
        "seed": 11,
        "clock": {"mode": "accelerated", "virtual_ms": 0},
        "latencies": {"stt_final_ms": 60, "tts_ttfa_ms": 80, "ttft_ms": 150, "tokens_per_s": 80.0},
        "scenarios": ["a", "b"],
        "recordings": 1,
        "utterances": 0,
        "telnyx": {
            "accounts": 1,
            "messages": 0,
            "calls": 0,
            "conferences": 0,
            "phone_numbers": 0,
            "webhook_deliveries": 0,
        },
        "elevenlabs": {
            "tts_requests": 0,
            "tts_sockets": 0,
            "stt_sessions": 0,
            "stt_sockets": 0,
            "stt_batch": 0,
        },
    }


async def test_reset_keeps_the_signing_key_and_drops_telnyx_accounts() -> None:
    state = new_state()
    key = state.signing_key.public_key_b64
    state.telnyx.account("byo-key")
    first_id = state.ids.uuid()
    state.reset()
    assert state.signing_key.public_key_b64 == key
    assert set(state.telnyx.accounts) == {"sim-telnyx-key"}
    assert state.ids.uuid() == first_id
    await state.aclose()
