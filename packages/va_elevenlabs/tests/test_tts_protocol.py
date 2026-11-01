"""TtsSocket against a recording WebSocket server on a manual clock: message sequence, keepalive cadence,
context limits, end of utterance, cancellation, the no-audio watchdog, reconnection, errors, metrics, spans
(specs/09 §2-§3, §8, §11, §12, §14; Decision P03-T10)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from tts_client_helpers import (
    API_KEY,
    Connection,
    RecordingTtsServer,
    Rig,
    audio_frame,
    collect,
    final_frame,
    opened,
    rig,
    voice,
)

from va_common.time import FrozenClock
from va_domain.voices import VoiceSettings
from va_elevenlabs import (
    TtsAuthError,
    TtsError,
    TtsProtocolError,
    TtsSocket,
    TtsUnavailableError,
    TtsVoice,
)
from va_elevenlabs.tts_models import TtsAudio

type Until = Callable[[list[TtsAudio]], bool]

KA_INIT = {"text": " ", "context_id": "ka"}
KEEPALIVE = {"text": "", "context_id": "ka"}
DEFAULT_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 1.0,
}
SCHEDULE = {"chunk_length_schedule": [50, 90, 120, 150]}


def init(context_id: str, settings: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "text": " ",
        "context_id": context_id,
        "voice_settings": settings or DEFAULT_SETTINGS,
        "generation_config": SCHEDULE,
    }


def has_final(context_id: str) -> Until:
    return lambda items: any(item.is_final and item.context_id == context_id for item in items)


def count(n: int) -> Until:
    return lambda items: len(items) >= n


async def utter(r: Rig, conn: Connection, context_id: str, *clauses: str, end: bool = True) -> None:
    """Start ``context_id``, send each clause with ``flush``, end the utterance when ``end``; read what the
    server received (the init and the clauses)."""
    await r.tts.start_context(context_id)
    for clause in clauses:
        await r.tts.send(context_id, clause, flush=True)
    if end:
        await r.tts.flush(context_id)
    await conn.take(1 + len(clauses))


@pytest.fixture
async def server() -> AsyncIterator[RecordingTtsServer]:
    async with RecordingTtsServer() as srv:
        yield srv


async def test_handshake_and_message_sequence(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        assert conn.headers["xi-api-key"] == API_KEY
        assert conn.path == "/v1/text-to-speech/voice123/multi-stream-input"
        assert ("output_format", "ulaw_8000") in conn.query
        assert ("sync_alignment", "true") in conn.query
        await r.tts.start_context("t1-0")
        await r.tts.send("t1-0", "Hello there.", flush=True)
        await r.tts.send("t1-0", "How are you?  ")
        await r.tts.flush("t1-0")
        await r.tts.cancel("t1-0")
        assert await conn.take(5) == [
            init("t1-0"),
            {"text": "Hello there. ", "context_id": "t1-0", "flush": True},
            {"text": "How are you? ", "context_id": "t1-0"},
            {"context_id": "t1-0", "flush": True},
            {"context_id": "t1-0", "close_context": True},
        ]
    assert await conn.next() == {"close_socket": True}
    assert r.tts.closed


async def test_flush_after_a_flushed_clause_sends_nothing(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await r.tts.start_context("t1-0")
        await r.tts.send("t1-0", "Hi.", flush=True)
        await r.tts.flush("t1-0")
        await r.tts.start_context("t1-1")
        assert [m["context_id"] for m in await conn.take(3)] == ["t1-0", "t1-0", "t1-1"]


async def test_context_settings_override_and_guards(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        settings = VoiceSettings(stability=0.1, speed=1.2)
        await r.tts.start_context("t1-0", voice(settings=settings))
        assert (await conn.next())["voice_settings"] == settings.model_dump()
        with pytest.raises(ValueError, match="voice, model, or language"):
            await r.tts.start_context("t1-1", voice("other"))
        with pytest.raises(ValueError, match="voice, model, or language"):
            await r.tts.start_context("t1-1", voice(model_id="eleven_multilingual_v2"))
        with pytest.raises(ValueError, match="reserved or already open"):
            await r.tts.start_context("t1-0")
        with pytest.raises(ValueError, match="reserved or already open"):
            await r.tts.start_context("ka")
        with pytest.raises(ValueError, match="not open for text"):
            await r.tts.send("nope", "Hi.")
        with pytest.raises(ValueError, match="non-whitespace"):
            await r.tts.send("t1-0", "   ")
        await r.tts.flush("t1-0")
        with pytest.raises(ValueError, match="not open for text"):
            await r.tts.send("t1-0", "More.")
        with pytest.raises(ValueError, match="not open for text"):
            await r.tts.flush("t1-0")
        await r.tts.cancel("unknown")


async def test_lifecycle_guards(server: RecordingTtsServer) -> None:
    tts = rig(server.url).tts
    with pytest.raises(RuntimeError, match="open"):
        await tts.start_context("t1-0")
    async with tts:
        await tts.open("pcm_16000", voice())
        with pytest.raises(RuntimeError, match="already open"):
            await tts.open("pcm_16000", voice())
    with pytest.raises(RuntimeError, match="closed"):
        await tts.send("t1-0", "Hi.")
    with pytest.raises(RuntimeError, match="closed"):
        await tts.open("pcm_16000", voice())
    await tts.close()
    assert [item async for item in tts.audio()] == []
    assert [item async for item in tts.audio()] == []


async def test_close_before_open_ends_audio(server: RecordingTtsServer) -> None:
    tts = rig(server.url).tts
    await tts.close()
    assert [item async for item in tts.audio()] == []
    assert server.handshakes == 0


async def test_keepalive_waits_fifteen_seconds(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await r.clock.run_for(14.9)
        await r.tts.start_context("t1-0")
        assert await conn.next() == init("t1-0")


async def test_keepalive_cadence_pauses_while_a_context_is_open(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await r.clock.run_for(15)
        assert await conn.next() == KEEPALIVE
        await r.clock.run_for(15)
        assert await conn.next() == KEEPALIVE
        await r.tts.start_context("t1-0")
        assert await conn.next() == init("t1-0")
        await r.clock.run_for(30)
        await r.tts.cancel("t1-0")
        assert await conn.next() == {"context_id": "t1-0", "close_context": True}
        await collect(r.tts, has_final("t1-0"))
        await r.clock.run_for(15)
        assert await conn.next() == KEEPALIVE


async def test_third_context_cancels_the_oldest(server: RecordingTtsServer) -> None:
    r = rig(server.url, keepalive_s=1000.0)
    server.auto_final = False
    async with opened(r.tts, server) as conn:
        for context_id in ("t1-0", "t1-1", "t2-0"):
            await r.tts.start_context(context_id)
        assert await conn.take(4) == [
            init("t1-0"),
            init("t1-1"),
            {"context_id": "t1-0", "close_context": True},
            init("t2-0"),
        ]
        await r.tts.start_context("t3-0")
        assert await conn.take(2) == [{"context_id": "t1-1", "close_context": True}, init("t3-0")]


async def test_audio_is_forwarded_and_close_context_follows_the_tail_grace(
    server: RecordingTtsServer,
) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hello there.")
        await conn.send(audio_frame("t1-0", "Hello ", audio=b"\x01" * 80))
        await conn.send(audio_frame("t1-0", "there. ", audio=b"\x02" * 80))
        items = await collect(r.tts, count(2))
        assert [(i.context_id, i.audio, i.is_final) for i in items] == [
            ("t1-0", b"\x01" * 80, False),
            ("t1-0", b"\x02" * 80, False),
        ]
        assert items[1].alignment is not None
        assert "".join(items[1].alignment.chars) == "there. "
        await r.clock.run_for(0.29)
        await r.tts.start_context("t1-1")
        assert await conn.next() == init("t1-1")
        await r.clock.run_for(0.01)
        assert await conn.next() == {"context_id": "t1-0", "close_context": True}
        final = await collect(r.tts, count(1))
        assert final == [TtsAudio("t1-0", b"", None, is_final=True)]
        await conn.send(final_frame("t1-0"))
        await conn.send(audio_frame("t1-1", None))
        await r.tts.send("t1-1", "Next.")
        next_items = await collect(r.tts, count(1))
        assert next_items[0].context_id == "t1-1"
        assert not next_items[0].is_final


async def test_unaligned_tail_waits_the_no_audio_window(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hello there.")
        await conn.send(audio_frame("t1-0", None))
        await collect(r.tts, count(1))
        await r.clock.run_for(2.9)
        await r.tts.start_context("t1-1")
        assert await conn.next() == init("t1-1")
        await r.clock.run_for(0.1)
        assert await conn.next() == {"context_id": "t1-0", "close_context": True}


async def test_cancel_drops_late_audio_and_still_reports_final(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    server.auto_final = False
    async with opened(r.tts, server) as conn:
        await r.tts.start_context("t1-0")
        await r.tts.send("t1-0", "Hello there. How are you?", flush=True)
        await conn.take(2)
        await conn.send(audio_frame("t1-0", "Hello "))
        await collect(r.tts, count(1))
        await r.tts.cancel("t1-0")
        await r.tts.cancel("t1-0")
        assert await conn.next() == {"context_id": "t1-0", "close_context": True}
        with pytest.raises(ValueError, match="not open for text"):
            await r.tts.send("t1-0", "More.")
        await conn.send(audio_frame("t1-0", "there. "))
        await conn.send(audio_frame("t1-0", "How "))
        await conn.send(final_frame("t1-0"))
        items = await collect(r.tts, count(1))
        assert items == [TtsAudio("t1-0", b"", None, is_final=True)]
        assert r.metric("va_tts_late_audio_dropped_total") == 2
        assert conn.pending() == []


async def test_unknown_contexts_empty_audio_and_ignored_frames(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hi.", end=False)
        await conn.send(final_frame("ka"))
        await conn.send(audio_frame("zz", "x"))
        await conn.send({"audio": "", "contextId": "t1-0", "alignment": None})
        await conn.send({"type": "future_message"})
        await conn.send(audio_frame("t1-0", "Hi."))
        items = await collect(r.tts, count(1))
        assert items[0].context_id == "t1-0"
        assert items[0].audio


async def test_no_audio_watchdog_reconnects_and_resends_unconfirmed_clauses(
    server: RecordingTtsServer,
) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hello there.", "How are you?", end=False)
        await conn.send(audio_frame("t1-0", "Hello "))
        await collect(r.tts, count(1))
        await r.clock.run_for(3.0)
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        second = await server.next_connection()
        assert await second.take(3) == [
            KA_INIT,
            init("t1-0-r1"),
            {"text": "How are you? ", "context_id": "t1-0-r1", "flush": True},
        ]
        await second.send(audio_frame("t1-0-r1", "How "))
        items = await collect(r.tts, count(1))
        assert items[0].context_id == "t1-0"
        assert r.metric("va_vendor_ws_reconnects_total", vendor="elevenlabs_tts") == 1
        assert r.metric("va_vendor_errors_total", vendor="elevenlabs_tts", code="no_audio_timeout") == 1
        assert r.tts.reconnects == 1
        # A healthy socket resets the failure count: the next loss waits the first delay again.
        await second.close(1011, "boom")
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        third = await server.next_connection()
        assert (await third.take(2))[1] == init("t1-0-r2")


async def test_reconnect_replays_bare_flush_and_finishes_gone_contexts(server: RecordingTtsServer) -> None:
    r = rig(server.url, keepalive_s=1000.0)
    server.auto_final = False
    async with opened(r.tts, server) as conn:
        await r.tts.start_context("t1-0")
        await r.tts.send("t1-0", "One two.")
        await r.tts.send("t1-0", "Three four.")
        await r.tts.flush("t1-0")
        await r.tts.start_context("t2-0")
        await r.tts.send("t2-0", "Done now.", flush=True)
        await conn.take(6)
        await conn.send(audio_frame("t2-0", "Done now."))
        await collect(r.tts, count(1))
        await r.tts.flush("t2-0")
        await r.tts.cancel("t1-0")
        assert await conn.next() == {"context_id": "t1-0", "close_context": True}
        await r.tts.start_context("t3-0")
        await r.tts.send("t3-0", "Alpha.")
        await r.tts.send("t3-0", "Beta.")
        await r.tts.flush("t3-0")
        await conn.take(4)
        await conn.close(1011, "gone")
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        second = await server.next_connection()
        assert await second.take(4) == [
            KA_INIT,
            init("t3-0-r1"),
            {"text": "Alpha. ", "context_id": "t3-0-r1"},
            {"text": "Beta. ", "context_id": "t3-0-r1", "flush": True},
        ]
        finals = await collect(r.tts, count(2))
        assert sorted(item.context_id for item in finals if item.is_final) == ["t1-0", "t2-0"]


async def test_connect_failures_after_a_loss_end_in_unavailable(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        server.reject_status = 503
        await conn.close(1011, "simulated disconnect")
        for delay in (0.05, 0.1, 0.2):
            await r.clock.wait_for_sleep(delay)
            await r.clock.run_for(delay)
        with pytest.raises(TtsUnavailableError, match="3 reconnect attempts failed"):
            await collect(r.tts, count(1))
        with pytest.raises(TtsUnavailableError):
            await r.tts.send("t1-0", "Hi.")
        assert r.metric("va_vendor_ws_reconnects_total", vendor="elevenlabs_tts") == 3
        assert server.handshakes == 4


async def test_sockets_closing_before_audio_count_as_failed_attempts(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hi.", end=False)
        await conn.close(1011, "a")
        for delay in (0.05, 0.1, 0.2):
            await r.clock.wait_for_sleep(delay)
            await r.clock.run_for(delay)
            fresh = await server.next_connection()
            await fresh.take(3)
            await fresh.close(1012, "restart")
        with pytest.raises(TtsUnavailableError):
            await collect(r.tts, count(1))
        context_spans = r.spans("elevenlabs.tts.context")
        assert [span.status.status_code.name for span in context_spans] == ["ERROR"]


async def test_handshake_auth_rejection_on_reconnect_is_fatal(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        server.reject_status = 401
        await conn.close(1011, "x")
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        with pytest.raises(TtsAuthError, match="HTTP 401"):
            await collect(r.tts, count(1))


async def test_open_rejected_handshake_is_auth_error(server: RecordingTtsServer) -> None:
    server.reject_status = 403
    tts = TtsSocket(rig(server.url).tts._config, clock=FrozenClock())
    with pytest.raises(TtsAuthError, match="HTTP 403"):
        await tts.open("ulaw_8000", voice())
    await tts.close()


async def test_open_retries_then_reports_unavailable() -> None:
    async with RecordingTtsServer() as srv:
        url = srv.url
    tts = TtsSocket(rig(url).tts._config, clock=FrozenClock())
    with pytest.raises(TtsUnavailableError, match="after 4 attempts"):
        await tts.open("ulaw_8000", voice())
    await tts.close()


async def test_open_succeeds_after_transient_failures(server: RecordingTtsServer) -> None:
    server.reject_status = 503
    r = rig(server.url)
    task = asyncio.create_task(r.tts.open("ulaw_8000", voice()))
    await r.clock.wait_for_sleep(0.05)
    server.reject_status = None
    await r.clock.run_for(0.05)
    await task
    conn = await server.next_connection()
    assert await conn.next() == KA_INIT
    assert server.handshakes == 2
    await r.tts.close()


async def test_recoverable_error_frame_is_counted_then_reconnects(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await conn.send({"message": "Quota exceeded", "error": "quota_exceeded", "code": 1008})
        await conn.close(1011, "quota")
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        second = await server.next_connection()
        assert await second.next() == KA_INIT
        assert r.metric("va_vendor_errors_total", vendor="elevenlabs_tts", code="quota_exceeded") == 1


@pytest.mark.parametrize(
    ("frame", "kind", "code"),
    [
        ({"message": "closed", "error": "context_closed", "code": 1008}, TtsProtocolError, "context_closed"),
        ('{"audio": 1, "contextId": "t1-0"}', TtsProtocolError, "invalid_server_frame"),
        ({"message": "bad key", "error": "invalid_api_key"}, TtsAuthError, "invalid_api_key"),
    ],
)
async def test_fatal_frames_stop_the_socket(
    server: RecordingTtsServer, frame: dict[str, object] | str, kind: type[TtsError], code: str
) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await r.tts.start_context("t1-0")
        await conn.next()
        await conn.send(frame)
        with pytest.raises(kind) as caught:
            await collect(r.tts, count(1))
        assert caught.value.code == code
        with pytest.raises(kind):
            await r.tts.send("t1-0", "Hi.")
        await r.tts.cancel("t1-0")
        async with asyncio.timeout(5):
            await conn.closed.wait()


async def test_invalid_api_key_close_is_fatal(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await conn.close(1008, "Invalid API key")
        with pytest.raises(TtsAuthError):
            await collect(r.tts, count(1))
        assert r.metric("va_vendor_errors_total", vendor="elevenlabs_tts", code="invalid_api_key") == 1


async def test_inactivity_close_reconnects(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await conn.close(1008, "Have not received a new text input within the timeout of 180 seconds.")
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        assert await (await server.next_connection()).next() == KA_INIT


async def test_send_timeout_takes_the_reconnect_path(server: RecordingTtsServer) -> None:
    r = rig(server.url, send_timeout_s=0.05)
    async with opened(r.tts, server):
        await r.tts.start_context("t1-0")
        ws = r.tts._ws
        assert ws is not None
        hang = asyncio.Event()

        async def stuck(_payload: str) -> None:
            await hang.wait()

        ws.send = stuck  # type: ignore[method-assign, assignment]
        await r.tts.send("t1-0", "Hello.", flush=True)
        await r.clock.wait_for_sleep(0.05)
        await r.clock.run_for(0.05)
        second = await server.next_connection()
        assert await second.take(3) == [
            KA_INIT,
            init("t1-0-r1"),
            {"text": "Hello. ", "context_id": "t1-0-r1", "flush": True},
        ]


async def test_reader_crash_becomes_a_terminal_error(
    server: RecordingTtsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:

        def boom(_raw: object) -> None:
            raise LookupError

        monkeypatch.setattr(r.tts, "_on_frame", boom)
        await conn.send(final_frame("t1-0"))
        with pytest.raises(TtsError, match="reader task crashed: LookupError") as caught:
            await collect(r.tts, count(1))
        assert caught.value.code == "client_error"


async def test_metrics_and_spans(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server) as conn:
        await utter(r, conn, "t1-0", "Hello there.", "Bye.")
        await r.clock.run_for(0.25)
        await conn.send(audio_frame("t1-0", "Hello there. Bye."))
        await conn.send(audio_frame("t1-0", " "))
        await collect(r.tts, count(2))
        await r.clock.run_for(0.3)
        await collect(r.tts, has_final("t1-0"))
    model = {"model": "eleven_flash_v2_5"}
    assert r.metric("va_tts_characters_total", **model) == len("Hello there. ") + len("Bye. ")
    assert r.metric("va_tts_ttfa_ms_count", **model) == 1
    assert r.metric("va_tts_ttfa_ms_sum", **model) == pytest.approx(250.0)
    (context,) = r.spans("elevenlabs.tts.context")
    assert context.attributes is not None
    assert context.attributes["elevenlabs.context_id"] == "t1-0"
    assert context.attributes["elevenlabs.characters"] == 18
    assert context.attributes["elevenlabs.ttfa_ms"] == pytest.approx(250.0)
    assert context.attributes["elevenlabs.reconnects"] == 0
    (connect,) = r.spans("elevenlabs.tts.connect")
    assert connect.attributes == {
        "elevenlabs.voice_id": "voice123",
        "elevenlabs.model_id": "eleven_flash_v2_5",
        "elevenlabs.attempt": 0,
    }


async def test_close_ends_open_context_spans_ok(server: RecordingTtsServer) -> None:
    r = rig(server.url)
    async with opened(r.tts, server):
        await r.tts.start_context("t1-0")
    (context,) = r.spans("elevenlabs.tts.context")
    assert context.status.status_code.name == "UNSET"


def test_voice_model_defaults() -> None:
    v = TtsVoice(provider_voice_id="x")
    assert (v.model_id, v.language_code, v.settings) == ("eleven_flash_v2_5", None, VoiceSettings())
