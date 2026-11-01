"""ElevenLabsRealtimeStt against a recording WebSocket server on a manual clock: connection, batching, commits
and their throttle, transcript handling, every specs/08 §4 server message row, reconnect replay, rotation,
metrics, spans (specs/08 §2-§4, §7, §11; Decision P03-T11)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest
from stt_client_helpers import (
    API_KEY,
    FRAME,
    RecordingSttServer,
    SttRig,
    audio_of,
    collect_events,
    committed,
    committed_words,
    entities,
    opened_stt,
    partial,
    server_error,
    session_started,
    stt_rig,
)
from ws_server_helpers import Connection, Message

from va_elevenlabs import (
    SttAuthError,
    SttConfig,
    SttError,
    SttProtocolError,
    SttQuotaError,
    SttTermsError,
    SttUnavailableError,
)

SILENCE = b"\xff" * 160
VENDOR = {"vendor": "elevenlabs_stt"}


@pytest.fixture
async def server() -> AsyncIterator[RecordingSttServer]:
    async with RecordingSttServer() as srv:
        yield srv


@pytest.fixture
def r(server: RecordingSttServer) -> SttRig:
    return stt_rig(server.url)


async def frames(r: SttRig, count: int, frame: bytes = FRAME) -> None:
    for _ in range(count):
        await r.stt.send_audio(frame)


def chunk(audio: bytes, *, commit: bool = False, previous_text: str | None = None) -> dict[str, object]:
    """What a received chunk looks like once decoded (``audio`` instead of base64)."""
    expected: dict[str, object] = {"audio": audio, "commit": commit, "sample_rate": 8000}
    if previous_text is not None:
        expected["previous_text"] = previous_text
    return expected


def decoded(message: Message) -> dict[str, object]:
    assert message["message_type"] == "input_audio_chunk"
    out: dict[str, object] = {"audio": audio_of(message), "commit": message["commit"]}
    out["sample_rate"] = message["sample_rate"]
    if "previous_text" in message:
        out["previous_text"] = message["previous_text"]
    return out


async def next_chunk(conn: Connection) -> dict[str, object]:
    return decoded(await conn.next())


async def reconnected(r: SttRig, server: RecordingSttServer, delay: float = 0.05) -> Connection:
    await r.clock.wait_for_sleep(delay)
    await r.clock.run_for(delay)
    return await server.next_connection()


# Connection.


async def test_open_sends_header_and_query_and_stores_the_session(
    r: SttRig, server: RecordingSttServer
) -> None:
    async with opened_stt(r, server, keyterms=["Acme Corp"]) as conn:
        assert conn.headers["xi-api-key"] == API_KEY
        assert conn.path == "/v1/speech-to-text/realtime"
        assert conn.query == [
            ("model_id", "scribe_v2_realtime"),
            ("audio_format", "ulaw_8000"),
            ("commit_strategy", "manual"),
            ("include_timestamps", "true"),
            ("include_language_detection", "true"),
            ("keyterms", "Acme Corp"),
            ("filter_background_audio", "false"),
            ("keepalive_interval_ms", "5000"),
            ("no_verbatim", "false"),
        ]
        assert r.stt.session_id == "sess-1"
        assert not r.stt.rotation_due
    assert r.stt.closed
    assert r.stt.session_id == "sess-1"
    await conn.wait_closed()
    assert conn.ws.close_code == 1000


async def test_handshake_rejection_is_an_auth_error(r: SttRig, server: RecordingSttServer) -> None:
    server.reject_status = 401
    with pytest.raises(SttAuthError):
        await r.stt.open(SttConfig())
    assert server.handshakes == 1
    with pytest.raises(SttAuthError):
        await r.stt.send_audio(FRAME)
    events = await collect_events(r.stt, lambda _: False)
    assert [(event.kind, event.error_type) for event in events] == [("error", "auth_error")]
    await r.stt.close()


async def test_open_retries_connect_errors_then_gives_up(r: SttRig, server: RecordingSttServer) -> None:
    server.reject_status = 503
    opening = r.stt.open(SttConfig())
    with pytest.raises(SttUnavailableError):
        await _drive(r, opening, (0.05, 0.1, 0.2))
    assert server.handshakes == 4


async def _drive(r: SttRig, work: Coroutine[Any, Any, None], delays: tuple[float, ...]) -> None:
    task = asyncio.ensure_future(work)
    for delay in delays:
        await r.clock.wait_for_sleep(delay)
        await r.clock.run_for(delay)
    await task


@pytest.mark.parametrize(
    "echo",
    [{"audio_format": "pcm_16000"}, {"sample_rate": 16000}, {"commit_strategy": "vad"}],
)
async def test_session_started_mismatch_is_fatal(
    r: SttRig, server: RecordingSttServer, echo: dict[str, object]
) -> None:
    server.echo = echo
    with pytest.raises(SttProtocolError) as caught:
        await r.stt.open(SttConfig())
    assert caught.value.code == "config_mismatch"
    await r.stt.close()


async def test_unexpected_first_frame_is_fatal(r: SttRig, server: RecordingSttServer) -> None:
    server.greetings.append(partial("hi"))
    with pytest.raises(SttProtocolError):
        await r.stt.open(SttConfig())


@pytest.mark.parametrize(
    ("frame", "error_class"),
    [
        (server_error("auth_error"), SttAuthError),
        (server_error("quota_exceeded"), SttQuotaError),
        (server_error("unaccepted_terms"), SttTermsError),
    ],
)
async def test_fatal_frame_during_handshake(
    r: SttRig, server: RecordingSttServer, frame: Message, error_class: type[SttError]
) -> None:
    server.greetings.append(frame)
    with pytest.raises(error_class):
        await r.stt.open(SttConfig())
    assert r.metric("va_vendor_errors_total", code=frame["message_type"], **VENDOR) == 1


async def test_auth_close_during_handshake(r: SttRig, server: RecordingSttServer) -> None:
    server.greetings.append(None)
    task = asyncio.ensure_future(r.stt.open(SttConfig()))
    conn = await server.next_connection()
    await conn.close(1008, "Invalid API key")
    with pytest.raises(SttAuthError):
        await task


async def test_invalid_request_during_handshake_retries_minimal(
    r: SttRig, server: RecordingSttServer
) -> None:
    server.greetings.append(server_error("invalid_request", "invalid keyterms: too long"))
    opening = r.stt.open(SttConfig(keyterms=["Acme"], entity_detection=["pii"]))
    await _drive(r, opening, (0.05,))
    first, second = server.connections
    assert ("keyterms", "Acme") in first.query
    assert ("entity_detection", "pii") in first.query
    assert all(key not in {"keyterms", "entity_detection"} for key, _ in second.query)
    await r.stt.close()


async def test_guards(r: SttRig, server: RecordingSttServer) -> None:
    with pytest.raises(RuntimeError):
        await r.stt.send_audio(FRAME)
    async with opened_stt(r, server, audio_format="pcm_16000", sample_rate=16000):
        with pytest.raises(RuntimeError):
            await r.stt.open(SttConfig())
        with pytest.raises(ValueError, match="16-bit"):
            await r.stt.send_audio(b"\x00" * 3)
    with pytest.raises(RuntimeError):
        await r.stt.commit()
    await r.stt.close()


# Sending.


async def test_five_frames_per_message_and_previous_text_only_first(
    r: SttRig, server: RecordingSttServer
) -> None:
    async with opened_stt(r, server, previous_text="Hi, this is Karen.") as conn:
        await frames(r, 4)
        assert conn.pending() == []
        await frames(r, 6)
        assert await next_chunk(conn) == chunk(FRAME * 5, previous_text="Hi, this is Karen.")
        assert await next_chunk(conn) == chunk(FRAME * 5)
        assert r.stt.audio_seconds == pytest.approx(0.2)


async def test_commit_flushes_the_buffer_or_sends_silence(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await r.stt.send_audio(FRAME, commit=True)
        assert await next_chunk(conn) == chunk(FRAME, commit=True)
        await r.clock.run_for(0.3)
        await r.stt.commit()
        assert await next_chunk(conn) == chunk(SILENCE, commit=True)


async def test_pcm16_silence_and_sample_rate(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server, audio_format="pcm_16000", sample_rate=16000) as conn:
        await r.stt.commit()
        message = await conn.next()
        assert (audio_of(message), message["sample_rate"], message["commit"]) == (bytes(640), 16000, True)


async def test_commit_throttle_defers_and_merges(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await r.stt.commit()
        assert (await next_chunk(conn))["commit"] is True
        await r.clock.run_for(0.1)
        await r.stt.send_audio(FRAME, commit=True)
        await r.clock.run_for(0.05)
        await r.stt.commit()
        await frames(r, 5)
        assert await next_chunk(conn) == chunk(FRAME * 5)
        await r.clock.run_for(0.09)
        assert conn.pending() == []
        await r.clock.run_for(0.01)
        assert await next_chunk(conn) == chunk(FRAME, commit=True)
        await frames(r, 5)
        assert await next_chunk(conn) == chunk(FRAME * 5)


# Transcripts.


async def test_partial_and_timestamped_commit(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(partial("hello"))
        event = await r.next_event()
        assert (event.kind, event.text, event.received_at) == ("partial", "hello", 0.0)
        await r.stt.commit()
        await conn.next()
        await r.clock.run_for(0.12)
        await conn.send(committed("hello there"))
        await conn.send(committed_words("hello there", "es"))
        event = await r.next_event()
        assert (event.kind, event.text, event.language_code) == ("committed", "hello there", "es")
        assert [(word.text, word.start_s, word.end_s, word.logprob) for word in event.words] == [
            ("hello", 0.0, 0.08, -0.1),
            ("there", 0.1, 0.18, -0.1),
        ]
        assert r.metric("va_stt_final_ms_sum") == pytest.approx(120.0)
        assert r.metric("va_stt_final_ms_count") == 1


async def test_plain_transcript_is_the_fallback_after_300_ms(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await r.stt.commit()
        await conn.next()
        await conn.send(committed("just text"))
        await r.clock.wait_for_sleep(0.3)
        await r.clock.run_for(0.3)
        event = await r.next_event()
        assert (event.kind, event.text, event.words, event.language_code) == (
            "committed",
            "just text",
            [],
            None,
        )
        await conn.send(committed_words("just text"))
        await conn.send(partial("next"))
        event = await r.next_event()
        assert (event.kind, event.text) == ("partial", "next")


async def test_server_commit_without_a_request(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(committed_words("unasked"))
        event = await r.next_event()
        assert (event.kind, event.text) == ("committed", "unasked")
        assert r.metric("va_stt_final_ms_count") == 0


async def test_entities_update_the_matching_commit(r: SttRig, server: RecordingSttServer) -> None:
    card = {
        "text": "4111 1111 1111 1111",
        "entity_type": "credit_card_number",
        "start_char": 8,
        "end_char": 27,
    }
    async with opened_stt(r, server) as conn:
        await conn.send(committed_words("card is 4111 1111 1111 1111"))
        await conn.send(entities("unrelated", [card]))
        await conn.send(entities("card is 4111 1111 1111 1111", [card]))
        events = await collect_events(r.stt, lambda items: len(items) == 2)
        assert [(event.kind, event.is_update, len(event.entities)) for event in events] == [
            ("committed", False, 0),
            ("committed", True, 1),
        ]
        assert events[1].entities[0].entity_type == "credit_card_number"
        assert events[1].words == events[0].words


@pytest.mark.parametrize("message_type", ["edited_transcript", "insufficient_audio_activity", "brand_new"])
async def test_ignored_messages(r: SttRig, server: RecordingSttServer, message_type: str) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send({"message_type": message_type, "text": "x"})
        await conn.send(session_started([], "again"))
        await conn.send(partial("after"))
        event = await r.next_event()
        assert (event.kind, event.text) == ("partial", "after")


# Server error rows.


async def test_warning_event(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(server_error("warning", "audio is quiet"))
        event = await r.next_event()
        assert (event.kind, event.error_type, event.text) == ("warning", "stt_warning", "audio is quiet")
        assert r.metric("va_vendor_errors_total", code="warning", **VENDOR) == 1


@pytest.mark.parametrize(
    ("message_type", "error_class"),
    [("auth_error", SttAuthError), ("quota_exceeded", SttQuotaError), ("unaccepted_terms", SttTermsError)],
)
async def test_fatal_rows_end_the_stream(
    r: SttRig, server: RecordingSttServer, message_type: str, error_class: type[SttError]
) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(server_error(message_type, "no"))
        events = await collect_events(r.stt, lambda _: False)
        assert [(event.kind, event.error_type, event.text) for event in events] == [
            ("error", message_type, "no")
        ]
        with pytest.raises(error_class):
            await r.stt.send_audio(FRAME)
        with pytest.raises(error_class):
            await r.stt.commit()
        await conn.wait_closed()
    [span] = r.spans("stt.session")
    assert span.status.description == message_type


@pytest.mark.parametrize(
    ("message_type", "delay"),
    [
        ("error", 0.05),
        ("session_time_limit_exceeded", 0.05),
        ("rate_limited", 0.2),
        ("resource_exhausted", 0.2),
        ("transcriber_error", 0.2),
    ],
)
async def test_reconnect_rows_replay_the_kept_audio(
    r: SttRig, server: RecordingSttServer, message_type: str, delay: float
) -> None:
    async with opened_stt(r, server, previous_text="Hello from Acme.") as conn:
        await frames(r, 5)
        await conn.next()
        await conn.send(server_error(message_type))
        event = await r.next_event()
        assert (event.kind, event.error_type) == ("warning", "stt_reconnect")
        await conn.wait_closed()
        await frames(r, 5)
        fresh = await reconnected(r, server, delay)
        assert await next_chunk(fresh) == chunk(FRAME * 5, previous_text="Hello from Acme.")
        assert await next_chunk(fresh) == chunk(FRAME * 5)
        assert r.stt.session_id == "sess-2"
        assert r.stt.reconnects == 1
        assert r.metric("va_vendor_ws_reconnects_total", **VENDOR) == 1
        assert r.metric("va_vendor_errors_total", code=message_type, **VENDOR) == 1
        await frames(r, 5)
        assert await next_chunk(fresh) == chunk(FRAME * 5)
    [span] = r.spans("stt.session")
    assert span.attributes["va.stt.reconnects"] == 1
    assert [s.attributes["elevenlabs.attempt"] for s in r.spans("elevenlabs.stt.connect")] == [0, 1]


async def test_replay_resends_the_outstanding_commit_at_its_position(
    r: SttRig, server: RecordingSttServer
) -> None:
    async with opened_stt(r, server) as conn:
        r.stt.add_context("Agent said hi.")
        await frames(r, 5, b"\x01" * 160)
        await r.stt.send_audio(b"\x02" * 160)
        await r.stt.send_audio(b"\x03" * 160, commit=True)
        await frames(r, 5, b"\x04" * 160)
        await conn.take(3)
        await conn.close(1011, "boom")
        fresh = await reconnected(r, server)
        before = b"\x01" * 800 + b"\x02" * 160 + b"\x03" * 160
        assert await next_chunk(fresh) == chunk(before[:800], previous_text="Agent said hi.")
        assert await next_chunk(fresh) == chunk(before[800:], commit=True)
        assert await next_chunk(fresh) == chunk(b"\x04" * 800)
        await fresh.send(committed_words("one two"))
        event = await r.next_event()
        assert (event.kind, event.error_type) == ("warning", "stt_reconnect")
        event = await r.next_event()
        assert (event.kind, event.text) == ("committed", "one two")
        await fresh.close(1011, "again")
        newest = await reconnected(r, server)
        assert await next_chunk(newest) == chunk(b"\x04" * 800, previous_text="Agent said hi. one two")


async def test_reconnect_with_only_a_commit_outstanding_sends_silence(server: RecordingSttServer) -> None:
    r = stt_rig(server.url, replay_max_s=0.02)
    async with opened_stt(r, server) as conn:
        await frames(r, 5)
        await r.stt.commit()
        await frames(r, 5)
        await conn.take(3)
        await conn.close(1011, "gone")
        fresh = await reconnected(r, server)
        assert await next_chunk(fresh) == chunk(SILENCE, commit=True)
        assert await next_chunk(fresh) == chunk(FRAME)


async def test_reconnect_emits_waiting_plain_transcripts(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await frames(r, 5)
        await r.stt.commit()
        await conn.take(2)
        await conn.send(committed("pending words"))
        await conn.send(partial("sync"))
        await r.next_event()
        await conn.close(1011, "gone")
        fresh = await reconnected(r, server)
        events = await collect_events(r.stt, lambda items: len(items) == 2)
        assert [(event.kind, event.text) for event in events] == [
            ("warning", "reconnecting the STT session"),
            ("committed", "pending words"),
        ]
        await frames(r, 5)
        assert await next_chunk(fresh) == chunk(FRAME * 5)


async def test_invalid_request_reconnects_minimal_then_fails(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server, keyterms=["Acme"], entity_detection=["pii", "pci"]) as conn:
        await conn.send(server_error("invalid_request", "invalid keyterms"))
        fresh = await reconnected(r, server)
        assert all(key not in {"keyterms", "entity_detection"} for key, _ in fresh.query)
        await fresh.send(server_error("invalid_request", "still invalid"))
        events = await collect_events(r.stt, lambda _: False)
        assert [(event.kind, event.error_type) for event in events] == [
            ("warning", "stt_reconnect"),
            ("error", "invalid_request"),
        ]
        with pytest.raises(SttProtocolError):
            await r.stt.send_audio(FRAME)


async def test_invalid_close_without_frame_reconnects_minimal(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server, keyterms=["Acme"]) as conn:
        await conn.close(1008, "invalid keyterms: bad")
        fresh = await reconnected(r, server)
        assert ("keyterms", "Acme") not in fresh.query
        assert r.metric("va_vendor_errors_total", code="invalid_request", **VENDOR) == 1
        await fresh.close(1008, "invalid again")
        events = await collect_events(r.stt, lambda _: False)
        assert events[-1].error_type == "invalid_request"


async def test_auth_close_without_frame_is_fatal(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.close(1008, "Invalid API key")
        events = await collect_events(r.stt, lambda _: False)
        assert [(event.kind, event.error_type) for event in events] == [("error", "auth_error")]


async def test_malformed_frame_is_fatal(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send("not json")
        events = await collect_events(r.stt, lambda _: False)
        assert [(event.kind, event.error_type) for event in events] == [("error", "invalid_server_frame")]


async def test_commit_throttled_resends_the_commit(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await r.stt.send_audio(FRAME, commit=True)
        assert (await next_chunk(conn))["commit"] is True
        await conn.send(server_error("commit_throttled"))
        await r.clock.wait_for_sleep(0.25)
        await r.clock.run_for(0.24)
        assert conn.pending() == []
        await r.clock.run_for(0.01)
        assert await next_chunk(conn) == chunk(SILENCE, commit=True)
        await r.clock.run_for(0.05)
        await conn.send(committed_words("resent"))
        event = await r.next_event()
        assert event.text == "resent"
        assert r.metric("va_stt_final_ms_sum") == pytest.approx(300.0)


@pytest.mark.parametrize(
    ("message_type", "batch_bytes"), [("queue_overflow", 1600), ("chunk_size_exceeded", 400)]
)
async def test_batch_resizing_rows(
    r: SttRig, server: RecordingSttServer, message_type: str, batch_bytes: int
) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(server_error(message_type))
        await conn.send(partial("marker"))
        await r.next_event()
        await frames(r, 20, b"\x01" * 80)
        assert len(audio_of(await conn.next())) == batch_bytes


async def test_input_error_is_logged_and_the_session_continues(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(server_error("input_error", "bad chunk"))
        await conn.send(partial("still here"))
        event = await r.next_event()
        assert event.text == "still here"
        assert r.metric("va_vendor_errors_total", code="input_error", **VENDOR) == 1
        await frames(r, 5)
        assert await next_chunk(conn) == chunk(FRAME * 5)


async def test_three_failed_reconnects_make_the_service_unavailable(
    r: SttRig, server: RecordingSttServer
) -> None:
    async with opened_stt(r, server) as conn:
        server.reject_status = 503
        await conn.close(1011, "down")
        for delay in (0.05, 0.1, 0.2):
            await r.clock.wait_for_sleep(delay)
            await r.clock.run_for(delay)
        events = await collect_events(r.stt, lambda _: False)
        assert [(event.kind, event.error_type) for event in events] == [
            ("warning", "stt_reconnect"),
            ("error", "unavailable"),
        ]
        assert r.metric("va_vendor_ws_reconnects_total", **VENDOR) == 3
        with pytest.raises(SttUnavailableError):
            await r.stt.send_audio(FRAME)


async def test_unhealthy_sessions_count_as_failures(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.close(1011, "first")
        for delay in (0.05, 0.1, 0.2):
            fresh = await reconnected(r, server, delay)
            await fresh.close(1011, "again")
        events = await collect_events(r.stt, lambda _: False)
        assert [event.error_type for event in events] == ["stt_reconnect"] * 4 + ["unavailable"]


async def test_healthy_sessions_reset_the_failure_count(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.close(1011, "first")
        fresh = await reconnected(r, server, 0.05)
        await fresh.send(partial("alive"))
        await collect_events(r.stt, lambda items: items[-1].kind == "partial")
        await fresh.close(1011, "second")
        newest = await reconnected(r, server, 0.05)
        assert r.stt.session_id == "sess-3"
        await r.clock.run_for(1.0)
        await newest.close(1011, "third")
        await reconnected(r, server, 0.05)
        assert r.stt.reconnects == 3


async def test_handshake_failure_during_reconnect_counts(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        server.greetings.append(server_error("rate_limited"))
        await conn.close(1011, "lost")
        await reconnected(r, server, 0.05)
        fresh = await reconnected(r, server, 0.1)
        assert r.stt.session_id == "sess-3"
        assert r.stt.reconnects == 2
        await fresh.send(partial("ok"))


async def test_fatal_handshake_during_reconnect(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        server.greetings.append(server_error("quota_exceeded"))
        await conn.close(1011, "lost")
        await reconnected(r, server, 0.05)
        events = await collect_events(r.stt, lambda _: False)
        assert events[-1].error_type == "quota_exceeded"


async def test_audio_during_the_gap_is_replayed(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.close(1011, "lost")
        await r.clock.wait_for_sleep(0.05)
        await frames(r, 5, b"\x07" * 160)
        await r.stt.commit()
        await r.clock.run_for(0.05)
        fresh = await server.next_connection()
        assert await next_chunk(fresh) == chunk(b"\x07" * 800)
        assert await next_chunk(fresh) == chunk(SILENCE, commit=True)


# Rotation.


async def test_rotation_switches_sessions_and_drains_the_old_one(server: RecordingSttServer) -> None:
    r = stt_rig(server.url, rotate_after_s=10.0)
    async with opened_stt(r, server, previous_text="Opener.") as old:
        await frames(r, 5)
        assert (await next_chunk(old))["previous_text"] == "Opener."
        await r.clock.run_for(10.0)
        assert r.stt.rotation_due
        await r.stt.commit()
        await old.next()
        await r.stt.rotate()
        fresh = await server.next_connection()
        assert (r.stt.session_id, r.stt.rotation_due) == ("sess-2", False)
        await frames(r, 5)
        assert await next_chunk(fresh) == chunk(FRAME * 5, previous_text="Opener.")
        await old.send(committed_words("old session words"))
        event = await r.next_event()
        assert event.text == "old session words"
        await r.clock.run_for(0.49)
        assert not old.closed.is_set()
        await r.clock.run_for(0.01)
        await old.wait_closed()
        assert old.ws.close_code == 1000
        await fresh.send(partial("new"))
        assert (await r.next_event()).text == "new"
        assert r.stt.reconnects == 0
    rotation = [s.attributes["elevenlabs.rotation"] for s in r.spans("elevenlabs.stt.connect")]
    assert rotation == [False, True]


async def test_failed_rotation_keeps_the_session(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        server.reject_status = 500
        await r.stt.rotate()
        event = await r.next_event()
        assert (event.kind, event.error_type) == ("warning", "stt_warning")
        assert r.stt.session_id == "sess-1"
        await frames(r, 5)
        assert await next_chunk(conn) == chunk(FRAME * 5)


async def test_rotate_during_reconnect_is_a_no_op(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.close(1011, "lost")
        await r.clock.wait_for_sleep(0.05)
        await r.stt.rotate()
        assert server.handshakes == 1
        await r.clock.run_for(0.05)
        await server.next_connection()


async def test_close_is_idempotent_and_ends_events(r: SttRig, server: RecordingSttServer) -> None:
    async with opened_stt(r, server) as conn:
        await conn.send(partial("a"))
        await r.next_event()
    await r.stt.close()
    assert await collect_events(r.stt, lambda _: False) == []
    [span] = r.spans("stt.session")
    assert span.attributes["va.stt.reconnects"] == 0
