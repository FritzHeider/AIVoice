"""ElevenLabsRealtimeStt against FakeElevenLabs (specs/30 §3.3 Decision P03-T05a) on real sockets and the
wall clock: transcripts with words, partials, PCM16, session records, entities, faults with reconnect replay
(no audio lost), commit throttling, auth and quota errors, the minimal-config retry, and rotation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from contract_helpers import SimClient, sim_served
from stt_client_helpers import collect_events
from stt_helpers import caller_audio
from tts_helpers import API_KEY

from va_elevenlabs import (
    CatalogSttMetrics,
    ElevenLabsRealtimeStt,
    SttAuthError,
    SttConfig,
    SttConnection,
    SttError,
    SttEvent,
    SttQuotaError,
    SttUnavailableError,
)
from va_elevenlabs.stt_buffer import pieces

type Until = Callable[[list[SttEvent]], bool]

UTTERANCE = "please call me back tomorrow"


def committed_count(n: int) -> Until:
    return lambda items: sum(item.kind == "committed" and not item.is_update for item in items) >= n


def ended(items: list[SttEvent]) -> bool:
    return items[-1].kind == "error"


def commits(items: list[SttEvent]) -> list[str]:
    return [item.text for item in items if item.kind == "committed" and not item.is_update]


class Sim(SimClient):
    def client(self, **conn: Any) -> ElevenLabsRealtimeStt:
        connection = SttConnection(ws_base=self.ws_base, api_key=conn.pop("api_key", API_KEY), **conn)
        return ElevenLabsRealtimeStt(connection, metrics=CatalogSttMetrics(self.service_metrics))

    async def sessions(self) -> list[dict[str, Any]]:
        return await self.records("/sim/elevenlabs/stt/sessions", "sessions")

    async def fault(self, **fault: Any) -> None:
        await self.put("/sim/elevenlabs/stt/fault", fault)

    def audio(self, text: str, audio_format: str = "ulaw_8000", **kwargs: int) -> bytes:
        return caller_audio(self.state, text, audio_format, **kwargs)


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with sim_served() as parts:
        yield Sim(*parts)


async def speak(stt: ElevenLabsRealtimeStt, audio: bytes, frame_bytes: int = 160) -> None:
    """Send ``audio`` in 20 ms frames, then commit."""
    for frame in pieces(audio, frame_bytes):
        await stt.send_audio(frame)
    await stt.commit()


async def test_utterance_is_committed_with_words_and_partials(sim: Sim) -> None:
    async with sim.client() as stt:
        await stt.open(SttConfig(keyterms=["Acme"], previous_text="Hi, this is Karen."))
        await speak(stt, sim.audio(UTTERANCE))
        events = await collect_events(stt, committed_count(1))
    assert any(event.kind == "partial" for event in events)
    final = events[-1]
    assert (final.kind, final.text, final.language_code) == ("committed", UTTERANCE, "en")
    assert [word.text for word in final.words] == UTTERANCE.split()
    assert all(word.end_s > word.start_s for word in final.words)
    [record] = await sim.sessions()
    assert record["keyterms"] == ["Acme"]
    assert record["previous_text"] == "Hi, this is Karen."
    assert record["audio_format"] == "ulaw_8000"
    assert record["commit_strategy"] == "manual"
    assert [t["text"] for t in record["transcripts"]] == [UTTERANCE]
    assert stt.audio_seconds == pytest.approx(record["audio_ms"] / 1000, abs=0.02)
    assert sim.metric("va_stt_final_ms_count") == 1


async def test_pcm_16000(sim: Sim) -> None:
    async with sim.client() as stt:
        await stt.open(SttConfig(audio_format="pcm_16000", sample_rate=16000))
        await speak(stt, sim.audio(UTTERANCE, "pcm_16000"), frame_bytes=640)
        events = await collect_events(stt, committed_count(1))
    assert commits(events) == [UTTERANCE]
    [record] = await sim.sessions()
    assert record["audio_format"] == "pcm_16000"


async def test_entity_detection_updates_the_commit(sim: Sim) -> None:
    text = "my card is 4111 1111 1111 1111"
    async with sim.client() as stt:
        await stt.open(SttConfig(entity_detection=["pii", "pci"]))
        await speak(stt, sim.audio(text))
        events = await collect_events(stt, lambda items: items[-1].is_update)
    update = events[-1]
    assert (update.kind, update.text) == ("committed", text)
    assert [(e.entity_type, e.text) for e in update.entities] == [
        ("credit_card_number", "4111 1111 1111 1111")
    ]
    [record] = await sim.sessions()
    assert record["entity_detection"] == ["pii", "pci"]


async def test_time_limit_mid_utterance_reconnects_without_losing_audio(sim: Sim) -> None:
    # The whole utterance (1.6 s) fits the 2 s replay buffer; the fault fires 600 ms into it.
    text = "call me back soon"
    await sim.fault(kind="session_time_limit_exceeded", after_ms=600)
    async with sim.client() as stt:
        await stt.open(SttConfig(previous_text="Hello from Acme."))
        await speak(stt, sim.audio(text, lead_ms=100, tail_ms=320))
        events = await collect_events(stt, committed_count(1))
    assert [event.error_type for event in events if event.kind == "warning"] == ["stt_reconnect"]
    assert commits(events) == [text]
    first, second = await sim.sessions()
    assert (first["end_reason"], first["transcripts"]) == ("fault", [])
    assert [t["text"] for t in second["transcripts"]] == [text]
    assert second["previous_text"] == "Hello from Acme."
    assert sim.metric("va_vendor_ws_reconnects_total", vendor="elevenlabs_stt") == 1
    errors = {"vendor": "elevenlabs_stt", "code": "session_time_limit_exceeded"}
    assert sim.metric("va_vendor_errors_total", **errors) == 1


async def test_throttled_commit_is_resent(sim: Sim) -> None:
    await sim.fault(kind="commit_throttled")
    async with sim.client() as stt:
        await stt.open(SttConfig())
        await speak(stt, sim.audio(UTTERANCE))
        events = await collect_events(stt, committed_count(1))
    assert commits(events) == [UTTERANCE]
    [record] = await sim.sessions()
    assert record["commits"] == 1


async def test_wrong_key_is_an_auth_error(sim: Sim) -> None:
    (await sim.control.put("/sim/elevenlabs/api-keys", json={"keys": ["someone-else"]})).raise_for_status()
    stt = sim.client()
    with pytest.raises(SttAuthError):
        await stt.open(SttConfig())
    await stt.close()
    assert await sim.sessions() == []
    assert sim.metric("va_vendor_errors_total", vendor="elevenlabs_stt", code="auth_error") == 1


@pytest.mark.parametrize(
    ("fault", "error_class", "error_type", "sessions"),
    [
        ({"kind": "quota_exceeded"}, SttQuotaError, "quota_exceeded", 1),
        ({"kind": "disconnect", "times": 4}, SttUnavailableError, "unavailable", 4),
    ],
    ids=["quota", "repeated-disconnects"],
)
async def test_fatal_faults_end_the_stream(
    sim: Sim, fault: dict[str, Any], error_class: type[SttError], error_type: str, sessions: int
) -> None:
    await sim.fault(**fault)
    async with sim.client() as stt:
        await stt.open(SttConfig())
        await speak(stt, sim.audio(UTTERANCE))
        events = await collect_events(stt, ended)
        assert events[-1].error_type == error_type
        with pytest.raises(error_class):
            await stt.commit()
    assert len(await sim.sessions()) == sessions


async def test_invalid_keyterm_falls_back_to_a_minimal_session(sim: Sim) -> None:
    async with sim.client() as stt:
        await stt.open(SttConfig(keyterms=["x" * 60], entity_detection=["pii"]))
        await speak(stt, sim.audio(UTTERANCE))
        events = await collect_events(stt, committed_count(1))
    assert commits(events) == [UTTERANCE]
    [record] = await sim.sessions()
    assert (record["keyterms"], record["entity_detection"]) == ([], [])


async def test_rotation_between_utterances_loses_nothing(sim: Sim) -> None:
    second_text = "yes that works for me"
    async with sim.client() as stt:
        await stt.open(SttConfig())
        await speak(stt, sim.audio(UTTERANCE))
        await collect_events(stt, committed_count(1))
        stt.add_context("Great, what time?")
        await stt.rotate()
        await speak(stt, sim.audio(second_text))
        events = await collect_events(stt, committed_count(1))
    assert commits(events) == [second_text]
    first, second = await sim.sessions()
    assert [t["text"] for t in first["transcripts"]] == [UTTERANCE]
    assert [t["text"] for t in second["transcripts"]] == [second_text]
    assert second["previous_text"] == f"{UTTERANCE} Great, what time?"
