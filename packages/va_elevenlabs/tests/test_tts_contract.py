"""TtsSocket against FakeElevenLabs (specs/30 §3.3 Decisions P03-T04, P03-T05c rule 9) on real sockets and the
wall clock: audio and alignment, records, cancellation, reconnection mid-utterance, faults, voice and key
errors."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from tts_client_helpers import collect
from tts_helpers import API_KEY, VOICE_ID, realtime_state, served

from va_common.metrics import Service, build_metrics, new_registry
from va_elevenlabs import (
    CatalogTtsMetrics,
    TtsAudio,
    TtsAuthError,
    TtsConfig,
    TtsSocket,
    TtsUnavailableError,
    TtsVoice,
    TtsVoiceNotFoundError,
)
from va_elevenlabs.tts_models import OutputFormat
from va_sim.state import SimState

type Until = Callable[[list[TtsAudio]], bool]


def final_of(context_id: str) -> Until:
    return lambda items: any(item.is_final and item.context_id == context_id for item in items)


def spoken(items: list[TtsAudio]) -> str:
    """The visible characters the alignments spell."""
    return "".join("".join(item.alignment.chars) for item in items if item.alignment is not None).replace(
        " ", ""
    )


class Sim:
    def __init__(self, state: SimState, ws_base: str, control: httpx.AsyncClient) -> None:
        self.state = state
        self.ws_base = ws_base
        self.control = control
        self.registry = new_registry(process_collectors=False)
        self.metrics = CatalogTtsMetrics(build_metrics(Service.MEDIA_WORKER, self.registry))

    def socket(self, **config: Any) -> TtsSocket:
        tts_config = TtsConfig(ws_base=self.ws_base, api_key=config.pop("api_key", API_KEY), **config)
        return TtsSocket(tts_config, metrics=self.metrics)

    async def requests(self) -> list[dict[str, Any]]:
        response = await self.control.get("/sim/elevenlabs/tts/requests")
        response.raise_for_status()
        records: list[dict[str, Any]] = response.json()["requests"]
        return records

    async def fault(self, **fault: Any) -> None:
        (await self.control.put("/sim/elevenlabs/tts/fault", json=fault)).raise_for_status()

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    state = realtime_state()
    async with served(state) as ws_base:
        http_base = ws_base.replace("ws://", "http://", 1)
        async with httpx.AsyncClient(base_url=http_base, timeout=5.0) as control:
            yield Sim(state, ws_base, control)


@asynccontextmanager
async def speaking(
    tts: TtsSocket, fmt: OutputFormat = "ulaw_8000", voice_id: str = VOICE_ID
) -> AsyncIterator[TtsSocket]:
    async with tts:
        await tts.open(fmt, TtsVoice(provider_voice_id=voice_id))
        yield tts


async def say(tts: TtsSocket, context_id: str, *clauses: str) -> None:
    await tts.start_context(context_id)
    for clause in clauses:
        await tts.send(context_id, clause, flush=True)
    await tts.flush(context_id)


async def test_two_clauses_produce_aligned_ulaw_audio_then_final(sim: Sim) -> None:
    async with speaking(sim.socket()) as tts:
        await say(tts, "t1-0", "Hello there.", "How are you?")
        items = await collect(tts, final_of("t1-0"))
    audio = [item for item in items if not item.is_final]
    assert {item.context_id for item in items} == {"t1-0"}
    assert items[-1] == TtsAudio("t1-0", b"", None, is_final=True)
    assert spoken(audio) == "Hellothere.Howareyou?"
    # 60 ms per character at 8 bytes per ms; the context's init space is spoken as silence.
    assert sum(len(item.audio) for item in audio) == len(" Hello there. How are you? ") * 60 * 8
    records = await sim.requests()
    assert [(r["context_id"], r["output_format"], r["model_id"]) for r in records] == [
        ("ka", "ulaw_8000", "eleven_flash_v2_5"),
        ("t1-0", "ulaw_8000", "eleven_flash_v2_5"),
    ]
    assert records[1]["text"] == " Hello there. How are you? "
    assert records[1]["end_reason"] == "close_context"
    assert records[1]["interrupted"] is False
    assert records[0]["end_reason"] == "close_socket"
    assert sim.metric("va_tts_ttfa_ms_count", model="eleven_flash_v2_5") == 1


async def test_pcm_16000_audio(sim: Sim) -> None:
    async with speaking(sim.socket(), "pcm_16000") as tts:
        await say(tts, "t1-0", "Hi there.")
        items = await collect(tts, final_of("t1-0"))
    assert sum(len(item.audio) for item in items) == len(" Hi there. ") * 60 * 32
    assert (await sim.requests())[1]["output_format"] == "pcm_16000"


async def test_cancel_during_a_delayed_chunk_drops_the_rest(sim: Sim) -> None:
    await sim.fault(kind="delay", after_chunks=1, delay_ms=400)
    async with speaking(sim.socket()) as tts:
        await say(tts, "t1-0", "This sentence is long enough for several chunks.")
        first = await collect(tts, lambda items: len(items) >= 1)
        await tts.cancel("t1-0")
        rest = await collect(tts, final_of("t1-0"))
    assert not first[0].is_final
    assert rest == [TtsAudio("t1-0", b"", None, is_final=True)]
    record = (await sim.requests())[1]
    assert record["interrupted"] is True
    assert record["end_reason"] == "close_context"


async def test_reconnects_mid_utterance_and_resends_only_the_unconfirmed_clause(sim: Sim) -> None:
    # " Hello there. " is 14 characters = 840 ms = 5 chunks; the drop hits the first chunk of clause 2.
    await sim.fault(kind="disconnect", after_chunks=5)
    tts = sim.socket()
    async with speaking(tts):
        await say(tts, "t1-0", "Hello there.", "How are you?")
        items = await collect(tts, final_of("t1-0"))
    assert {item.context_id for item in items} == {"t1-0"}
    assert spoken(items) == "Hellothere.Howareyou?"
    records = await sim.requests()
    retried = [r for r in records if r["context_id"] == "t1-0-r1"]
    assert len(retried) == 1
    assert retried[0]["text"] == " How are you? "
    assert [r["end_reason"] for r in records if r["context_id"] == "t1-0"] == ["fault"]
    assert tts.reconnects == 1
    assert sim.metric("va_vendor_ws_reconnects_total", vendor="elevenlabs_tts") == 1


async def test_error_fault_is_counted_and_recovered(sim: Sim) -> None:
    await sim.fault(kind="error", after_chunks=0)
    async with speaking(sim.socket()) as tts:
        await say(tts, "t1-0", "Hello there.")
        items = await collect(tts, final_of("t1-0"))
    assert spoken(items) == "Hellothere."
    assert sim.metric("va_vendor_errors_total", vendor="elevenlabs_tts", code="internal_error") == 1
    assert sim.metric("va_vendor_ws_reconnects_total", vendor="elevenlabs_tts") == 1


async def test_repeated_disconnects_end_in_unavailable(sim: Sim) -> None:
    await sim.fault(kind="disconnect", after_chunks=0, times=4)
    tts = sim.socket()
    async with speaking(tts):
        await say(tts, "t1-0", "Hello there.")
        with pytest.raises(TtsUnavailableError):
            await collect(tts, final_of("t1-0"))
    assert tts.reconnects == 3
    assert (await sim.control.get("/sim/elevenlabs/tts/fault")).json() == {"fault": None}


async def test_unknown_voice_is_fatal(sim: Sim) -> None:
    async with speaking(sim.socket(), voice_id="NoSuchVoice000000000") as tts:
        with pytest.raises(TtsVoiceNotFoundError):
            await collect(tts, bool)
    assert await sim.requests() == []


async def test_rejected_key_is_fatal(sim: Sim) -> None:
    (await sim.control.put("/sim/elevenlabs/api-keys", json={"keys": ["someone-else"]})).raise_for_status()
    async with speaking(sim.socket()) as tts:
        with pytest.raises(TtsAuthError):
            await collect(tts, bool)
    assert sim.metric("va_vendor_errors_total", vendor="elevenlabs_tts", code="invalid_api_key") == 1
