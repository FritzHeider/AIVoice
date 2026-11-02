"""``TelnyxMediaTransport`` against FakeTelnyx: caller audio, DTMF and mark echoes in; media, marks and clear
out (PCMU 8 kHz and the hd_audio L16 16 kHz leg)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sim_media import StreamedCall, streamed_call
from telnyx_helpers import ok

from va_media.session import STREAM_CLOSED, CallSession, SessionEnd
from va_media.transport import TelnyxMediaTransport
from va_voice.audio.formats import L16_16000, ULAW_8000
from va_voice.pipeline.protocols import InboundAudio, InboundDtmf, MarkEcho, TransportEvent

WAIT_S = 5.0


class TransportBody:
    """A session body that hands the transport to the test and queues every inbound event."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[TransportEvent] = asyncio.Queue()
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self._transport: TelnyxMediaTransport | None = None

    @property
    def transport(self) -> TelnyxMediaTransport:
        assert self._transport is not None
        return self._transport

    async def __call__(self, session: CallSession) -> SessionEnd:
        self._transport = TelnyxMediaTransport(session.socket, session.start)
        self.started.set()
        try:
            async for event in self._transport.events():
                self.events.put_nowait(event)
        finally:
            self.ended.set()
        return STREAM_CLOSED

    async def next_of[T](self, kind: type[T]) -> T:
        """The next queued event of ``kind`` (caller audio and other kinds in between are skipped)."""
        async with asyncio.timeout(WAIT_S):
            while True:
                event = await self.events.get()
                if isinstance(event, kind):
                    return event


async def caller_audio(body: TransportBody, count: int) -> list[InboundAudio]:
    return [await body.next_of(InboundAudio) for _ in range(count)]


async def hang_up(streamed: StreamedCall, body: TransportBody) -> None:
    await ok(streamed.client, streamed.ccid, "hangup")
    async with asyncio.timeout(WAIT_S):
        await body.ended.wait()
    assert body.transport.stop_received or body.transport.closed_by_peer is not None


async def test_pcmu_stream_in_both_directions(models_dir: Path) -> None:
    body = TransportBody()
    async with streamed_call(models_dir, body) as streamed:
        async with asyncio.timeout(WAIT_S):
            await body.started.wait()
        transport = body.transport
        assert transport.fmt == ULAW_8000

        audio = await caller_audio(body, 5)
        chunks = [frame.chunk for frame in audio]
        assert chunks == sorted(chunks)
        assert len(set(chunks)) == len(chunks)
        assert {len(frame.payload) for frame in audio} == {160}

        for _ in range(10):
            await transport.send_audio(bytes([0x10, 0x90]) * 80)
        await transport.send_mark("seg:0:0")
        assert await body.next_of(MarkEcho) == MarkEcho("seg:0:0")

        response = await streamed.client.post(
            f"/sim/telnyx/calls/{streamed.ccid}/dtmf", json={"digits": "12"}
        )
        assert response.status_code == 202, response.text
        assert [await body.next_of(InboundDtmf) for _ in range(2)] == [InboundDtmf("1"), InboundDtmf("2")]

        await transport.clear()
        await transport.send_mark("after-clear")
        assert await body.next_of(MarkEcho) == MarkEcho("after-clear")
        session = streamed.sim_call().stream
        assert session is not None
        assert session.session is not None
        assert session.session.received == {"media": 10, "mark": 2, "clear": 1}

        await hang_up(streamed, body)
        await transport.send_audio(bytes(160))
        assert transport.stats.dropped_sends == 1


async def test_hd_audio_l16_leg(models_dir: Path) -> None:
    body = TransportBody()
    hd: dict[str, Any] = {
        "stream_codec": "L16",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "L16",
        "stream_bidirectional_sampling_rate": 16000,
    }
    async with streamed_call(models_dir, body, **hd) as streamed:
        async with asyncio.timeout(WAIT_S):
            await body.started.wait()
        transport = body.transport
        assert transport.fmt == L16_16000
        audio = await caller_audio(body, 3)
        assert {len(frame.payload) for frame in audio} == {640}

        for _ in range(5):
            await transport.send_audio(b"\x10\x00" * 320)
        await transport.send_mark("seg:1:0")
        assert await body.next_of(MarkEcho) == MarkEcho("seg:1:0")
        session = streamed.sim_call().stream
        assert session is not None
        assert session.session is not None
        assert session.session.received == {"media": 5, "mark": 1}
        assert transport.stats.dropped_frames == {}
        await hang_up(streamed, body)
