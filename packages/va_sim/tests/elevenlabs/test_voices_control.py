"""FakeElevenLabs voices control API: registry listing, mutation records, the voices fault, the subscription
(GET/PATCH and live counts), previews, reset, and /sim/state."""

from __future__ import annotations

import pytest
from sim_helpers import new_state
from tts_helpers import rest_client
from voice_helpers import add_voice, wav_sample

from va_sim.elevenlabs.tts_audio import parse_output_format, synthesize
from va_sim.elevenlabs.voice_catalog import KAREN_VOICE_ID

SUBSCRIPTION = "/sim/elevenlabs/subscription"
DAY_S = 86_400


async def test_registry_listing_adds_internal_fields() -> None:
    async with rest_client(new_state()) as client:
        voice_id = (await add_voice(client, remove_background_noise="true")).json()["voice_id"]
        await add_voice(client, "Renamed", files=(), path=f"/v1/voices/{voice_id}/edit")
        voices = (await client.get("/sim/elevenlabs/voices")).json()["voices"]
    assert len(voices) == 12
    karen, clone = voices[0], voices[-1]
    assert (karen["voice_id"], karen["remove_background_noise"], karen["edits"]) == (KAREN_VOICE_ID, False, 0)
    assert (clone["voice_id"], clone["name"], clone["edits"]) == (voice_id, "Renamed", 1)
    assert clone["remove_background_noise"] is False


async def test_requests_log_successful_mutations_only() -> None:
    state = new_state()
    wav = wav_sample(1.0)
    async with rest_client(state) as client:
        await add_voice(client, files=[("short.wav", wav_sample(0.1))])
        voice_id = (
            await add_voice(client, "Clone", files=[("a.wav", wav)], description="d", labels='{"k": "v"}')
        ).json()["voice_id"]
        await state.clock.sleep(1)
        await add_voice(client, "Edited", files=(), path=f"/v1/voices/{voice_id}/edit")
        await client.post(f"/v1/voices/{voice_id}/settings/edit", json={"style": 0.3})
        await client.delete(f"/v1/voices/{voice_id}")
        await client.delete(f"/v1/voices/{KAREN_VOICE_ID}")
        requests = (await client.get("/sim/elevenlabs/voices/requests")).json()["requests"]
    add, edit, settings, delete = requests
    assert add == {
        "id": "voicereq_1",
        "operation": "add",
        "voice_id": voice_id,
        "name": "Clone",
        "description": "d",
        "labels": {"k": "v"},
        "files": [
            {
                "file_name": "a.wav",
                "mime_type": "audio/wav",
                "size_bytes": len(wav),
                "hash": add["files"][0]["hash"],
                "duration_secs": 1.0,
            }
        ],
        "remove_background_noise": False,
        "settings": None,
        "received_ms": add["received_ms"],
    }
    assert (edit["id"], edit["operation"], edit["name"], edit["labels"], edit["files"]) == (
        "voicereq_2",
        "edit",
        "Edited",
        None,
        [],
    )
    assert edit["received_ms"] >= add["received_ms"] + 1000
    assert (settings["operation"], settings["name"], settings["settings"]["style"]) == (
        "settings_edit",
        None,
        0.3,
    )
    assert (delete["operation"], delete["voice_id"], delete["name"], delete["files"]) == (
        "delete",
        voice_id,
        None,
        [],
    )


async def test_fault_put_get_delete() -> None:
    state = new_state()
    async with rest_client(state) as client:
        assert (await client.get("/sim/elevenlabs/voices/fault")).json() == {"fault": None}
        put = await client.put(
            "/sim/elevenlabs/voices/fault", json={"kind": "delay", "delay_ms": 10, "times": 3}
        )
        armed = (await client.get("/sim/elevenlabs/voices/fault")).json()
        bad = await client.put("/sim/elevenlabs/voices/fault", json={"kind": "disconnect"})
        deleted = await client.delete("/sim/elevenlabs/voices/fault")
    assert put.status_code == deleted.status_code == 204
    assert armed == {"fault": {"kind": "delay", "delay_ms": 10, "remaining": 3}}
    assert bad.status_code == 422
    assert state.elevenlabs.voices.fault is None


async def test_subscription_defaults_and_live_counts() -> None:
    state = new_state()
    async with rest_client(state) as client:
        before = (await client.get("/v1/user/subscription")).json()
        await client.post(f"/v1/text-to-speech/{KAREN_VOICE_ID}", json={"text": "Hello there"})
        voice_id = (await add_voice(client)).json()["voice_id"]
        await add_voice(client, "No files", files=(), path=f"/v1/voices/{voice_id}/edit")
        await add_voice(client, "With files", path=f"/v1/voices/{voice_id}/edit")
        after = (await client.get("/v1/user/subscription")).json()
        control = (await client.get(SUBSCRIPTION)).json()
    now_unix = int(state.clock.now().timestamp())
    assert before == {
        "tier": "creator",
        "character_count": 0,
        "character_limit": 100_000,
        "can_extend_character_limit": False,
        "allowed_to_extend_character_limit": False,
        "next_character_count_reset_unix": before["next_character_count_reset_unix"],
        "voice_slots_used": 0,
        "professional_voice_slots_used": 0,
        "voice_limit": 30,
        "max_voice_add_edits": 95,
        "voice_add_edit_counter": 0,
        "professional_voice_limit": 0,
        "can_extend_voice_limit": False,
        "can_use_instant_voice_cloning": True,
        "can_use_professional_voice_cloning": False,
        "currency": "usd",
        "status": "active",
        "billing_period": "monthly_period",
        "character_refresh_period": "monthly_period",
    }
    assert after["next_character_count_reset_unix"] == now_unix + 30 * DAY_S
    assert (after["character_count"], after["voice_slots_used"], after["voice_add_edit_counter"]) == (
        11,
        1,
        2,
    )
    assert control == after


async def test_subscription_patch() -> None:
    async with rest_client(new_state()) as client:
        patched = await client.patch(
            SUBSCRIPTION, json={"tier": "pro", "character_limit": 500, "max_voice_add_edits": 0}
        )
        public = (await client.get("/v1/user/subscription")).json()
    assert patched.status_code == 200
    assert (patched.json()["tier"], patched.json()["character_limit"], patched.json()["voice_limit"]) == (
        "pro",
        500,
        30,
    )
    assert public == patched.json()


@pytest.mark.parametrize(
    "body", [{"voice_limit": -1}, {"tier": ""}, {"seats": 3}, {"character_limit": "many"}]
)
async def test_subscription_patch_is_validated(body: dict[str, object]) -> None:
    async with rest_client(new_state()) as client:
        response = await client.patch(SUBSCRIPTION, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_preview_is_the_tts_audio_of_a_greeting() -> None:
    async with rest_client(new_state(), key=None) as client:
        voice = (await client.get(f"/v1/voices/{KAREN_VOICE_ID}", headers={"xi-api-key": "k"})).json()
        preview = await client.get(voice["preview_url"])
        missing = await client.get("/sim/elevenlabs/previews/nobody.mp3")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "audio/mpeg"
    assert preview.content == synthesize("Hi, this is Karen.", parse_output_format("mp3_44100_128"))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_reset_reseeds_voices_and_clears_records_fault_and_limits() -> None:
    state = new_state()
    async with rest_client(state) as client:
        voice_id = (await add_voice(client)).json()["voice_id"]
        await client.post(f"/v1/voices/{KAREN_VOICE_ID}/settings/edit", json={"speed": 0.8})
        await client.patch(SUBSCRIPTION, json={"voice_limit": 2})
        await client.put("/sim/elevenlabs/voices/fault", json={"kind": "error"})
        assert state.snapshot()["elevenlabs"]["voices"] == 12  # type: ignore[index, call-overload]
        assert (await client.post("/sim/reset")).status_code == 204
        gone = await client.get(f"/v1/voices/{voice_id}")
        karen = (await client.get(f"/v1/voices/{KAREN_VOICE_ID}/settings")).json()
        subscription = (await client.get(SUBSCRIPTION)).json()
    voices = state.elevenlabs.voices
    assert gone.status_code == 404
    assert karen["speed"] == 1.0
    assert (subscription["voice_limit"], subscription["voice_add_edit_counter"]) == (30, 0)
    assert (len(voices), voices.requests, voices.fault) == (11, [], None)
