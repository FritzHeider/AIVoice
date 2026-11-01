"""Live smoke tests of the phase-03 vendor clients against real Telnyx, ElevenLabs, and Telnyx Inference
(specs/30 §7 Decision P03-T16). Skipped unless ``LIVE_TESTS=1``; each test also skips when its vendor key is
empty. Run: ``LIVE_TESTS=1 LIVE_TELNYX_API_KEY=... LIVE_ELEVENLABS_API_KEY=... LIVE_LLM_API_KEY=...
uv run pytest tests_live -m live -rs``."""

from __future__ import annotations

import asyncio
import math
import os
import re
import uuid
from typing import Final, Literal

import orjson
import pytest
from live_clients import BATCH_STT_MODEL, ELEVENLABS_WS_BASE, embeddings_dimensions
from live_env import LiveEnv, live_enabled
from live_fixture import SttFixture, ensure_stt_fixture
from live_wer import wer
from pydantic import BaseModel

from va_elevenlabs import (
    AudioUpload,
    BatchOptions,
    ElevenLabsRealtimeStt,
    ElevenLabsRest,
    SttConfig,
    SttConnection,
    SttEvent,
    TtsAudio,
    TtsConfig,
    TtsSocket,
    TtsVoice,
)
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL
from va_llm import ChatMessage, DialogueRequest, Embeddings, LlmGateway, ToolDef
from va_telnyx import NumberSearch, TelnyxClient
from va_telnyx.bootstrap import RESOURCE_PREFIX, platform_resource_name

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not live_enabled(os.environ), reason="LIVE_TESTS=1 is not set"),
]

MAX_WER: Final = 0.10
STEP_TIMEOUT_S: Final = 30.0
FRAME_BYTES: Final = 160  # 20 ms of μ-law at 8 kHz
FRAME_S: Final = 0.02
TRAILING_SILENCE_FRAMES: Final = 25
ULAW_SILENCE: Final = b"\xff"
TTS_PHRASE: Final = "Hello, this is a short live test of streaming speech."
LONG_PARAGRAPH: Final = " ".join(
    [
        "This paragraph is long on purpose so that the speech service is still generating audio",
        "when the client closes the context in the middle of the stream.",
    ]
    * 6
)
E164: Final = re.compile(r"^\+[1-9]\d{6,14}$")


class Sentiment(BaseModel):
    """Record the overall sentiment of the user's message."""

    sentiment: Literal["positive", "negative", "neutral"]


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


async def _premade_voice(rest: ElevenLabsRest) -> str:
    voices = await rest.list_voices()
    assert voices, "the account lists no voices"
    premade = [voice.voice_id for voice in voices if voice.category == "premade"]
    return premade[0] if premade else voices[0].voice_id


async def _fixture(rest: ElevenLabsRest) -> SttFixture:
    return await ensure_stt_fixture(rest, voice_id=await _premade_voice(rest), model_id=DEFAULT_TTS_MODEL)


async def test_telnyx_account_read_live(telnyx: TelnyxClient, live_env: LiveEnv) -> None:
    balance = await telnyx.get_balance()
    assert len(balance.currency) == 3
    apps = await telnyx.list_call_control_apps(RESOURCE_PREFIX)
    assert all(RESOURCE_PREFIX in app.application_name for app in apps)
    profiles = await telnyx.list_outbound_voice_profiles(RESOURCE_PREFIX)
    assert all(RESOURCE_PREFIX in profile.name for profile in profiles)
    messaging_name = platform_resource_name(os.environ.get("VA_ENV", "staging"))
    messaging = await telnyx.list_messaging_profiles(messaging_name)
    assert all(profile.name == messaging_name for profile in messaging)
    if live_env.from_number:
        number = await telnyx.get_phone_number_by_e164(live_env.from_number)
        assert number is not None
        assert number.phone_number == live_env.from_number


async def test_number_search_live(telnyx: TelnyxClient) -> None:
    found = await telnyx.search_numbers(NumberSearch(country_code="US", features=("voice",), limit=5))
    assert 1 <= len(found) <= 5
    assert all(E164.match(number.phone_number) for number in found)


async def test_elevenlabs_voices_live(eleven_rest: ElevenLabsRest) -> None:
    voices = await eleven_rest.list_voices()
    assert voices
    assert all(voice.voice_id and voice.name for voice in voices)
    voice = await eleven_rest.get_voice(await _premade_voice(eleven_rest))
    assert voice.voice_id


async def _until_final(socket: TtsSocket, context_id: str) -> list[TtsAudio]:
    """Every item of ``context_id`` up to and including its ``is_final``."""
    items: list[TtsAudio] = []
    async with asyncio.timeout(STEP_TIMEOUT_S):
        async for item in socket.audio():
            if item.context_id != context_id:
                continue
            items.append(item)
            if item.is_final:
                return items
    pytest.fail(f"the TTS socket ended before context {context_id!r} finished")


async def _first_audio(socket: TtsSocket, context_id: str) -> TtsAudio:
    async with asyncio.timeout(STEP_TIMEOUT_S):
        async for item in socket.audio():
            if item.context_id == context_id and item.audio:
                return item
            assert not (item.context_id == context_id and item.is_final), "the context ended without audio"
    pytest.fail(f"the TTS socket ended before context {context_id!r} produced audio")


async def test_elevenlabs_tts_ws_live(elevenlabs_key: str, eleven_rest: ElevenLabsRest) -> None:
    voice = TtsVoice(provider_voice_id=await _premade_voice(eleven_rest))
    async with TtsSocket(TtsConfig(ELEVENLABS_WS_BASE, elevenlabs_key)) as socket:
        await socket.open("ulaw_8000", voice)
        await socket.start_context("a")
        await socket.send("a", TTS_PHRASE)
        await socket.flush("a")
        spoken = await _until_final(socket, "a")
        audio = b"".join(item.audio for item in spoken)
        assert len(audio) >= FRAME_BYTES
        chars = "".join(
            char for item in spoken if item.alignment is not None for char in item.alignment.chars
        )
        assert chars.split() == TTS_PHRASE.split()

        await socket.start_context("b")
        await socket.send("b", LONG_PARAGRAPH)
        await socket.flush("b")
        await _first_audio(socket, "b")
        await socket.cancel("b")
        after_cancel = await _until_final(socket, "b")
        assert [item for item in after_cancel if item.audio] == []
        assert after_cancel[-1].is_final


async def _committed(stt: ElevenLabsRealtimeStt) -> SttEvent:
    async with asyncio.timeout(STEP_TIMEOUT_S):
        async for event in stt.events():
            assert event.kind != "error", f"stt error {event.error_type}"
            if event.kind == "committed" and not event.is_update and event.text.strip():
                return event
    pytest.fail("the STT session ended without a committed transcript")


async def test_elevenlabs_stt_ws_live(elevenlabs_key: str, eleven_rest: ElevenLabsRest) -> None:
    fixture = await _fixture(eleven_rest)
    audio = fixture.ulaw + ULAW_SILENCE * (FRAME_BYTES * TRAILING_SILENCE_FRAMES)
    async with ElevenLabsRealtimeStt(SttConnection(ELEVENLABS_WS_BASE, elevenlabs_key)) as stt:
        await stt.open(SttConfig(language_code="en", include_language_detection=False))
        for start in range(0, len(audio), FRAME_BYTES):
            await stt.send_audio(audio[start : start + FRAME_BYTES])
            # The vendor expects real-time audio: pace the frames like a call does.
            await asyncio.sleep(FRAME_S)
        await stt.commit()
        event = await _committed(stt)
    assert wer(fixture.text, event.text) <= MAX_WER, event.text


async def test_elevenlabs_batch_stt_live(eleven_rest: ElevenLabsRest) -> None:
    fixture = await _fixture(eleven_rest)
    transcript = await eleven_rest.batch_transcribe(
        AudioUpload("stt_phrase.wav", fixture.wav), BatchOptions(model_id=BATCH_STT_MODEL, language_code="en")
    )
    assert transcript.words
    assert wer(fixture.text, transcript.text) <= MAX_WER, transcript.text


async def test_telnyx_inference_live(gateway: LlmGateway, embeddings: Embeddings) -> None:
    org_id = uuid.uuid4()
    request = DialogueRequest(
        org_id=org_id, messages=(_user("Reply with the single word: ready"),), temperature=0
    )
    async with await gateway.stream_dialogue(request) as stream:
        text = "".join([delta async for delta in stream.text()])
    assert text.strip()
    assert stream.ttft_ms is not None
    assert stream.ttft_ms > 0
    assert not stream.fell_back

    weather = ToolDef(
        name="get_weather",
        description="Look up the current weather for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    ask = _user("What is the weather in Paris right now? Use the get_weather tool.")
    tool_request = DialogueRequest(org_id=org_id, messages=(ask,), tools=(weather,), temperature=0)
    async with await gateway.stream_dialogue(tool_request) as stream:
        calls = await stream.tool_calls()
    assert [call.name for call in calls] == ["get_weather"]
    assert "paris" in str(orjson.loads(calls[0].arguments_json)["city"]).lower()

    result = await gateway.call_structured(
        "analysis",
        Sentiment,
        org_id=org_id,
        messages=[_user("I love this product, it works perfectly and support was wonderful!")],
        function_name="record_sentiment",
        description="Record the overall sentiment of the user's message.",
    )
    assert result.sentiment == "positive"

    vectors = await embeddings.embed(["hello world", "voice agents on the phone"], org_id=org_id)
    assert [len(vector) for vector in vectors] == [embeddings_dimensions()] * 2
    for vector in vectors:
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-3)
