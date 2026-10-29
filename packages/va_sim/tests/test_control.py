"""The /sim/* control API: reset, clock, state, latency config, scenarios, recordings."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from sim_helpers import new_state, scenario_yaml
from starlette.applications import Starlette

from va_sim.clock import AcceleratedClock
from va_sim.server import simulator_routes
from va_sim.settings import PROFILES, SimName
from va_sim.state import SimState
from va_sim.wav import pcm16_wav

YAML = {"content-type": "application/yaml"}


@asynccontextmanager
async def client_for(sim: SimName, state: SimState) -> AsyncIterator[httpx.AsyncClient]:
    app = Starlette(routes=simulator_routes(sim, state))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as client:
        yield client


async def test_reset_clears_state_and_optionally_reseeds() -> None:
    state = new_state(seed=1)
    state.store_recording("r", b"RIFF")
    async with client_for("llm", state) as client:
        plain = await client.post("/sim/reset")
        assert plain.status_code == 204
        assert (state.recordings, state.seed) == ({}, 1)
        seeded = await client.post("/sim/reset", json={"seed": 42})
        assert seeded.status_code == 204
        assert state.seed == 42
        bad = await client.post("/sim/reset", json={"seed": "x", "extra": 1})
        broken = await client.post("/sim/reset", content=b"{", headers={"content-type": "application/json"})
    assert bad.status_code == 422
    assert {issue["path"] for issue in bad.json()["error"]["issues"]} == {"seed", "extra"}
    assert broken.status_code == 422
    assert broken.json()["error"]["message"] == "the request body is not valid JSON"


async def test_clock_reflects_accelerated_time() -> None:
    state = new_state()
    assert isinstance(state.clock, AcceleratedClock)
    async with client_for("stripe", state) as client:
        before = (await client.get("/sim/clock")).json()
        state.clock.advance(2.5)
        after = (await client.get("/sim/clock")).json()
    assert before == {"mode": "accelerated", "now": "2026-01-01T00:00:00Z", "virtual_ms": 0}
    assert after == {"mode": "accelerated", "now": "2026-01-01T00:00:02.500000Z", "virtual_ms": 2500}


async def test_state_snapshot() -> None:
    state = new_state(seed=5)
    async with client_for("elevenlabs", state) as client:
        response = await client.get("/sim/state")
    assert response.json() == state.snapshot()


async def test_register_yaml_scenario_then_read_it_back() -> None:
    state = new_state()
    async with client_for("telnyx", state) as client:
        created = await client.post("/sim/telnyx/scenarios", content=scenario_yaml("demo"), headers=YAML)
        inbound = await client.post(
            "/sim/telnyx/scenarios",
            content=scenario_yaml("caller", inbound_from="+14155550199"),
            headers={"content-type": "text/yaml; charset=utf-8"},
        )
        detail = await client.get("/sim/telnyx/scenarios/demo")
        listing = await client.get("/sim/telnyx/scenarios")
    assert created.status_code == 201
    assert created.json() == {"id": "demo", "match": {"to": "+14155550101", "inbound_from": None}}
    assert inbound.status_code == 201
    assert detail.json()["answer"]["after_ms"] == 1000
    assert detail.json()["steps"] == [{"hangup": {}}]
    assert [item["id"] for item in listing.json()["scenarios"]] == ["caller", "demo"]
    assert state.scenario_for_inbound("+14155550199") is not None


async def test_invalid_yaml_scenario_reports_issue_lines() -> None:
    body = scenario_yaml("demo") + "colour: red\nprofile: slow\n"
    async with client_for("telnyx", new_state()) as client:
        response = await client.post("/sim/telnyx/scenarios", content=body, headers=YAML)
        undecodable = await client.post("/sim/telnyx/scenarios", content=b"\xff\xfe", headers=YAML)
    assert response.status_code == 422
    error = response.json()["error"]
    assert (error["code"], error["message"]) == ("invalid_request", "the scenario is invalid")
    assert [(issue["path"], issue["line"], issue["column"]) for issue in error["issues"]] == [
        ("colour", 6, 1),
        ("profile", 7, 1),
    ]
    assert undecodable.status_code == 422
    assert undecodable.json()["error"]["message"] == "the scenario is not UTF-8 text"


async def test_json_scenario_body() -> None:
    state = new_state()
    good = {"id": "json-one", "match": {"to": "+14155550101"}, "answer": {"after_ms": 0}}
    bad = {"id": "json-two", "match": {"to": "+14155550101"}, "steps": [{"silence": {"ms": -1}}]}
    async with client_for("telnyx", state) as client:
        created = await client.post("/sim/telnyx/scenarios", json=good)
        rejected = await client.post("/sim/telnyx/scenarios", json=bad)
        unsupported = await client.post(
            "/sim/telnyx/scenarios", content=b"id: x", headers={"content-type": "text/plain"}
        )
    assert created.status_code == 201
    assert "json-one" in state.scenarios
    assert rejected.status_code == 422
    assert rejected.json()["error"]["issues"] == [
        {
            "path": "steps[0].silence.ms",
            "line": None,
            "column": None,
            "message": "Input should be greater than or equal to 0",
        }
    ]
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media_type"


async def test_unknown_scenario_is_404() -> None:
    async with client_for("telnyx", new_state()) as client:
        response = await client.get("/sim/telnyx/scenarios/missing")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "no scenario with id 'missing'"}}


async def test_recordings_are_served_as_wav() -> None:
    state = new_state()
    async with client_for("telnyx", state) as client:
        missing = await client.get("/sim/recordings/rec-1.wav")
        state.store_recording("rec-1", pcm16_wav([b"\x01\x00", b"\x02\x00"], 8000))
        found = await client.get("/sim/recordings/rec-1.wav")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert found.status_code == 200
    assert found.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(found.content), "rb") as reader:
        assert reader.getnchannels() == 2


async def test_llm_config_patch_and_profile() -> None:
    state = new_state()
    async with client_for("llm", state) as client:
        initial = await client.get("/sim/llm/config")
        patched = await client.patch("/sim/llm/config", json={"ttft_ms": 5, "tokens_per_s": 400})
        unknown = await client.patch("/sim/llm/config", json={"tts_ttfa_ms": 1})
        invalid = await client.patch("/sim/llm/config", json={"tokens_per_s": 0})
        profile = await client.post("/sim/llm/config/profile", json={"profile": "realistic"})
        bad_profile = await client.post("/sim/llm/config/profile", json={"profile": "slow"})
    assert initial.json() == {"ttft_ms": 150, "tokens_per_s": 80.0}
    assert patched.json() == {"ttft_ms": 5, "tokens_per_s": 400.0}
    assert unknown.status_code == 422
    assert unknown.json()["error"]["issues"][0]["path"] == "tts_ttfa_ms"
    assert invalid.status_code == 422
    assert profile.json() == {"ttft_ms": 250, "tokens_per_s": 80.0}
    assert state.latencies == PROFILES["realistic"]
    assert bad_profile.status_code == 422


async def test_elevenlabs_config_patch_keeps_other_values() -> None:
    state = new_state()
    async with client_for("elevenlabs", state) as client:
        patched = await client.patch("/sim/elevenlabs/config", json={"tts_ttfa_ms": 250})
        empty = await client.patch(
            "/sim/elevenlabs/config", content=b"", headers={"content-type": "application/json"}
        )
    assert patched.json() == {"tts_ttfa_ms": 250, "stt_final_ms": 60}
    assert state.latencies.ttft_ms == 150
    assert empty.status_code == 422


async def test_routes_are_scoped_to_their_simulator() -> None:
    state = new_state()
    async with client_for("llm", state) as client:
        scenarios = await client.get("/sim/telnyx/scenarios")
        recording = await client.get("/sim/recordings/x.wav")
        other_config = await client.get("/sim/elevenlabs/config")
    async with client_for("stripe", state) as client:
        stripe_config = await client.get("/sim/stripe/config")
    assert [r.status_code for r in (scenarios, recording, other_config, stripe_config)] == [
        404,
        404,
        404,
        404,
    ]
