"""FakeTelnyx media WebSocket client: one session per stream (specs/30-testing-and-evals.md §3.2 media WS
client and clock; specs/06-telephony-telnyx.md §4.2, §8; Decision 2026-10-29 P03-T03b rules 2 and 4 to 11).

The session connects to ``stream_url`` with the Ed25519 stream signature, sends ``connected`` and ``start``,
and then runs two loops until the stream stops: a tick loop that every 20 ms on the sim clock plays one frame
of queued agent audio, echoes due marks, sends pending frames (errors, injected frames, one DTMF digit), and
sends one caller ``media`` frame; and a receive loop that parses agent ``media``/``mark``/``clear`` frames.
Only the tick loop writes to the socket, so frame order and sequence numbers stay consistent. On an
accelerated clock virtual time does not move while the session connects (the handshake takes no simulated
time). Listeners (the scenario caller runner, Decision 2026-10-29 P03-T03c) hear about every tick, with
whether the agent frame it played was speech, and about the end of the session.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np
import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from va_common.logging import get_logger
from va_sim.clock import iso_z
from va_sim.tasks import hold_clock
from va_sim.telnyx.agent_speech import AGENT_SPEECH_LEVEL, is_speech
from va_sim.telnyx.keys import SIGNATURE_HEADER, TIMESTAMP_HEADER
from va_sim.telnyx.media_format import FRAME_MS, TRACK_RATE, agent_format, stream_format
from va_sim.telnyx.media_frames import (
    AgentClear,
    AgentMark,
    AgentMedia,
    MediaFault,
    StreamIdentity,
    connected_frame,
    dtmf_frame,
    error_frame,
    mark_frame,
    media_frame,
    parse_agent_frame,
    start_frame,
    stop_frame,
)
from va_sim.telnyx.playout import CallerQueue, Playout

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.telnyx.call_media import CallMedia
    from va_sim.telnyx.calls import Call, StreamState

__all__ = ["AgentTick", "MediaListener", "MediaSession"]

_FRAME_S: Final = FRAME_MS / 1000
_OPEN_TIMEOUT_S: Final = 5.0
_CLOSE_TIMEOUT_S: Final = 2.0
_MAX_MESSAGE_BYTES: Final = 2**22
_log: Final = get_logger(__name__)

type Frame = dict[str, Any]


class _PeerClosedError(Exception):
    """The agent closed the WebSocket before the stream was stopped."""


@dataclass(frozen=True, slots=True)
class AgentTick:
    """One tick of the session: its sim time and whether the agent frame it played out was speech."""

    at_ms: int
    speech: bool


class MediaListener(Protocol):
    """Told about every tick and about the end of the session; must return at once (O(1) work)."""

    def on_tick(self, tick: AgentTick) -> None: ...

    def on_media_end(self) -> None: ...


def _dumps(frame: Frame) -> str:
    return orjson.dumps(frame).decode()


class MediaSession:
    """The media WebSocket of one stream of ``call``."""

    def __init__(self, state: SimState, call: Call, stream: StreamState, events: CallMedia) -> None:
        self._state = state
        self._call = call
        self._stream = stream
        self._events = events
        self.fmt = stream_format(stream.params)
        self.agent_fmt = agent_format(stream.params)
        self._identity = StreamIdentity(
            stream_id=stream.stream_id,
            user_id=state.ids.uuid(),
            call_control_id=call.call_control_id,
            call_session_id=call.call_session_id,
            from_=call.from_,
            to=call.to,
            client_state=call.client_state,
        )
        self._playout = Playout(self.agent_fmt)
        self._caller = CallerQueue(self.fmt)
        self._dtmf: deque[str] = deque()
        self._outbox: list[Frame] = []
        self._sequence = 1
        self._chunk = 0
        self._held: Frame | None = None
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._stopping = False
        self._listeners: list[MediaListener] = []
        self._ended = False
        self._dtmf_queued = 0
        self.frames_sent = 0
        self.marks_echoed = 0
        self.received: Counter[str] = Counter()

    # Control.

    @property
    def connected(self) -> bool:
        """Connected and not stopping: caller audio can be queued."""
        return self._ws is not None and not self._stopping

    @property
    def caller_sent(self) -> int:
        """Caller audio bytes sent so far (compare with the offset :meth:`queue_caller` returns)."""
        return self._caller.sent

    @property
    def dtmf_sent(self) -> int:
        """DTMF digits sent so far (compare with the count :meth:`queue_dtmf` returns)."""
        return self._dtmf_queued - len(self._dtmf)

    def add_listener(self, listener: MediaListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: MediaListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def start(self) -> None:
        self._task = self._state.tasks.spawn(self._run(), name=f"telnyx-media-{self._stream.stream_id}")

    def stop(self) -> None:
        """Stop the stream: cancel a pending connect, else send ``stop`` and close with 1000."""
        if self._stopping:
            return
        self._stopping = True
        if self._ws is None and self._task is not None:
            self._task.cancel()
        else:
            self._stop.set()
        self._end()

    async def wait_closed(self) -> None:
        """Wait until the session has ended (the socket is closed)."""
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    def queue_caller(self, wire: bytes, utterance: tuple[int | None, str] | None = None) -> int:
        """Queue caller audio in the stream format; an ``(id, text)`` utterance gets a timeline entry (id
        None: a WAV file). Returns the offset :attr:`caller_sent` reaches once the audio has been sent."""
        entry: dict[str, Any] | None = None
        if utterance is not None:
            entry = {
                "utterance_id": utterance[0],
                "text": utterance[1],
                "start_ms": None,
                "end_ms": None,
                "first_agent_audio_ms": None,
                "latency_ms": None,
            }
            self._call.timeline_utterances.append(entry)
        return self._caller.push(wire, entry)

    def queue_dtmf(self, digits: str) -> int:
        """One ``dtmf`` event per digit, at successive ticks; returns the :attr:`dtmf_sent` count reached once
        these digits have been sent."""
        self._dtmf.extend(digits)
        self._dtmf_queued += len(digits)
        return self._dtmf_queued

    def inject(self, frame: Frame) -> None:
        """Send ``frame`` verbatim before the next caller media frame."""
        self._outbox.append(frame)

    def stats(self) -> dict[str, object]:
        """The session part of the call summary ``stream``."""
        return {
            "connected": self.connected,
            "frames_sent": self.frames_sent,
            "received": dict(self.received),
            "marks_echoed": self.marks_echoed,
        }

    # Connection.

    def _end(self) -> None:
        """Tell the listeners, once, that the session is over (stopped, closed by the agent, or failed)."""
        if self._ended:
            return
        self._ended = True
        for listener in tuple(self._listeners):
            listener.on_media_end()

    async def _run(self) -> None:
        try:
            await self._session()
        finally:
            self._end()

    async def _session(self) -> None:
        try:
            ws = await self._connect()
        except (OSError, TimeoutError, WebSocketException) as exc:
            _log.info("sim.telnyx_media_connect_failed", stream_id=self._stream.stream_id, error=repr(exc))
            self._events.stream_failed(self._call, self._stream, "connection_failed")
            return
        self._ws = ws
        try:
            await self._serve(ws)
        except* _PeerClosedError:
            self._events.stream_failed(self._call, self._stream, "connection_closed")
        finally:
            await ws.close()

    def _headers(self) -> dict[str, str]:
        url = str(self._stream.params["stream_url"])
        timestamp = int(self._state.clock.now().timestamp())
        headers = {
            SIGNATURE_HEADER: self._state.signing_key.sign(f"{timestamp}|{url}".encode()),
            TIMESTAMP_HEADER: str(timestamp),
        }
        token = self._stream.params["stream_auth_token"]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _connect(self) -> ClientConnection:
        name = f"telnyx-media-hold-{self._stream.stream_id}"
        async with hold_clock(self._state.clock, self._state.tasks, name=name):
            return await connect(
                str(self._stream.params["stream_url"]),
                additional_headers=self._headers(),
                open_timeout=_OPEN_TIMEOUT_S,
                close_timeout=_CLOSE_TIMEOUT_S,
                max_size=_MAX_MESSAGE_BYTES,
                compression=None,
                ping_interval=None,
                proxy=None,
            )

    async def _serve(self, ws: ClientConnection) -> None:
        self._align_tracks()
        await ws.send(_dumps(connected_frame()))
        await ws.send(_dumps(start_frame(self._identity, self.fmt)))
        self._events.media_connected(self._call, self)
        async with asyncio.TaskGroup() as group:
            loops = (group.create_task(self._receive_loop(ws)), group.create_task(self._tick_loop(ws)))
            await self._stop.wait()
            for loop in loops:
                loop.cancel()
        with contextlib.suppress(ConnectionClosed):
            await ws.send(_dumps(stop_frame(self._identity, self._next_sequence())))

    def _align_tracks(self) -> None:
        """Pad both call tracks with silence up to now: they stay aligned with the sim clock from answer."""
        call = self._call
        elapsed_ms = self._state.clock.virtual_ms() - (call.answered_ms or 0)
        size = elapsed_ms * TRACK_RATE * 2 // 1000
        for track in (call.audio.caller, call.audio.agent):
            if len(track) < size:
                track.extend(bytes(size - len(track)))

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # Receiving.

    async def _receive_loop(self, ws: ClientConnection) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for raw in ws:
                self._handle(raw)
        raise _PeerClosedError

    def _handle(self, raw: str | bytes) -> None:
        parsed = parse_agent_frame(raw, self.agent_fmt)
        match parsed:
            case MediaFault(code=code, detail=detail):
                self.received["invalid"] += 1
                self._outbox.append(error_frame(self._stream.stream_id, code, detail))
            case AgentMedia(audio=audio):
                self.received["media"] += 1
                self._playout.push(audio)
                self._note_agent_audio(audio)
            case AgentMark(name=name):
                self.received["mark"] += 1
                self._playout.mark(name)
            case AgentClear():
                self.received["clear"] += 1
                self._playout.clear()

    def _note_agent_audio(self, audio: bytes) -> None:
        """Record the first agent speech after each finished caller utterance (timeline latency)."""
        waiting = [
            entry
            for entry in self._call.timeline_utterances
            if entry["end_ms"] is not None and entry["first_agent_audio_ms"] is None
        ]
        if not waiting:
            return
        peak = int(np.max(np.abs(self.agent_fmt.to_pcm(audio).astype(np.int32))))
        if peak <= AGENT_SPEECH_LEVEL:
            return
        now = self._state.clock.virtual_ms()
        for entry in waiting:
            entry["first_agent_audio_ms"] = now
            entry["latency_ms"] = now - entry["end_ms"]

    # Sending.

    async def _tick_loop(self, ws: ClientConnection) -> None:
        clock = self._state.clock
        started = clock.monotonic()
        ticks = 0
        try:
            while True:
                await clock.sleep(max(0.0, started + ticks * _FRAME_S - clock.monotonic()))
                ticks += 1
                for frame in self._tick():
                    await ws.send(_dumps(frame))
        except ConnectionClosed as exc:
            raise _PeerClosedError from exc

    def _tick(self) -> list[Frame]:
        """One 20 ms step: the frames to send, in order."""
        stream_id = self._stream.stream_id
        played, marks = self._playout.tick()
        agent_pcm = self.agent_fmt.decode_pcm8k(played)
        self._call.audio.agent.extend(agent_pcm)
        frames = [mark_frame(stream_id, self._next_sequence(), name) for name in marks]
        self.marks_echoed += len(marks)
        frames.extend(self._outbox)
        self._outbox.clear()
        if self._dtmf:
            digit = self._dtmf.popleft()
            occurred_at = iso_z(self._state.clock.now())
            frames.append(dtmf_frame(stream_id, self._next_sequence(), digit, occurred_at))
            self._call.caller_dtmf.append({"digit": digit, "at": occurred_at})
        frames.extend(self._caller_frames())
        tick = AgentTick(self._state.clock.virtual_ms(), is_speech(agent_pcm))
        for listener in tuple(self._listeners):
            listener.on_tick(tick)
        return frames

    def _caller_frames(self) -> list[Frame]:
        """The next caller media frame; in jitter mode adjacent frames go out swapped (2, 1, 4, 3, ...)."""
        frame = self._caller.next_frame()
        now = self._state.clock.virtual_ms()
        for entry in frame.started:
            entry["start_ms"] = now
        for entry in frame.ended:
            entry["end_ms"] = now
        self._call.audio.caller.extend(self.fmt.decode_pcm8k(frame.payload))
        self._chunk += 1
        media = media_frame(self._stream.stream_id, self._next_sequence(), self._chunk, frame.payload)
        held, self._held = self._held, None
        if held is not None:
            out = [media, held]
        elif self._state.telnyx.config.media_jitter:
            self._held = media
            out = []
        else:
            out = [media]
        self.frames_sent += len(out)
        return out
