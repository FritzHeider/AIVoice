"""Media features of a FakeTelnyx call: streaming, the scenario caller runner, recording, playback, and
answering machine detection (specs/30-testing-and-evals.md §3.2 and §3.5, Decision 2026-10-29 P03-T02b rules
8, 10, 11 and P03-T03c rule 1).

Recordings are always WAV (specs/06 §10.2 Decision: no MP3 encoder): 8 kHz PCM16, channel 0 the caller track
and channel 1 the agent track (``single``: their clipped sum), padded with silence to the recording's
sim-clock duration, and served at ``<base>/sim/recordings/<id>.wav``.
"""

from __future__ import annotations

import asyncio
from array import array
from collections.abc import Mapping
from typing import Final, Literal

from va_sim.clock import iso_z
from va_sim.telnyx.call_events import CallEvents, Outgoing
from va_sim.telnyx.caller_script import CallerScript
from va_sim.telnyx.calls import ActiveRecording, Call, Playback, StreamState
from va_sim.telnyx.media_session import MediaSession
from va_sim.wav import pcm16_wav

__all__ = [
    "AMD_DETECTION_S",
    "BEEP_EVENTS",
    "RECORDING_SAMPLE_RATE",
    "STANDARD_AMD_RESULT",
    "CallMedia",
    "mix_tracks",
]

RECORDING_SAMPLE_RATE: Final = 8000
AMD_DETECTION_S: Final = 1.8
_BYTES_PER_MS: Final = RECORDING_SAMPLE_RATE * 2 // 1000
_PCM16_MAX: Final = 32_767
_PCM16_MIN: Final = -32_768

# Standard detection reports human | machine | not_sure for the scenario's answered_by.
STANDARD_AMD_RESULT: Final[Mapping[str, str]] = {
    "human_residence": "human",
    "human_business": "human",
    "machine": "machine",
    "silence": "not_sure",
    "fax_detected": "not_sure",
}
# Detection mode → (detection event, greeting event or None when the mode does not report the beep).
BEEP_EVENTS: Final[Mapping[str, tuple[str, str | None]]] = {
    "premium": ("call.machine.premium.detection.ended", "call.machine.premium.greeting.ended"),
    "detect": ("call.machine.detection.ended", None),
    "detect_words": ("call.machine.detection.ended", None),
    "detect_beep": ("call.machine.detection.ended", "call.machine.greeting.ended"),
    "greeting_end": ("call.machine.detection.ended", "call.machine.greeting.ended"),
}


def mix_tracks(caller: bytes, agent: bytes) -> bytes:
    """The sample-wise sum of two PCM16 tracks, clipped; the shorter one is padded with silence."""
    length = max(len(caller), len(agent))
    left = array("h", caller.ljust(length, b"\x00"))
    right = array("h", agent.ljust(length, b"\x00"))
    mixed = array("h", (max(_PCM16_MIN, min(_PCM16_MAX, a + b)) for a, b in zip(left, right, strict=True)))
    return mixed.tobytes()


def _encode(channels: Literal["single", "dual"], caller: bytes, agent: bytes) -> bytes:
    """The recording WAV; CPU-bound (the mix walks every sample), so it runs in a worker thread."""
    tracks = [caller, agent] if channels == "dual" else [mix_tracks(caller, agent)]
    return pcm16_wav(tracks, RECORDING_SAMPLE_RATE)


class CallMedia(CallEvents):
    """Streams, recordings, playbacks, and AMD timers of call legs."""

    # Streaming.

    def start_stream(self, call: Call, params: dict[str, object]) -> None:
        """``streaming.started``, then a media session connects to ``stream_url`` in the background."""
        stream = StreamState(self._state.ids.uuid(), params)
        call.stream = stream
        self.emit(call, "streaming.started", stream_url=params["stream_url"])
        stream.session = MediaSession(self._state, call, stream, self)
        stream.session.start()

    def stop_stream(self, call: Call) -> Outgoing | None:
        """Close the stream; returns its ``streaming.stopped`` event (None when not streaming), sent once the
        media session has sent ``stop`` and closed the socket."""
        stream = call.stream
        if stream is None:
            return None
        call.stream = None
        session = stream.session
        if session is not None:
            session.stop()
        prepare = session.wait_closed if session is not None else None
        return self.event(
            call, "streaming.stopped", call.payload(stream_url=stream.params["stream_url"]), prepare
        )

    def stream_failed(self, call: Call, stream: StreamState, reason: str) -> None:
        """The media session could not connect or the agent closed it: ``streaming.failed``, no stream."""
        if call.stream is not stream:
            return
        call.stream = None
        self.emit(
            call,
            "streaming.failed",
            stream_id=stream.stream_id,
            stream_url=stream.params["stream_url"],
            failure_reason=reason,
        )

    # Scenario caller runner.

    def media_connected(self, call: Call, session: MediaSession) -> None:
        """A media session of ``call`` connected: start the leg's caller script on it (once per leg), and the
        script of a bridged transfer leg that has no stream of its own."""
        if call.script is None:
            self._start_script(call, session)
        if call.bridged_with is not None:
            self.run_transfer_script(call.bridged_with, call)

    def run_transfer_script(self, leg: Call, peer: Call) -> None:
        """Run transfer leg ``leg``'s script on ``peer``'s connected media session when ``leg`` has no stream:
        the platform hears the transfer target on the original call's stream."""
        if not leg.transfer_leg or leg.script is not None or leg.stream is not None:
            return
        session = peer.stream.session if peer.stream is not None else None
        if session is not None and session.connected:
            self._start_script(leg, session)

    def _start_script(self, call: Call, session: MediaSession) -> None:
        """Start ``call``'s scenario script on ``session``: greeting and ``steps`` for the caller, only
        ``transfer_target.steps`` for a transfer leg; nothing when there is nothing to run."""
        scenario = self._state.scenarios.get(call.scenario_id or "")
        if scenario is None:
            return
        if call.transfer_leg:
            target = scenario.transfer_target
            steps = () if target is None else target.steps
            if not steps:
                return
            script = CallerScript(self._state, call, "transfer_target", steps, None)
        else:
            if not scenario.steps and scenario.greeting_audio is None:
                return
            script = CallerScript(self._state, call, "caller", scenario.steps, scenario.greeting_audio)
        call.script = script
        script.start(session)

    # Playback.

    def start_playback(self, call: Call, media_url: str, *, overlay: bool) -> None:
        call.playbacks.append(Playback(media_url, overlay))
        self.emit(call, "call.playback.started", media_url=media_url, overlay=overlay)

    def stop_playbacks(
        self, call: Call, status: Literal["cancelled", "call_hangup"], *, overlay: bool | None, current: bool
    ) -> list[Outgoing]:
        """End matching playbacks (all, or the latest when ``current``); one ``call.playback.ended`` each."""
        matching = [p for p in call.playbacks if overlay is None or p.overlay == overlay]
        stopped = matching[-1:] if current else matching
        call.playbacks = [p for p in call.playbacks if all(p is not s for s in stopped)]
        return [
            self.event(
                call,
                "call.playback.ended",
                call.payload(media_url=p.media_url, overlay=p.overlay, status=status),
            )
            for p in stopped
        ]

    # Recording.

    def start_recording(
        self, call: Call, channels: Literal["single", "dual"], record_format: Literal["wav", "mp3"]
    ) -> None:
        call.recording = ActiveRecording(
            recording_id=self._state.ids.uuid(),
            channels=channels,
            format=record_format,
            started_at=self._state.clock.now(),
            started_ms=self._state.clock.virtual_ms(),
            caller_offset=len(call.audio.caller),
            agent_offset=len(call.audio.agent),
        )

    def stop_recording(self, call: Call) -> Outgoing | None:
        """Finish the recording; returns ``call.recording.saved`` whose ``prepare`` stores the WAV."""
        recording = call.recording
        if recording is None:
            return None
        call.recording = None
        call.recordings.append(recording.recording_id)
        duration = (self._state.clock.virtual_ms() - recording.started_ms) * _BYTES_PER_MS
        caller = bytes(call.audio.caller[recording.caller_offset :]).ljust(duration, b"\x00")
        agent = bytes(call.audio.agent[recording.agent_offset :]).ljust(duration, b"\x00")
        url = f"{call.base_url}/sim/recordings/{recording.recording_id}.wav"
        urls = {"wav": url, "mp3": None}

        async def store() -> None:
            wav = await asyncio.to_thread(_encode, recording.channels, caller, agent)
            self._state.store_recording(recording.recording_id, wav)

        payload = call.payload(
            recording_id=recording.recording_id,
            channels=recording.channels,
            format=recording.format,
            recording_started_at=iso_z(recording.started_at),
            recording_ended_at=iso_z(self._state.clock.now()),
            recording_urls=urls,
            public_recording_urls=dict(urls),
        )
        return self.event(call, "call.recording.saved", payload, prepare=store)

    # Answering machine detection.

    def schedule_amd(self, call: Call) -> None:
        """Detection events for an answered outgoing leg, driven by its scenario outcome."""
        events = BEEP_EVENTS.get(call.amd)
        if events is None:
            return
        detection, greeting = events
        answered_by = call.outcome.answered_by
        result = answered_by if call.amd == "premium" else STANDARD_AMD_RESULT[answered_by]
        self.later(call, AMD_DETECTION_S, lambda: self.emit(call, detection, result=result), name="amd")
        beep_at = call.outcome.beep_at_s
        if greeting is not None and beep_at is not None:
            self.later(call, beep_at, lambda: self.emit(call, greeting, result="beep_detected"), name="beep")
