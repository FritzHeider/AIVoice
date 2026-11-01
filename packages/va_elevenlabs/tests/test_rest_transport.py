"""ElevenLabsRest request pipeline (respx): authentication header, per-operation retry schedules and
timeouts, ``Retry-After``, final error mapping, metrics outcomes and durations, spans, and the specs/27 §3.6
rows."""

from __future__ import annotations

import random

import httpx
import orjson
import pytest
import respx
from clock_helpers import RecordingClock
from obs_helpers import vendor_metric_rows
from opentelemetry.trace import StatusCode
from rest_client_helpers import BASE_URL, KEY, RestRig

from va_common.metrics import LAT_MS, new_registry
from va_domain.voices import VoiceSettings
from va_elevenlabs import (
    AudioUpload,
    BatchOptions,
    ElevenLabsApiError,
    ElevenLabsProtocolError,
    ElevenLabsRateLimitedError,
    ElevenLabsRest,
    ElevenLabsRestMetrics,
    ElevenLabsTransportError,
    VoiceFile,
)
from va_elevenlabs.rest_metrics import REST_DURATION_METRIC, REST_REQUESTS_METRIC
from va_elevenlabs.rest_retry import RestRetry
from va_elevenlabs.rest_transport import ElevenLabsTransport
from va_elevenlabs.voices_rest import GET_VOICE

VOICE = "/v1/voices/v1"
TTS = "/v1/text-to-speech/v1"
VOICE_BODY = {"voice_id": "v1", "name": "Karen"}


def detail(status: str) -> dict[str, object]:
    return {"detail": {"status": status, "message": status.replace("_", " ")}}


async def test_requests_carry_the_key_header_and_never_show_it(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.get(VOICE).respond(200, json=VOICE_BODY)
    voice = await rig.client.get_voice("v1")
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == KEY
    assert request.headers["accept"] == "application/json"
    assert voice.name == "Karen"
    assert KEY not in repr(rig.client)
    assert repr(rig.client) == f"ElevenLabsRest(base_url={BASE_URL!r})"


async def test_operation_timeouts(api: respx.MockRouter, rig: RestRig) -> None:
    get = api.get(VOICE).respond(200, json=VOICE_BODY)
    add = api.post("/v1/voices/add").respond(200, json={"voice_id": "v2"})
    batch = api.post("/v1/speech-to-text").respond(200, json={"text": "", "words": []})
    await rig.client.get_voice("v1")
    await rig.client.add_voice("n", [VoiceFile("a.wav", b"RIFF")])
    await rig.client.batch_transcribe(AudioUpload("a.wav", b"RIFF"), BatchOptions("scribe_v2"))
    timeouts = [route.calls.last.request.extensions["timeout"] for route in (get, add, batch)]
    assert [t["read"] for t in timeouts] == [10.0, 60.0, 300.0]
    assert {t["connect"] for t in timeouts} == {5.0}


async def test_reads_retry_429_and_5xx_with_jitter(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(VOICE).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200, json=VOICE_BODY),
        ]
    )
    await rig.client.get_voice("v1")
    assert len(rig.clock.sleeps) == 3
    for slept, base in zip(rig.clock.sleeps, (0.5, 1.0, 2.0), strict=True):
        assert base <= slept <= base + 0.1
    assert rig.count("get_voice", "ok") == 1
    [span] = rig.spans("elevenlabs.get_voice")
    assert span.attributes["elevenlabs.attempts"] == 4
    assert span.attributes["http.status_code"] == 200
    assert span.attributes["elevenlabs.voice_id"] == "v1"


async def test_reads_give_up_after_three_retries(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(VOICE).respond(429, json=detail("too_many_concurrent_requests"))
    with pytest.raises(ElevenLabsRateLimitedError) as caught:
        await rig.client.get_voice("v1")
    assert caught.value.code == "too_many_concurrent_requests"
    assert len(rig.clock.sleeps) == 3
    assert rig.count("get_voice", "rate_limited") == 1
    [span] = rig.spans("elevenlabs.get_voice")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["elevenlabs.attempts"] == 4


async def test_retry_after_lengthens_a_wait_and_a_long_one_stops(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(VOICE).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "3"}),
            httpx.Response(503, headers={"Retry-After": "31"}),
        ]
    )
    with pytest.raises(ElevenLabsApiError) as caught:
        await rig.client.get_voice("v1")
    assert caught.value.status == 503
    assert rig.clock.sleeps == [3.0]
    assert rig.count("get_voice", "server_error") == 1


async def test_client_errors_are_not_retried(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.post(f"{VOICE}/settings/edit").respond(
        422, json={"detail": [{"loc": ["body"], "msg": "bad"}]}
    )
    with pytest.raises(ElevenLabsApiError) as caught:
        await rig.client.update_voice_settings("v1", VoiceSettings())
    assert caught.value.code == "validation_error"
    assert route.call_count == 1
    assert rig.clock.sleeps == []
    assert rig.count("update_voice_settings", "client_error") == 1


async def test_transport_failures_are_retried_then_raised(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.delete(VOICE).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ElevenLabsTransportError) as caught:
        await rig.client.delete_voice("v1")
    assert isinstance(caught.value.cause, httpx.ReadTimeout)
    assert route.call_count == 4
    assert rig.count("delete_voice", "transport_error") == 1


async def test_add_and_edit_are_never_retried(api: respx.MockRouter, rig: RestRig) -> None:
    add = api.post("/v1/voices/add").mock(side_effect=httpx.ConnectError("refused"))
    edit = api.post(f"{VOICE}/edit").respond(503)
    with pytest.raises(ElevenLabsTransportError):
        await rig.client.add_voice("n", [VoiceFile("a.wav", b"RIFF")])
    with pytest.raises(ElevenLabsApiError):
        await rig.client.edit_voice("v1", "n")
    assert (add.call_count, edit.call_count) == (1, 1)
    assert rig.clock.sleeps == []


async def test_tts_retries_once_after_200_ms(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.post(TTS).respond(500)
    with pytest.raises(ElevenLabsApiError):
        await rig.client.tts_convert("v1", "Hi", model_id="m", output_format="ulaw_8000")
    assert route.call_count == 2
    [slept] = rig.clock.sleeps
    assert 0.2 <= slept <= 0.3
    assert route.calls.last.request.headers["accept"] == "audio/*"


async def test_tts_body_and_headers(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.post(TTS).respond(
        200,
        content=b"\xff" * 8,
        headers={"content-type": "audio/basic", "x-character-count": "2", "request-id": "r1"},
    )
    audio = await rig.client.tts_convert(
        "v1", "Hi", model_id="m", output_format="ulaw_8000", voice_settings=VoiceSettings(speed=1.2)
    )
    request = route.calls.last.request
    assert request.url.params["output_format"] == "ulaw_8000"
    assert orjson.loads(request.content) == {
        "text": "Hi",
        "model_id": "m",
        "apply_text_normalization": "auto",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.2,
        },
    }
    assert (audio.audio, audio.content_type, audio.character_count, audio.request_id) == (
        b"\xff" * 8,
        "audio/basic",
        2,
        "r1",
    )


async def test_tts_without_optional_headers(api: respx.MockRouter, rig: RestRig) -> None:
    api.post(TTS).respond(200, content=b"\x00", headers={"x-character-count": "n/a"})
    audio = await rig.client.tts_convert("v1", "Hi", model_id="m", output_format="pcm_16000")
    assert (audio.character_count, audio.request_id) == (None, None)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"[]", "the body is not a JSON object"),
        (b"not json", "the body is not a JSON object"),
        (orjson.dumps({"alignment": None}), "audio_base64 is missing"),
        (orjson.dumps({"audio_base64": "***"}), "audio_base64 is not base64"),
        (
            orjson.dumps(
                {
                    "audio_base64": "AA==",
                    "alignment": {
                        "characters": ["a", "b"],
                        "character_start_times_seconds": [0.0],
                        "character_end_times_seconds": [0.1, 0.2],
                    },
                }
            ),
            "alignment: Value error, alignment arrays must have equal lengths",
        ),
    ],
)
async def test_malformed_timestamps_bodies(
    api: respx.MockRouter, rig: RestRig, body: bytes, reason: str
) -> None:
    api.post(f"{TTS}/with-timestamps").respond(200, content=body)
    with pytest.raises(ElevenLabsProtocolError) as caught:
        await rig.client.tts_with_timestamps("v1", "ab", model_id="m", output_format="ulaw_8000")
    assert caught.value.reason == reason
    assert rig.count("tts_with_timestamps", "client_error") == 1


async def test_timestamps_without_alignment(api: respx.MockRouter, rig: RestRig) -> None:
    api.post(f"{TTS}/with-timestamps").respond(200, json={"audio_base64": "AAE="})
    result = await rig.client.tts_with_timestamps("v1", "ab", model_id="m", output_format="ulaw_8000")
    assert (result.audio, result.alignment, result.normalized_alignment) == (b"\x00\x01", None, None)


async def test_list_voices_pages_count_as_one_operation(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.get("/v2/voices").mock(
        side_effect=[
            httpx.Response(200, json={"voices": [VOICE_BODY], "has_more": True, "next_page_token": "Mg"}),
            httpx.Response(500),
            httpx.Response(200, json={"voices": [{"voice_id": "v2", "name": "B"}], "has_more": False}),
        ]
    )
    voices = await rig.client.list_voices(category="premade", search="k", voice_type="default", page_size=1)
    assert [voice.voice_id for voice in voices] == ["v1", "v2"]
    first, _, last = (call.request.url.params for call in route.calls)
    assert dict(first) == {
        "page_size": "1",
        "include_total_count": "false",
        "category": "premade",
        "search": "k",
        "voice_type": "default",
    }
    assert ("next_page_token", "Mg") in last.multi_items()
    assert rig.count("list_voices", "ok") == 1
    [span] = rig.spans("elevenlabs.list_voices")
    assert span.attributes["elevenlabs.attempts"] == 3


async def test_list_voices_protocol_errors(api: respx.MockRouter, rig: RestRig) -> None:
    api.get("/v2/voices").respond(200, json={"voices": [], "has_more": True})
    with pytest.raises(ElevenLabsProtocolError, match="has_more without next_page_token"):
        await rig.client.list_voices()
    api.get("/v2/voices").respond(200, json={"voices": [{"name": "no id"}]})
    with pytest.raises(ElevenLabsProtocolError, match=r"voices\.0\.voice_id: Field required"):
        await rig.client.list_voices()


async def test_list_voices_stops_after_the_page_cap(
    api: respx.MockRouter, rig: RestRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("va_elevenlabs.voices_rest.MAX_VOICE_PAGES", 2)
    api.get("/v2/voices").respond(200, json={"voices": [], "has_more": True, "next_page_token": "x"})
    with pytest.raises(ElevenLabsProtocolError, match="more than 2 pages"):
        await rig.client.list_voices()


async def test_list_voices_rejects_a_bad_page_size(rig: RestRig) -> None:
    for size in (0, 101):
        with pytest.raises(ValueError, match="page_size"):
            await rig.client.list_voices(page_size=size)


async def test_vendor_voice_and_settings_parsing_is_lenient(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(VOICE).respond(
        200,
        json={
            **VOICE_BODY,
            "settings": {"stability": 0.1, "speed": None, "brand_new_knob": 3},
            "samples": [{"sample_id": "s1", "unknown": True}],
            "something_new": [1],
        },
    )
    api.get(f"{VOICE}/settings").respond(200, json={"similarity_boost": 0.9, "style": None})
    voice = await rig.client.get_voice("v1")
    settings = await rig.client.get_voice_settings("v1")
    assert voice.settings == VoiceSettings(stability=0.1)
    assert voice.samples is not None
    assert voice.samples[0].sample_id == "s1"
    assert settings == VoiceSettings(similarity_boost=0.9)


async def test_settings_out_of_range_is_a_protocol_error(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(f"{VOICE}/settings").respond(200, json={"speed": 3.0})
    with pytest.raises(ElevenLabsProtocolError, match="speed"):
        await rig.client.get_voice_settings("v1")


async def test_ids_are_path_encoded_and_must_not_be_empty(api: respx.MockRouter, rig: RestRig) -> None:
    route = api.get("/v1/voices/a%2Fb").respond(200, json={"voice_id": "a/b", "name": "x"})
    await rig.client.get_voice("a/b")
    assert route.called
    with pytest.raises(ValueError, match="must not be empty"):
        await rig.client.get_voice("")


async def test_without_metrics_nothing_is_recorded(api: respx.MockRouter) -> None:
    api.get(VOICE).respond(200, json=VOICE_BODY)
    async with httpx.AsyncClient() as http:
        client = ElevenLabsRest(KEY, f"{BASE_URL}/", http, clock=RecordingClock())
        voice = await client.get_voice("v1")
    assert voice.voice_id == "v1"


async def test_duration_includes_retry_waits(api: respx.MockRouter, rig: RestRig) -> None:
    api.get(VOICE).mock(side_effect=[httpx.Response(500), httpx.Response(200, json=VOICE_BODY)])
    await rig.client.get_voice("v1")
    total = rig.registry.get_sample_value(f"{REST_DURATION_METRIC}_sum", {"operation": "get_voice"})
    assert total == pytest.approx(rig.clock.sleeps[0] * 1000)


def test_retry_schedule_edges() -> None:
    schedule = RestRetry((1.0,), jitter_s=0.0)
    rng = random.Random(1)  # noqa: S311 - jitter is zero here
    assert schedule.wait(0, rng) == 1.0
    assert schedule.wait(0, rng, retry_after_s=0.5) == 1.0
    assert schedule.wait(0, rng, retry_after_s=30.0) == 30.0
    assert schedule.wait(0, rng, retry_after_s=30.5) is None
    assert schedule.wait(1, rng) is None


def test_metrics_match_spec_section_3_6() -> None:
    assert vendor_metric_rows("va_elevenlabs_") == {
        (REST_REQUESTS_METRIC, "counter", ("operation", "outcome")),
        (REST_DURATION_METRIC, "histogram", ("operation",)),
    }
    registry = new_registry(process_collectors=False)
    ElevenLabsRestMetrics(registry).observe("get_voice", "ok", 12.0)
    bounds = [
        sample.labels["le"]
        for metric in registry.collect()
        if metric.name == REST_DURATION_METRIC
        for sample in metric.samples
        if sample.name.endswith("_bucket")
    ]
    assert bounds[:-1] == [str(float(b)) for b in LAT_MS]


async def test_transport_repr_hides_the_key() -> None:
    async with httpx.AsyncClient() as http:
        transport = ElevenLabsTransport(
            api_key=KEY,
            base_url=f"{BASE_URL}/",
            http=http,
            clock=RecordingClock(),
            rng=random.Random(0),  # noqa: S311 - jitter in a test
            metrics=None,
            tracer_provider=None,
        )
    assert repr(transport) == f"ElevenLabsTransport(base_url={BASE_URL!r})"


async def test_voice_settings_of_the_wrong_type_are_a_protocol_error(
    api: respx.MockRouter, rig: RestRig
) -> None:
    api.get(VOICE).respond(200, json={**VOICE_BODY, "settings": "loud"})
    with pytest.raises(ElevenLabsProtocolError, match="settings"):
        await rig.client.get_voice("v1")


async def test_a_malformed_batch_entity_is_a_failed_operation(api: respx.MockRouter, rig: RestRig) -> None:
    entity = {"text": "a", "entity_type": "email_address", "start_char": None, "end_char": 1}
    api.post("/v1/speech-to-text").respond(200, json={"text": "a", "words": [], "entities": [entity]})
    with pytest.raises(ElevenLabsProtocolError, match=r"entities\[\]\.start_char is not an integer"):
        await rig.client.batch_transcribe(AudioUpload("a.wav", b"RIFF"), BatchOptions("scribe_v2"))
    assert rig.count("batch_transcribe", "client_error") == 1
    assert rig.count("batch_transcribe", "ok") == 0
    [span] = rig.spans("elevenlabs.batch_transcribe")
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("slip", [TypeError, ValueError, KeyError, IndexError])
async def test_a_parser_slip_is_reported_as_a_protocol_error(
    api: respx.MockRouter, slip: type[Exception]
) -> None:
    api.get(VOICE).respond(200, json=VOICE_BODY)
    registry = new_registry(process_collectors=False)
    metrics = ElevenLabsRestMetrics(registry)

    def parse(response: object) -> None:
        raise slip("x")

    async with httpx.AsyncClient() as http:
        transport = ElevenLabsTransport(
            api_key=KEY,
            base_url=BASE_URL,
            http=http,
            clock=RecordingClock(),
            rng=random.Random(0),  # noqa: S311 - jitter in a test
            metrics=metrics,
            tracer_provider=None,
        )
        with pytest.raises(ElevenLabsProtocolError) as caught:
            await transport.call(GET_VOICE, VOICE, parse)
    assert caught.value.reason == slip.__name__
    assert isinstance(caught.value.__cause__, slip)
    labels = {"operation": "get_voice", "outcome": "client_error"}
    assert registry.get_sample_value(REST_REQUESTS_METRIC, labels) == 1.0
