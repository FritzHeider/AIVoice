"""ElevenLabsRest against FakeElevenLabs: REST synthesis, the voice library and cloning, batch transcription,
vendor errors and their platform mapping, and retried faults."""

from __future__ import annotations

import pytest
from batch_helpers import caller_track, place, wav_file
from rest_client_helpers import SimRest, sim_rest
from tts_helpers import VOICE_ID
from voice_helpers import wav_sample

from va_domain.voices import VoiceSettings
from va_elevenlabs import (
    AudioUpload,
    BatchOptions,
    ElevenLabsApiError,
    ElevenLabsAuthError,
    ElevenLabsNotFoundError,
    VoiceFile,
    group_utterances,
)
from va_sim.audio.codec import ulaw_decode
from va_sim.elevenlabs.tts_audio import CHAR_MS
from va_sim.elevenlabs.voice_catalog import KAREN_VOICE_ID

MODEL = "eleven_flash_v2_5"
PREMADE_COUNT = 11


def sample(name: str, seconds: float = 3.0) -> VoiceFile:
    return VoiceFile(name, wav_sample(seconds), "audio/wav")


async def test_tts_convert_returns_the_audio_and_billed_characters() -> None:
    text = "Hello there"
    async with sim_rest() as sim:
        audio = await sim.client.tts_convert(VOICE_ID, text, model_id=MODEL, output_format="ulaw_8000")
        [record] = await sim.records("/sim/elevenlabs/tts/requests", "requests")
    assert len(audio.audio) == len(text) * CHAR_MS * 8
    assert audio.content_type == "audio/basic"
    assert audio.character_count == len(text)
    assert audio.request_id == record["id"]
    assert (record["transport"], record["model_id"], record["output_format"]) == ("rest", MODEL, "ulaw_8000")
    assert sim.count("tts_convert", "ok") == 1


async def test_tts_with_timestamps_aligns_every_character() -> None:
    text = "Sure, one moment."
    settings = VoiceSettings(stability=0.3, speed=1.1)
    async with sim_rest() as sim:
        result = await sim.client.tts_with_timestamps(
            VOICE_ID,
            text,
            model_id=MODEL,
            output_format="ulaw_8000",
            voice_settings=settings,
            language_code="en",
        )
    assert result.alignment is not None
    assert result.normalized_alignment == result.alignment
    assert "".join(result.alignment.characters) == text
    assert result.alignment.character_end_times_seconds[-1] == pytest.approx(len(text) * CHAR_MS / 1000)
    assert len(result.audio) == len(text) * CHAR_MS * 8
    assert result.character_count == len(text)


async def test_unknown_voice_is_not_found_and_maps_to_voice_not_found() -> None:
    async with sim_rest() as sim:
        with pytest.raises(ElevenLabsNotFoundError) as caught:
            await sim.client.tts_convert("nope", "Hi", model_id=MODEL, output_format="ulaw_8000")
    assert caught.value.code == "voice_not_found"
    assert caught.value.to_app_error().code == "VOICE_NOT_FOUND"
    assert sim.count("tts_convert", "client_error") == 1


async def test_a_tts_error_fault_is_retried_once() -> None:
    async with sim_rest() as sim:
        await sim.send("PUT", "/sim/elevenlabs/tts/fault", {"kind": "error"})
        audio = await sim.client.tts_convert(VOICE_ID, "Hi", model_id=MODEL, output_format="ulaw_8000")
    assert audio.audio
    assert len(sim.clock.sleeps) == 1
    assert 0.2 <= sim.clock.sleeps[0] <= 0.3
    [span] = sim.spans("elevenlabs.tts_convert")
    assert span.attributes["elevenlabs.attempts"] == 2


async def test_wrong_key_is_an_auth_error() -> None:
    async with sim_rest(key="wrong-key") as sim:
        await sim.send("PUT", "/sim/elevenlabs/api-keys", {"keys": ["sim-elevenlabs-key"]})
        with pytest.raises(ElevenLabsAuthError) as caught:
            await sim.client.get_voice(KAREN_VOICE_ID)
    assert caught.value.code == "invalid_api_key"
    assert "wrong-key" not in str(caught.value)
    assert caught.value.to_app_error().code == "INTERNAL_ERROR"
    assert sim.count("get_voice", "auth_error") == 1


async def test_list_voices_walks_every_page_and_filters() -> None:
    async with sim_rest() as sim:
        premade = await sim.client.list_voices(category="premade", page_size=3)
        karen = await sim.client.list_voices(search="karen")
    assert len(premade) == PREMADE_COUNT
    assert len({voice.voice_id for voice in premade}) == PREMADE_COUNT
    assert all(voice.category == "premade" for voice in premade)
    assert KAREN_VOICE_ID in {voice.voice_id for voice in karen}
    first = premade[0]
    assert first.settings is not None
    assert first.verified_languages
    assert first.preview_url is not None
    assert sim.count("list_voices", "ok") == 2
    paged, searched = sim.spans("elevenlabs.list_voices")
    # One span per call; attempts count every page request (11 premade voices in pages of 3).
    assert (paged.attributes["elevenlabs.attempts"], searched.attributes["elevenlabs.attempts"]) == (4, 1)


async def clone(sim: SimRest, *files: VoiceFile) -> str:
    added = await sim.client.add_voice(
        "acme:Support",
        list(files),
        description="Calm support voice",
        labels={"accent": "american", "gender": "female"},
        remove_background_noise=True,
    )
    assert added.requires_verification is False
    return added.voice_id


async def test_clone_lifecycle_add_edit_settings_delete() -> None:
    async with sim_rest() as sim:
        voice_id = await clone(sim, sample("a.wav"), sample("b.wav", 2.0))
        created = await sim.client.get_voice(voice_id)
        await sim.client.edit_voice(
            voice_id, "acme:Support 2", [sample("c.wav", 4.0)], description="Calm support voice"
        )
        edited = await sim.client.get_voice(voice_id)
        await sim.client.update_voice_settings(voice_id, VoiceSettings(stability=0.2, speed=0.9))
        settings = await sim.client.get_voice_settings(voice_id)
        deleted = await sim.client.delete_voice(voice_id)
        again = await sim.client.delete_voice(voice_id)
        requests = await sim.records("/sim/elevenlabs/voices/requests", "requests")
    assert created.category == "cloned"
    assert created.labels == {"accent": "american", "gender": "female"}
    assert created.description == "Calm support voice"
    assert created.samples is not None
    assert [(s.file_name, s.mime_type, s.duration_secs) for s in created.samples] == [
        ("a.wav", "audio/wav", 3.0),
        ("b.wav", "audio/wav", 2.0),
    ]
    assert edited.name == "acme:Support 2"
    assert edited.samples is not None
    assert [s.file_name for s in edited.samples] == ["c.wav"]
    assert settings == VoiceSettings(stability=0.2, speed=0.9)
    assert (deleted, again) == (True, False)
    assert [r["operation"] for r in requests] == ["add", "edit", "settings_edit", "delete"]
    assert requests[0]["remove_background_noise"] is True
    assert sim.count("delete_voice", "client_error") == 1


async def test_delete_missing_voice_raises_when_not_tolerated() -> None:
    async with sim_rest() as sim:
        with pytest.raises(ElevenLabsNotFoundError):
            await sim.client.delete_voice("missing", missing_ok=False)


async def test_premade_voices_cannot_be_deleted() -> None:
    async with sim_rest() as sim:
        with pytest.raises(ElevenLabsApiError) as caught:
            await sim.client.delete_voice(KAREN_VOICE_ID)
    assert (caught.value.status, caught.value.code) == (400, "voice_not_editable")


async def test_short_sample_and_voice_limit_are_clone_failures() -> None:
    async with sim_rest() as sim:
        with pytest.raises(ElevenLabsApiError) as short:
            await sim.client.add_voice("x", [sample("short.wav", 0.5)])
        await sim.send("PATCH", "/sim/elevenlabs/subscription", {"voice_limit": 0})
        with pytest.raises(ElevenLabsApiError) as limited:
            await sim.client.add_voice("x", [sample("ok.wav")])
    assert short.value.code == "invalid_sample"
    assert limited.value.code == "voice_limit_reached"
    for error in (short.value, limited.value):
        app_error = error.to_app_error()
        assert app_error.code == "VOICE_CLONE_FAILED"
        assert app_error.details["provider_message"] == error.provider_message
    assert sim.clock.sleeps == []
    assert sim.count("add_voice", "client_error") == 2


async def test_add_voice_is_never_retried_on_a_server_error() -> None:
    async with sim_rest() as sim:
        await sim.send("PUT", "/sim/elevenlabs/voices/fault", {"kind": "error"})
        with pytest.raises(ElevenLabsApiError) as caught:
            await sim.client.add_voice("x", [sample("ok.wav")])
    assert caught.value.status == 500
    assert caught.value.to_app_error().code == "UPSTREAM_TIMEOUT"
    assert sim.clock.sleeps == []
    assert sim.count("add_voice", "server_error") == 1


async def test_reads_retry_a_rate_limit() -> None:
    async with sim_rest() as sim:
        await sim.send("PUT", "/sim/elevenlabs/voices/fault", {"kind": "rate_limited", "times": 2})
        voice = await sim.client.get_voice(KAREN_VOICE_ID)
    assert voice.voice_id == KAREN_VOICE_ID
    assert len(sim.clock.sleeps) == 2
    assert 0.5 <= sim.clock.sleeps[0] <= 0.6
    assert 1.0 <= sim.clock.sleeps[1] <= 1.1


CALLER = "I need to book a table"
AGENT = "Sure, for how many people?"


async def recording(sim: SimRest) -> bytes:
    """A dual-channel WAV: the caller on channel 0 at 0.5 s, the agent's TTS on channel 1 at 3 s."""
    spoken = await sim.client.tts_convert(VOICE_ID, AGENT, model_id=MODEL, output_format="ulaw_8000")
    caller = place([(500, caller_track(sim.state, CALLER))], 6000)
    agent = place([(3000, ulaw_decode(spoken.audio))], 6000)
    return wav_file([caller, agent])


async def test_batch_transcribes_a_stereo_recording_into_utterances() -> None:
    options = BatchOptions.for_recording(
        "scribe_v2", language_code="en", keyterms=("table",), zero_retention=True
    )
    async with sim_rest() as sim:
        wav = await recording(sim)
        transcript = await sim.client.batch_transcribe(AudioUpload("raw.wav", wav), options)
        [record] = await sim.records("/sim/elevenlabs/stt/batch", "requests")
    utterances = group_utterances(transcript.words)
    assert [(u.role, u.text) for u in utterances] == [("user", CALLER), ("assistant", AGENT)]
    assert utterances[0].start < utterances[1].start
    assert transcript.text == f"{CALLER} {AGENT}"
    assert transcript.language_code == "en"
    assert transcript.audio_duration_secs == pytest.approx(6.0)
    assert transcript.transcription_id is not None
    assert record["use_multi_channel"] is True
    assert record["multichannel_output_style"] == "combined"
    assert record["keyterms"] == ["table"]
    assert record["enable_logging"] is False
    assert record["channels"] == 2


async def test_batch_separate_with_redaction_merges_both_channels() -> None:
    options = BatchOptions.for_recording("scribe_v2", redact=True)
    async with sim_rest() as sim:
        wav = await recording(sim)
        transcript = await sim.client.batch_transcribe(AudioUpload("raw.wav", wav), options)
        [record] = await sim.records("/sim/elevenlabs/stt/batch", "requests")
    assert record["multichannel_output_style"] == "separate"
    assert record["entity_detection"] == ["pii", "pci"]
    assert record["enable_logging"] is True
    assert [(u.role, u.text) for u in group_utterances(transcript.words)] == [
        ("user", CALLER),
        ("assistant", AGENT),
    ]
    assert transcript.text == f"{CALLER} {AGENT}"


async def test_batch_rate_limit_is_retried_after_five_seconds() -> None:
    async with sim_rest() as sim:
        wav = await recording(sim)
        await sim.send("PUT", "/sim/elevenlabs/stt/batch/fault", {"kind": "rate_limited"})
        transcript = await sim.client.batch_transcribe(
            AudioUpload("raw.wav", wav), BatchOptions.for_recording("scribe_v2")
        )
    assert transcript.words
    assert len(sim.clock.sleeps) == 1
    assert 5.0 <= sim.clock.sleeps[0] <= 5.1
    assert sim.count("batch_transcribe", "ok") == 1
