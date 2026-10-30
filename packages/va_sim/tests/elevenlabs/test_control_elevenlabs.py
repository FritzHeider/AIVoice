"""FakeElevenLabs control API: TTS request records, fault arming, the API-key allow list, reset, scoping."""

from __future__ import annotations

from sim_helpers import new_state
from telnyx_helpers import telnyx_client
from tts_helpers import rest_client

CONVERT = "/v1/text-to-speech/v/with-timestamps"


async def test_requests_lists_records() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.post(CONVERT, json={"text": "hi"})
        response = await client.get("/sim/elevenlabs/tts/requests")
    (record,) = response.json()["requests"]
    assert record["id"] == "ttsreq_1"
    assert record["transport"] == "rest"
    assert record["voice_id"] == "v"


async def test_fault_put_get_delete() -> None:
    state = new_state()
    async with rest_client(state) as client:
        assert (await client.get("/sim/elevenlabs/tts/fault")).json() == {"fault": None}
        put = await client.put(
            "/sim/elevenlabs/tts/fault", json={"kind": "delay", "delay_ms": 40, "times": 2}
        )
        assert put.status_code == 204
        assert (await client.get("/sim/elevenlabs/tts/fault")).json() == {
            "fault": {"kind": "delay", "after_chunks": 0, "delay_ms": 40, "remaining": 2}
        }
        assert (await client.delete("/sim/elevenlabs/tts/fault")).status_code == 204
        assert state.elevenlabs.fault is None


async def test_fault_body_is_validated() -> None:
    async with rest_client(new_state()) as client:
        bad_kind = await client.put("/sim/elevenlabs/tts/fault", json={"kind": "explode"})
        bad_times = await client.put("/sim/elevenlabs/tts/fault", json={"kind": "error", "times": 0})
        extra = await client.put("/sim/elevenlabs/tts/fault", json={"kind": "error", "chunks": 1})
    for response in (bad_kind, bad_times, extra):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


async def test_api_keys_allow_list() -> None:
    state = new_state()
    async with rest_client(state, key="k2") as client:
        assert (await client.get("/sim/elevenlabs/api-keys")).json() == {"keys": []}
        put = await client.put("/sim/elevenlabs/api-keys", json={"keys": ["k1", "k1", "k2"]})
        assert put.status_code == 204
        assert (await client.get("/sim/elevenlabs/api-keys")).json() == {"keys": ["k1", "k2"]}
        assert (await client.post(CONVERT, json={"text": "ok"})).status_code == 200
        await client.put("/sim/elevenlabs/api-keys", json={"keys": ["k1"]})
        assert (await client.post(CONVERT, json={"text": "no"})).status_code == 401
        blank = await client.put("/sim/elevenlabs/api-keys", json={"keys": [""]})
        assert blank.status_code == 422


async def test_reset_clears_records_keys_and_fault() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.post(CONVERT, json={"text": "hi"})
        await client.put("/sim/elevenlabs/api-keys", json={"keys": ["sim-elevenlabs-key"]})
        await client.put("/sim/elevenlabs/tts/fault", json={"kind": "error"})
        assert state.snapshot()["elevenlabs"] == {"tts_requests": 1, "tts_sockets": 0}
        assert (await client.post("/sim/reset")).status_code == 204
    assert state.elevenlabs.tts_requests == []
    assert state.elevenlabs.api_keys == ()
    assert state.elevenlabs.fault is None


async def test_elevenlabs_routes_are_absent_on_the_telnyx_port() -> None:
    async with telnyx_client(new_state()) as client:
        assert (await client.get("/sim/elevenlabs/tts/requests")).status_code == 404
        assert (await client.post("/v1/text-to-speech/v", json={"text": "x"})).status_code == 404
