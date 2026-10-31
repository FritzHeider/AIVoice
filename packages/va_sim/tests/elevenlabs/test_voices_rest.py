"""FakeElevenLabs voices REST: the seeded registry, /v2/voices paging and filters, voice reads, settings,
add/edit/delete with sample checks, the voice limit, auth, and the voices fault."""

from __future__ import annotations

from typing import Any

import pytest
from sim_helpers import new_state
from tts_helpers import rest_client
from voice_helpers import ADD, V2, add_voice, mp3_sample, voice_files, wav_sample

from va_sim.elevenlabs.voice_catalog import DEFAULT_VOICE_SETTINGS, KAREN_VOICE_ID, PREMADE_VOICES
from va_sim.elevenlabs.voice_params import encode_page_token

LABEL_KEYS = {"accent", "description", "age", "gender", "use_case"}
KAREN = f"/v1/voices/{KAREN_VOICE_ID}"


def names(body: dict[str, Any]) -> list[str]:
    return [voice["name"] for voice in body["voices"]]


async def test_the_seed_has_karen_and_ten_labelled_voices() -> None:
    async with rest_client(new_state()) as client:
        body = (await client.get("/v1/voices")).json()
    voices = body["voices"]
    assert len(voices) == 11
    assert voices[0]["voice_id"] == KAREN_VOICE_ID
    assert voices[0]["name"] == "Karen"
    assert voices[0]["labels"] == {
        "accent": "american",
        "description": "warm",
        "age": "young",
        "gender": "female",
        "use_case": "conversational",
    }
    assert all(set(voice["labels"]) == LABEL_KEYS for voice in voices)
    assert all(len(voice["voice_id"]) == 20 and voice["voice_id"].isalnum() for voice in voices)
    multilingual = [voice["name"] for voice in voices if len(voice["verified_languages"]) > 1]
    assert multilingual == ["Aria", "Charlotte"]


async def test_premade_voice_shape() -> None:
    async with rest_client(new_state()) as client:
        voice = (await client.get(KAREN, params={"with_settings": "true"})).json()
    assert voice == {
        "voice_id": KAREN_VOICE_ID,
        "name": "Karen",
        "samples": None,
        "category": "premade",
        "labels": voice["labels"],
        "description": PREMADE_VOICES[0].description,
        "preview_url": f"http://elevenlabs/sim/elevenlabs/previews/{KAREN_VOICE_ID}.mp3",
        "available_for_tiers": [],
        "settings": dict(DEFAULT_VOICE_SETTINGS),
        "sharing": None,
        "high_quality_base_model_ids": ["eleven_flash_v2_5", "eleven_multilingual_v2"],
        "verified_languages": [
            {
                "language": "en",
                "model_id": "eleven_flash_v2_5",
                "accent": "american",
                "locale": "en-US",
                "preview_url": None,
            }
        ],
        "is_owner": False,
        "is_legacy": False,
        "is_mixed": False,
        "created_at_unix": None,
    }


async def test_v2_paging_follows_tokens_until_has_more_is_false() -> None:
    seen: list[str] = []
    async with rest_client(new_state()) as client:
        params: dict[str, str | int] = {"page_size": 4}
        while True:
            body = (await client.get(V2, params=params)).json()
            assert body["total_count"] == 11
            seen.extend(voice["voice_id"] for voice in body["voices"])
            if not body["has_more"]:
                assert body["next_page_token"] is None
                break
            params = {"page_size": 4, "next_page_token": body["next_page_token"]}
    assert len(seen) == len(set(seen)) == 11
    assert seen == [voice.voice_id for voice in reversed(PREMADE_VOICES)]


async def test_v2_defaults_newest_first_ten_per_page() -> None:
    async with rest_client(new_state()) as client:
        body = (await client.get(V2)).json()
    assert len(body["voices"]) == 10
    assert body["has_more"] is True
    assert body["next_page_token"] == encode_page_token(10)
    assert body["voices"][-1]["name"] == "Aria"


async def test_v2_sort_by_name_and_direction() -> None:
    async with rest_client(new_state()) as client:
        ascending = (await client.get(V2, params={"sort": "name", "page_size": 100})).json()
        descending = (
            await client.get(V2, params={"sort": "name", "sort_direction": "desc", "page_size": 100})
        ).json()
        oldest = (await client.get(V2, params={"sort_direction": "asc", "page_size": 2})).json()
    expected = sorted(voice.name for voice in PREMADE_VOICES)
    assert names(ascending) == expected
    assert names(descending) == expected[::-1]
    assert names(oldest) == ["Karen", "Aria"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"search": "BRITISH"}, ["George"]),
        ({"search": "karen"}, ["Karen"]),
        ({"search": "storyteller"}, ["George"]),
        ({"search": "premade"}, [voice.name for voice in reversed(PREMADE_VOICES)]),
        ({"category": "cloned"}, []),
        ({"voice_type": "default", "page_size": 2}, ["Charlotte", "Liam"]),
        ({"voice_type": "community"}, []),
        ({"voice_type": "personal"}, []),
        ({"voice_ids": [KAREN_VOICE_ID, PREMADE_VOICES[6].voice_id]}, ["George", "Karen"]),
        ({"fine_tuning_state": "x", "voice_ids": KAREN_VOICE_ID}, ["Karen"]),
    ],
)
async def test_v2_filters(params: dict[str, Any], expected: list[str]) -> None:
    async with rest_client(new_state()) as client:
        body = (await client.get(V2, params={"page_size": 100, **params})).json()
    if "page_size" not in params:
        assert names(body) == expected
        assert body["total_count"] == len(expected)
    else:
        assert names(body)[: len(expected)] == expected


async def test_v2_filters_see_clones() -> None:
    async with rest_client(new_state()) as client:
        added = (await add_voice(client, "Sales clone", labels='{"team": "sales"}')).json()
        personal = (await client.get(V2, params={"voice_type": "personal"})).json()
        by_label = (await client.get(V2, params={"search": "SALES"})).json()
        default = (await client.get(V2, params={"voice_type": "default", "page_size": 100})).json()
    assert [voice["voice_id"] for voice in personal["voices"]] == [added["voice_id"]]
    assert names(by_label) == ["Sales clone"]
    assert "Sales clone" not in names(default)


async def test_v2_without_total_count() -> None:
    async with rest_client(new_state()) as client:
        body = (await client.get(V2, params={"include_total_count": "FALSE"})).json()
    assert body["total_count"] is None


async def test_v2_token_past_the_end_is_an_empty_last_page() -> None:
    async with rest_client(new_state()) as client:
        body = (await client.get(V2, params={"next_page_token": encode_page_token(50)})).json()
    assert body == {"voices": [], "has_more": False, "total_count": 11, "next_page_token": None}


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"page_size": 0}, "page_size"),
        ({"page_size": 101}, "page_size"),
        ({"next_page_token": "!!!"}, "next_page_token"),
        ({"next_page_token": "YWJj"}, "next_page_token"),  # "abc"
        ({"next_page_token": "LTE"}, "next_page_token"),  # "-1"
        ({"next_page_token": "_w"}, "next_page_token"),  # not ASCII
        ({"sort": "age"}, "sort"),
        ({"sort_direction": "up"}, "sort_direction"),
        ({"voice_type": "mine"}, "voice_type"),
        ({"category": "famous"}, "category"),
        ({"include_total_count": "maybe"}, "include_total_count"),
        ({"voice_ids": [str(index) for index in range(101)]}, "voice_ids"),
    ],
)
async def test_v2_invalid_query_is_422(params: dict[str, Any], field: str) -> None:
    async with rest_client(new_state()) as client:
        response = await client.get(V2, params=params)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["query", field]


async def test_unknown_voice_is_404() -> None:
    async with rest_client(new_state()) as client:
        responses = [
            await client.get("/v1/voices/ghost"),
            await client.get("/v1/voices/ghost/settings"),
            await client.post("/v1/voices/ghost/settings/edit", json={"stability": 0.2}),
            await client.delete("/v1/voices/ghost"),
            await add_voice(client, path="/v1/voices/ghost/edit"),
        ]
    for response in responses:
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "status": "voice_not_found",
                "message": "A voice with the voice_id ghost was not found.",
            }
        }


async def test_settings_read_and_merge() -> None:
    state = new_state()
    async with rest_client(state) as client:
        assert (await client.get(f"{KAREN}/settings")).json() == dict(DEFAULT_VOICE_SETTINGS)
        edited = await client.post(
            f"{KAREN}/settings/edit", json={"stability": 0.2, "speed": 1.1, "style": None, "other": 1}
        )
        assert edited.json() == {"status": "ok"}
        settings = (await client.get(f"{KAREN}/settings")).json()
        voice = (await client.get(KAREN)).json()
    assert settings == {**DEFAULT_VOICE_SETTINGS, "stability": 0.2, "speed": 1.1}
    assert voice["settings"] == settings
    (record,) = state.elevenlabs.voices.requests
    assert (record.operation, record.settings) == ("settings_edit", settings)


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"stability": 1.5}, ["body", "stability"]),
        ({"similarity_boost": -0.1}, ["body", "similarity_boost"]),
        ({"speed": 0.5}, ["body", "speed"]),
        ({"use_speaker_boost": "yes"}, ["body", "use_speaker_boost"]),
        ([1, 2], ["body"]),
    ],
)
async def test_settings_edit_validation(body: object, loc: list[str]) -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(f"{KAREN}/settings/edit", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == loc


async def test_settings_edit_invalid_json_is_422() -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(f"{KAREN}/settings/edit", content=b"{nope")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body"]


async def test_add_rejects_a_half_second_wav() -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await add_voice(client, files=[("short.wav", wav_sample(0.5))])
    assert response.status_code == 400
    assert response.json() == {
        "detail": {"status": "invalid_sample", "message": "Sample 'short.wav' is shorter than 1 second"}
    }
    assert len(state.elevenlabs.voices) == 11
    assert state.elevenlabs.voices.requests == []


async def test_add_creates_a_cloned_voice() -> None:
    state = new_state()
    wav, mp3 = wav_sample(1.25), mp3_sample(2.0)
    async with rest_client(state) as client:
        response = await add_voice(
            client,
            "Front desk",
            files=[("desk.wav", wav), ("desk.mp3", mp3)],
            description="Our receptionist",
            labels='{"accent": "irish"}',
            remove_background_noise="True",
        )
        voice_id = response.json()["voice_id"]
        voice = (await client.get(f"/v1/voices/{voice_id}")).json()
        newest = (await client.get(V2, params={"page_size": 1})).json()
    assert response.json() == {"voice_id": voice_id, "requires_verification": False}
    assert len(voice_id) == 20
    assert voice_id.isalnum()
    assert newest["voices"][0]["voice_id"] == voice_id
    assert newest["total_count"] == 12
    assert voice["category"] == "cloned"
    assert (voice["name"], voice["description"], voice["labels"]) == (
        "Front desk",
        "Our receptionist",
        {"accent": "irish"},
    )
    assert (voice["preview_url"], voice["is_owner"], voice["high_quality_base_model_ids"]) == (None, True, [])
    assert voice["created_at_unix"] == int(state.clock.now().timestamp())
    assert voice["verified_languages"] == []
    first, second = voice["samples"]
    assert set(first) == {"sample_id", "file_name", "mime_type", "size_bytes", "hash", "duration_secs"}
    assert (first["file_name"], first["mime_type"], first["size_bytes"], first["duration_secs"]) == (
        "desk.wav",
        "audio/wav",
        len(wav),
        1.25,
    )
    assert (second["mime_type"], second["duration_secs"]) == ("audio/mpeg", 2.011)
    assert len(first["hash"]) == 64
    assert len(first["sample_id"]) == 20
    assert first["sample_id"] != second["sample_id"]
    assert state.elevenlabs.voices.get(voice_id).remove_background_noise is True  # type: ignore[union-attr]


async def test_add_accepts_unmeasured_containers() -> None:
    async with rest_client(new_state()) as client:
        response = await add_voice(client, files=[("a.ogg", b"OggS" + bytes(64))])
        voice = (await client.get(f"/v1/voices/{response.json()['voice_id']}")).json()
    assert voice["samples"][0]["duration_secs"] is None
    assert voice["samples"][0]["mime_type"] == "audio/ogg"


async def test_add_rejects_unreadable_samples() -> None:
    async with rest_client(new_state()) as client:
        response = await add_voice(client, files=[("ok.wav", wav_sample(2.0)), ("x.txt", b"text")])
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "status": "invalid_sample",
        "message": "Sample 'x.txt' could not be read as audio",
    }


@pytest.mark.parametrize(
    ("fields", "loc"),
    [
        ({"labels": "{not json"}, ["body", "labels"]),
        ({"labels": '["a"]'}, ["body", "labels"]),
        ({"labels": '{"a": 1}'}, ["body", "labels"]),
        ({"remove_background_noise": "sometimes"}, ["body", "remove_background_noise"]),
        ({"description": "d" * 501}, ["body", "description"]),
    ],
)
async def test_add_field_validation(fields: dict[str, Any], loc: list[str]) -> None:
    async with rest_client(new_state()) as client:
        response = await add_voice(client, **fields)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == loc


@pytest.mark.parametrize("name", [None, "", "n" * 257])
async def test_add_needs_a_name(name: str | None) -> None:
    async with rest_client(new_state()) as client:
        response = await add_voice(client, name)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]


async def test_add_needs_one_to_25_files() -> None:
    async with rest_client(new_state()) as client:
        missing = await add_voice(client, files=())
        too_many = await add_voice(client, files=[(f"{index}.wav", wav_sample(1.0)) for index in range(26)])
        text_file = await client.post(ADD, files=[("name", (None, "x")), ("files", (None, "not an upload"))])
        over_parser_limit = await client.post(
            ADD, files=[("name", (None, "x")), *voice_files(*[("f.wav", b"x")] * 27)]
        )
    assert missing.json()["detail"] == [
        {"loc": ["body", "files"], "msg": "Field required", "type": "missing"}
    ]
    assert too_many.json()["detail"][0]["loc"] == ["body", "files"]
    assert text_file.json()["detail"][0]["loc"] == ["body", "files"]
    assert over_parser_limit.json()["detail"][0]["loc"] == ["body"]
    for response in (missing, too_many, text_file, over_parser_limit):
        assert response.status_code == 422


async def test_add_needs_multipart() -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(ADD, json={"name": "x"})
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"loc": ["body"], "msg": "the body must be multipart/form-data", "type": "value_error"}
    ]


async def test_voice_limit_is_enforced() -> None:
    async with rest_client(new_state()) as client:
        await client.patch("/sim/elevenlabs/subscription", json={"voice_limit": 1})
        first = await add_voice(client)
        second = await add_voice(client)
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json() == {
        "detail": {
            "status": "voice_limit_reached",
            "message": "You have reached your maximum amount of custom voices (1 / 1).",
        }
    }


async def test_edit_replaces_fields_and_samples() -> None:
    state = new_state()
    async with rest_client(state) as client:
        voice_id = (await add_voice(client, labels='{"a": "b"}', description="old")).json()["voice_id"]
        path = f"/v1/voices/{voice_id}/edit"
        keep = await add_voice(client, "Renamed", files=(), path=path)
        kept = (await client.get(f"/v1/voices/{voice_id}")).json()
        replace = await add_voice(
            client, "Again", files=[("new.mp3", mp3_sample(1.5))], path=path, labels='{"c": "d"}'
        )
        replaced = (await client.get(f"/v1/voices/{voice_id}")).json()
        short = await add_voice(client, "Short", files=[("s.wav", wav_sample(0.2))], path=path)
    assert keep.json() == replace.json() == {"status": "ok"}
    assert (kept["name"], kept["labels"], kept["description"]) == ("Renamed", {"a": "b"}, None)
    assert [sample["file_name"] for sample in kept["samples"]] == ["sample.wav"]
    assert (replaced["name"], replaced["labels"]) == ("Again", {"c": "d"})
    assert [sample["file_name"] for sample in replaced["samples"]] == ["new.mp3"]
    assert short.status_code == 400
    assert short.json()["detail"]["status"] == "invalid_sample"
    assert state.elevenlabs.voices.get(voice_id).edits == 2  # type: ignore[union-attr]


async def test_premade_voices_cannot_be_edited_or_deleted() -> None:
    expected = {
        "detail": {"status": "voice_not_editable", "message": "Premade voices cannot be edited or deleted"}
    }
    async with rest_client(new_state()) as client:
        edit = await add_voice(client, path=f"{KAREN}/edit")
        delete = await client.delete(KAREN)
        still = await client.get(KAREN)
    assert (edit.status_code, edit.json()) == (400, expected)
    assert (delete.status_code, delete.json()) == (400, expected)
    assert still.status_code == 200


async def test_edit_validation_comes_before_the_lookup() -> None:
    async with rest_client(new_state()) as client:
        response = await add_voice(client, None, path="/v1/voices/ghost/edit")
    assert response.status_code == 422


async def test_delete_removes_a_clone() -> None:
    async with rest_client(new_state()) as client:
        voice_id = (await add_voice(client)).json()["voice_id"]
        deleted = await client.delete(f"/v1/voices/{voice_id}")
        again = await client.delete(f"/v1/voices/{voice_id}")
        gone = await client.get(f"/v1/voices/{voice_id}")
    assert deleted.json() == {"status": "ok"}
    assert again.status_code == gone.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", V2),
        ("GET", "/v1/voices"),
        ("GET", KAREN),
        ("GET", f"{KAREN}/settings"),
        ("POST", f"{KAREN}/settings/edit"),
        ("POST", ADD),
        ("POST", f"{KAREN}/edit"),
        ("DELETE", KAREN),
        ("GET", "/v1/user/subscription"),
    ],
)
async def test_every_route_needs_a_key(method: str, path: str) -> None:
    state = new_state()
    state.elevenlabs.api_keys = ("good",)
    async with rest_client(state, key=None) as client:
        missing = await client.request(method, path)
        rejected = await client.request(method, path, headers={"xi-api-key": "bad"})
    assert missing.status_code == rejected.status_code == 401
    assert missing.json()["detail"]["status"] == "needs_authorization"
    assert rejected.json()["detail"]["status"] == "invalid_api_key"


async def test_fault_error_and_rate_limit_are_consumed_in_order() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.put("/sim/elevenlabs/voices/fault", json={"kind": "error"})
        failed = await client.get(V2)
        await client.put("/sim/elevenlabs/voices/fault", json={"kind": "rate_limited", "times": 2})
        limited = [await client.get("/v1/user/subscription"), await add_voice(client)]
        recovered = await client.get(KAREN)
    assert (failed.status_code, failed.json()["detail"]) == (
        500,
        {"status": "internal_error", "message": "Simulated voices failure"},
    )
    for response in limited:
        assert (response.status_code, response.json()["detail"]) == (
            429,
            {"status": "too_many_concurrent_requests", "message": "Simulated rate limit"},
        )
    assert recovered.status_code == 200
    assert len(state.elevenlabs.voices) == 11


async def test_fault_delay_waits_on_the_sim_clock_and_does_not_touch_tts() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.put("/sim/elevenlabs/voices/fault", json={"kind": "delay", "delay_ms": 5000})
        tts = await client.post(f"/v1/text-to-speech/{KAREN_VOICE_ID}", json={"text": "hi"})
        assert state.elevenlabs.voices.fault is not None
        before = state.clock.virtual_ms()
        delayed = await client.get(KAREN)
    assert tts.status_code == 200
    assert delayed.status_code == 200
    assert state.clock.virtual_ms() - before >= 5000
    assert state.elevenlabs.voices.fault is None


async def test_faults_apply_after_auth() -> None:
    state = new_state()
    async with rest_client(state, key=None) as client:
        await client.put("/sim/elevenlabs/voices/fault", json={"kind": "error"})
        response = await client.get(V2)
    assert response.status_code == 401
    assert state.elevenlabs.voices.fault is not None
