"""Multipart encoding of voice add/edit and batch transcription, decoded with the stdlib ``email`` parser."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from rest_client_helpers import Part, RestRig, form_parts

from va_elevenlabs import AudioUpload, BatchOptions, SourceUrl, VoiceFile
from va_elevenlabs.stt_batch import MAX_BATCH_KEYTERMS

TRANSCRIPT = {"language_code": "en", "text": "", "words": []}


@pytest.fixture(autouse=True)
def _vendor_routes(api: respx.MockRouter) -> None:
    api.post("/v1/voices/add").respond(200, json={"voice_id": "new", "requires_verification": True})
    api.post("/v1/voices/v1/edit").respond(200, json={"status": "ok"})
    api.post("/v1/speech-to-text").respond(200, json=TRANSCRIPT)


def fields(parts: list[Part]) -> list[tuple[str, str]]:
    return [(part.name, part.text) for part in parts if part.filename is None]


def uploads(parts: list[Part]) -> list[tuple[str, str | None, str | None, bytes]]:
    return [(p.name, p.filename, p.content_type, p.data) for p in parts if p.filename is not None]


async def test_add_voice_fields_then_one_part_per_file(api: respx.MockRouter, rig: RestRig) -> None:
    added = await rig.client.add_voice(
        "acme:Support",
        [VoiceFile("a.wav", b"RIFF-a", "audio/wav"), VoiceFile("b.mp3", b"ID3-b", "audio/mpeg")],
        description="Calm",
        labels={"accent": "british", "age": "young"},
        remove_background_noise=True,
    )
    parts = form_parts(api.calls.last.request)
    assert (added.voice_id, added.requires_verification) == ("new", True)
    assert [part.name for part in parts] == [
        "name",
        "description",
        "labels",
        "remove_background_noise",
        "files",
        "files",
    ]
    assert fields(parts) == [
        ("name", "acme:Support"),
        ("description", "Calm"),
        ("labels", '{"accent":"british","age":"young"}'),
        ("remove_background_noise", "true"),
    ]
    assert uploads(parts) == [
        ("files", "a.wav", "audio/wav", b"RIFF-a"),
        ("files", "b.mp3", "audio/mpeg", b"ID3-b"),
    ]


async def test_add_voice_needs_a_file(rig: RestRig) -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        await rig.client.add_voice("x", [])


async def test_edit_voice_without_files_sends_only_fields(api: respx.MockRouter, rig: RestRig) -> None:
    await rig.client.edit_voice("v1", "Renamed")
    parts = form_parts(api.calls.last.request)
    assert fields(parts) == [("name", "Renamed"), ("remove_background_noise", "false")]
    assert uploads(parts) == []


async def test_voice_file_from_path_guesses_the_type(tmp_path: Path) -> None:
    path = tmp_path / "sample.wav"
    path.write_bytes(b"RIFF....WAVE")
    file = await VoiceFile.from_path(path)
    unknown = tmp_path / "sample.unknownext"
    unknown.write_bytes(b"x")
    assert (file.filename, file.content_type, file.data) == ("sample.wav", "audio/x-wav", b"RIFF....WAVE")
    assert (await VoiceFile.from_path(unknown)).content_type == "application/octet-stream"
    assert (await VoiceFile.from_path(unknown, content_type="audio/ogg")).content_type == "audio/ogg"
    assert "RIFF" not in repr(file)


async def test_batch_stereo_recording_fields(api: respx.MockRouter, rig: RestRig) -> None:
    options = BatchOptions.for_recording(
        "scribe_v2", language_code="es", keyterms=("Acme", "order number"), zero_retention=True
    )
    await rig.client.batch_transcribe(AudioUpload("raw.wav", b"RIFF-call"), options)
    request = api.calls.last.request
    parts = form_parts(request)
    assert request.url.params["enable_logging"] == "false"
    assert fields(parts) == [
        ("model_id", "scribe_v2"),
        ("use_multi_channel", "true"),
        ("timestamps_granularity", "word"),
        ("tag_audio_events", "false"),
        ("diarize", "false"),
        ("multichannel_output_style", "combined"),
        ("language_code", "es"),
        ("keyterms", "Acme"),
        ("keyterms", "order number"),
    ]
    assert uploads(parts) == [("file", "raw.wav", "audio/wav", b"RIFF-call")]


async def test_batch_redaction_asks_for_separate_channels(api: respx.MockRouter, rig: RestRig) -> None:
    options = BatchOptions.for_recording("scribe_v2", redact=True)
    await rig.client.batch_transcribe(SourceUrl("https://bucket.invalid/raw.wav?sig=1"), options)
    request = api.calls.last.request
    parts = form_parts(request)
    assert "enable_logging" not in request.url.params
    assert ("multichannel_output_style", "separate") in fields(parts)
    assert [value for name, value in fields(parts) if name == "entity_detection"] == ["pii", "pci"]
    assert fields(parts)[-1] == ("cloud_storage_url", "https://bucket.invalid/raw.wav?sig=1")
    assert uploads(parts) == []


async def test_batch_mono_recording_diarizes_two_speakers(api: respx.MockRouter, rig: RestRig) -> None:
    options = BatchOptions.for_recording("scribe_v2", stereo=False)
    await rig.client.batch_transcribe(AudioUpload("mono.wav", b"RIFF"), options)
    assert fields(form_parts(api.calls.last.request)) == [
        ("model_id", "scribe_v2"),
        ("use_multi_channel", "false"),
        ("timestamps_granularity", "word"),
        ("tag_audio_events", "false"),
        ("diarize", "true"),
        ("num_speakers", "2"),
        ("detect_speaker_roles", "true"),
    ]


def test_keyterms_are_capped() -> None:
    BatchOptions("scribe_v2", keyterms=tuple(f"k{i}" for i in range(MAX_BATCH_KEYTERMS)))
    with pytest.raises(ValueError, match="at most 1000 keyterms"):
        BatchOptions("scribe_v2", keyterms=tuple(f"k{i}" for i in range(MAX_BATCH_KEYTERMS + 1)))
