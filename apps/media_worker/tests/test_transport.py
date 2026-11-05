"""``TelnyxMediaTransport`` over an in-memory socket: inbound events, drops, errors, serialized sends."""

from __future__ import annotations

import asyncio
import base64

import orjson
import pytest
from media_helpers import (
    FakeSocket,
    connected_msg,
    dtmf_msg,
    error_msg,
    mark_msg,
    media_msg,
    start_msg,
    stop_msg,
)
from service_helpers import json_log_stream, log_events

from va_media.session import StreamStart
from va_media.transport import TelnyxMediaTransport, UnsupportedMediaFormatError, leg_format, stream_format
from va_telnyx.media import MediaFormat
from va_voice.audio.formats import L16_16000, ULAW_8000
from va_voice.outbound import SendTiming, TimedSink
from va_voice.pipeline.protocols import InboundAudio, InboundDtmf, MarkEcho, TransportEvent

ULAW_FRAME = bytes(range(160))
L16_FRAME = bytes(640)


def start_of(encoding: str = "PCMU", rate: int = 8000, channels: int = 1) -> StreamStart:
    media_format = MediaFormat.model_validate(
        {"encoding": encoding, "sample_rate": rate, "channels": channels}
    )
    return StreamStart("stream-1", "cc-1", media_format)


async def collect(transport: TelnyxMediaTransport) -> list[TransportEvent]:
    return [event async for event in transport.events()]


def sent(socket: FakeSocket) -> list[dict[str, object]]:
    return [orjson.loads(message) for message in socket.sent]


@pytest.mark.parametrize(
    ("encoding", "rate", "expected"), [("PCMU", 8000, ULAW_8000), ("L16", 16000, L16_16000)]
)
def test_leg_format_of_supported_streams(encoding: str, rate: int, expected: object) -> None:
    assert leg_format(start_of(encoding, rate).media_format) == expected


@pytest.mark.parametrize(
    ("encoding", "rate", "expected"), [("PCMU", 8000, "PCMU/8000"), ("L16", 16000, "L16/16000")]
)
def test_stream_format_names_encoding_and_rate(encoding: str, rate: int, expected: str) -> None:
    assert stream_format(start_of(encoding, rate).media_format) == expected


@pytest.mark.parametrize(
    ("encoding", "rate", "channels"), [("PCMU", 16000, 1), ("PCMA", 8000, 1), ("PCMU", 8000, 2)]
)
def test_leg_format_rejects_other_streams(encoding: str, rate: int, channels: int) -> None:
    with pytest.raises(UnsupportedMediaFormatError, match=f"{encoding}/{rate}/{channels}"):
        leg_format(start_of(encoding, rate, channels).media_format)
    with pytest.raises(UnsupportedMediaFormatError):
        TelnyxMediaTransport(FakeSocket(), start_of(encoding, rate, channels))


async def test_inbound_events_pass_through_in_arrival_order_until_stop() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    for message in (
        connected_msg(),
        start_msg(),
        media_msg(2, ULAW_FRAME),
        media_msg(1, ULAW_FRAME[::-1]),
        dtmf_msg("5"),
        mark_msg("seg:0:0"),
        orjson.dumps({"event": "surprise"}).decode(),
        stop_msg(),
        media_msg(3, ULAW_FRAME),
    ):
        socket.feed(message)
    events = await collect(transport)
    assert events == [
        InboundAudio(2, ULAW_FRAME),
        InboundAudio(1, ULAW_FRAME[::-1]),
        InboundDtmf("5"),
        MarkEcho("seg:0:0"),
    ]
    assert transport.stop_received
    assert transport.closed_by_peer is None
    assert transport.ended
    assert (transport.stats.frames, transport.stats.dtmf, transport.stats.marks) == (2, 1, 1)
    assert (transport.stream_id, transport.fmt) == ("stream-1", ULAW_8000)


async def test_peer_close_ends_the_iterator() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    socket.feed(media_msg(1, ULAW_FRAME))
    socket.hang_up()
    assert await collect(transport) == [InboundAudio(1, ULAW_FRAME)]
    assert transport.closed_by_peer == 1000
    assert not transport.stop_received
    assert transport.ended


async def test_frames_that_are_not_caller_audio_are_dropped_and_counted() -> None:
    stream = json_log_stream()
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of("L16", 16000))
    for message in (
        media_msg(1, L16_FRAME, track="outbound"),
        media_msg(2, L16_FRAME, track="outbound"),
        media_msg(3, b""),
        media_msg(4, bytes(639)),
        media_msg(5, L16_FRAME, track=None),
        media_msg(6, L16_FRAME),
    ):
        socket.feed(message)
    socket.feed(stop_msg())
    assert await collect(transport) == [InboundAudio(5, L16_FRAME), InboundAudio(6, L16_FRAME)]
    assert transport.stats.dropped_frames == {"track": 2, "empty": 1, "invalid_media": 1}
    drops = [e["reason"] for e in log_events(stream) if e["event"] == "media.frame_dropped"]
    assert drops == ["track", "empty", "invalid_media"], "one warning per kind"


async def test_malformed_messages_are_skipped_without_logging_the_payload() -> None:
    stream = json_log_stream()
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    socket.feed("not json secret-payload")
    socket.feed(orjson.dumps({"event": "media", "media": {"chunk": "x"}}).decode())
    socket.feed(media_msg(1, ULAW_FRAME))
    socket.feed(stop_msg())
    assert await collect(transport) == [InboundAudio(1, ULAW_FRAME)]
    assert transport.stats.protocol_errors == 2
    logged = stream.getvalue()
    assert logged.count("media.protocol_error") == 2
    assert "secret-payload" not in logged


async def test_error_frames_rate_limit_callback_and_levels() -> None:
    stream = json_log_stream()
    socket = FakeSocket()
    limited: list[int] = []
    transport = TelnyxMediaTransport(socket, start_of(), on_rate_limited=lambda: limited.append(1))
    for code in (100005, 100005, 100003, 100004, 100002):
        socket.feed(error_msg(code))
    socket.feed(media_msg(1, ULAW_FRAME))
    socket.feed(stop_msg())
    assert await collect(transport) == [InboundAudio(1, ULAW_FRAME)], "errors never end the stream"
    assert limited == [1, 1]
    assert transport.stats.errors == {100005: 2, 100003: 1, 100004: 1, 100002: 1}
    levels = {(e["code"], e["level"]) for e in log_events(stream) if e["event"].startswith("media.telnyx_")}
    assert levels == {(100005, "warning"), (100003, "error"), (100004, "error"), (100002, "warning")}


async def test_rate_limit_without_a_callback_is_counted() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    socket.feed(error_msg(100005))
    socket.feed(stop_msg())
    assert await collect(transport) == []
    assert transport.stats.errors == {100005: 1}


async def test_events_has_a_single_consumer() -> None:
    transport = TelnyxMediaTransport(FakeSocket(), start_of())
    transport.events()
    with pytest.raises(RuntimeError, match="already"):
        transport.events()


async def test_outbound_messages_are_the_telnyx_json() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    await transport.send_audio(ULAW_FRAME)
    await transport.send_mark("seg:0:0")
    await transport.clear()
    media, mark, clear = sent(socket)
    assert media == {"event": "media", "media": {"payload": base64.b64encode(ULAW_FRAME).decode()}}
    assert base64.b64decode(str(media["media"]["payload"])) == ULAW_FRAME  # type: ignore[index]
    assert mark == {"event": "mark", "mark": {"name": "seg:0:0"}}
    assert clear == {"event": "clear"}
    stats = transport.stats
    assert (stats.sent_frames, stats.sent_marks, stats.clears, stats.dropped_sends) == (1, 1, 1, 0)
    with pytest.raises(ValueError, match="empty"):
        await transport.send_audio(b"")


async def test_concurrent_sends_are_serialized() -> None:
    socket = FakeSocket()
    socket.send_gate = asyncio.Event()
    transport = TelnyxMediaTransport(socket, start_of())
    sends = asyncio.gather(transport.send_audio(ULAW_FRAME), transport.clear(), transport.send_mark("m"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert socket.sent == []
    assert transport._send_lock.locked()
    socket.send_gate.set()
    await sends
    assert [message["event"] for message in sent(socket)] == ["media", "clear", "mark"]


class StepTimer:
    """Each read returns the next scripted time (seconds)."""

    def __init__(self, *times: float) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


async def test_audio_send_timing_separates_lock_wait_from_socket_time() -> None:
    socket = FakeSocket()
    socket.send_gate = asyncio.Event()
    # mark: requested 0.0, locked 0.0; audio: requested 0.001, locked 0.030, done 0.032.
    transport = TelnyxMediaTransport(socket, start_of(), timer=StepTimer(0.0, 0.0, 0.001, 0.030, 0.032))
    assert isinstance(transport, TimedSink)
    assert TelnyxMediaTransport(FakeSocket(), start_of()).last_audio_timing is None
    mark = asyncio.create_task(transport.send_mark("m"))
    await asyncio.sleep(0)
    audio = asyncio.create_task(transport.send_audio(ULAW_FRAME))
    await asyncio.sleep(0)
    assert transport._send_lock.locked()
    socket.send_gate.set()
    await asyncio.gather(mark, audio)
    timing = transport.last_audio_timing
    assert timing is not None
    assert timing.lock_wait_ms == pytest.approx(29.0)
    assert timing.socket_ms == pytest.approx(2.0)
    assert [message["event"] for message in sent(socket)] == ["mark", "media"]


async def test_a_dropped_audio_send_still_records_its_timing() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of(), timer=StepTimer(1.0, 1.0, 1.0))
    socket.hang_up()
    await transport.send_audio(ULAW_FRAME)
    assert transport.last_audio_timing == SendTiming(lock_wait_ms=0.0, socket_ms=0.0)


async def test_sends_after_the_stream_ended_are_dropped() -> None:
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    socket.feed(stop_msg())
    await collect(transport)
    await transport.send_audio(ULAW_FRAME)
    await transport.send_mark("m")
    assert socket.sent == []
    assert transport.stats.dropped_sends == 2

    hung = FakeSocket()
    transport = TelnyxMediaTransport(hung, start_of())
    hung.hang_up()  # the reader has not noticed yet: the send itself finds the socket closed
    await transport.send_audio(ULAW_FRAME)
    assert transport.ended
    await transport.clear()
    assert transport.stats.dropped_sends == 2
    assert (transport.stats.sent_frames, transport.stats.clears) == (0, 0)


async def test_digits_and_payloads_never_reach_the_logs() -> None:
    stream = json_log_stream()
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, start_of())
    socket.feed(dtmf_msg("7"))
    socket.feed(media_msg(1, ULAW_FRAME, track="outbound"))
    socket.feed(stop_msg())
    assert await collect(transport) == [InboundDtmf("7")]
    transport.log_summary()
    summary = next(e for e in log_events(stream) if e["event"] == "media.transport_closed")
    assert summary["dtmf"] == 1
    assert summary["dropped_frames"] == {"track": 1}
    assert summary["stop_received"] is True
    assert '"7"' not in stream.getvalue()
    assert base64.b64encode(ULAW_FRAME).decode() not in stream.getvalue()
