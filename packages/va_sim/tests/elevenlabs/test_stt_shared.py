"""Cross-process utterance markers (specs/30 §3 Decision P03-T15c): caller audio synthesized with an id
allocated by one sim process is transcribed by another process's FakeElevenLabs STT."""

from __future__ import annotations

import numpy as np
from fakeredis import FakeAsyncRedis, FakeServer
from stt_helpers import ULAW, chunked, encode, recv_type, send_audio, silence, stt_socket
from tts_helpers import realtime_state, recv_json, served

from va_sim.audio import synthesize_pcm16
from va_sim.elevenlabs.stt_params import AUDIO_FORMATS
from va_sim.shared import SharedState

TEXT = "please call me back tomorrow"


async def test_a_marker_allocated_by_another_process_is_transcribed() -> None:
    server = FakeServer()
    redis_a, redis_b = FakeAsyncRedis(server=server), FakeAsyncRedis(server=server)
    caller, listener = realtime_state(), realtime_state()
    try:
        await caller.attach_shared(SharedState(redis_a))
        await listener.attach_shared(SharedState(redis_b))
        await listener.allocate_utterance("an earlier line", "en", ("an", "earlier", "line"))
        utterance_id = await caller.allocate_utterance(TEXT, "en", tuple(TEXT.split()))
        assert utterance_id == 2
        fmt = AUDIO_FORMATS["ulaw_8000"]
        speech = synthesize_pcm16(
            utterance_id, TEXT, sample_rate=8000, seed=caller.utterance_seed(utterance_id)
        )
        audio = encode(np.concatenate([silence(fmt, 200), speech, silence(fmt, 400)]), fmt)
        async with served(listener) as base, stt_socket(base, ULAW) as ws:
            assert (await recv_json(ws))["message_type"] == "session_started"
            for piece in chunked(audio, fmt):
                await send_audio(ws, piece)
            await send_audio(ws, b"", commit=True)
            committed, _ = await recv_type(ws, "committed_transcript")
        assert committed == {"message_type": "committed_transcript", "text": TEXT}
        (record,) = listener.elevenlabs.stt.sessions
        assert record.transcripts[0].utterance_ids == [utterance_id]
    finally:
        await caller.aclose()
        await listener.aclose()
        await redis_a.aclose()
        await redis_b.aclose()
