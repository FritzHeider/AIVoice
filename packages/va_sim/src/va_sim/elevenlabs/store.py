"""FakeElevenLabs state (specs/30 §3.3; Decision P03-T04): accepted keys, TTS request records, fault, sockets.

``api_keys`` empty (the default) accepts every non-empty key. Every TTS context (WebSocket) or REST request is
recorded as a :class:`TtsRequest`. One :class:`TtsFault` can be armed at a time; each firing consumes one of
its ``times``. Live TTS sockets register themselves so ``reset`` can close them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final, Literal, Protocol

__all__ = ["ElevenLabsStore", "EndReason", "FaultKind", "LiveSocket", "Transport", "TtsFault", "TtsRequest"]

type Transport = Literal["ws", "rest"]
type EndReason = Literal[
    "close_context", "close_socket", "inactivity", "disconnect", "fault", "reset", "completed"
]
type FaultKind = Literal["disconnect", "error", "delay"]

_REQUEST_PREFIX: Final = "ttsreq_"


@dataclass(slots=True)
class TtsRequest:
    """One TTS context (WebSocket) or request (REST) as served at ``GET /sim/elevenlabs/tts/requests``."""

    id: str
    transport: Transport
    voice_id: str
    model_id: str
    output_format: str
    context_id: str | None
    started_ms: int
    text: str = ""
    generated_text: str = ""
    characters: int = 0
    ended_ms: int | None = None
    interrupted: bool = False
    end_reason: EndReason | None = None

    def end(self, reason: EndReason, now_ms: int, *, interrupted: bool = False) -> None:
        """Mark the request ended (the first end wins); ``interrupted`` only ever turns the flag on."""
        if self.end_reason is not None:
            return
        self.end_reason = reason
        self.ended_ms = now_ms
        self.interrupted = self.interrupted or interrupted

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TtsFault:
    """An armed fault: fires ``remaining`` more times."""

    kind: FaultKind
    after_chunks: int
    delay_ms: int
    remaining: int

    def to_json(self) -> dict[str, object]:
        return asdict(self)


class LiveSocket(Protocol):
    """A connected TTS WebSocket that the simulator can close."""

    def close_for_reset(self) -> None: ...


class ElevenLabsStore:
    """Accepted API keys, TTS request records, the armed fault, and the live TTS sockets."""

    def __init__(self) -> None:
        self.api_keys: tuple[str, ...] = ()
        self.tts_requests: list[TtsRequest] = []
        self.fault: TtsFault | None = None
        self.sockets: set[LiveSocket] = set()

    def accepts(self, key: str | None) -> bool:
        """Whether ``key`` authenticates: any non-empty key unless an allow list is set."""
        if not key:
            return False
        return not self.api_keys or key in self.api_keys

    def new_request(  # noqa: PLR0913 - keyword-only record fields
        self,
        *,
        transport: Transport,
        voice_id: str,
        model_id: str,
        output_format: str,
        context_id: str | None,
        started_ms: int,
    ) -> TtsRequest:
        request = TtsRequest(
            id=f"{_REQUEST_PREFIX}{len(self.tts_requests) + 1}",
            transport=transport,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
            context_id=context_id,
            started_ms=started_ms,
        )
        self.tts_requests.append(request)
        return request

    def arm_fault(self, kind: FaultKind, *, after_chunks: int, delay_ms: int, times: int) -> None:
        self.fault = TtsFault(kind=kind, after_chunks=after_chunks, delay_ms=delay_ms, remaining=times)

    def take_fault(self, kinds: frozenset[FaultKind], chunk_index: int | None = None) -> TtsFault | None:
        """Fire the armed fault when its kind is in ``kinds`` (and, for chunks, at ``after_chunks``)."""
        fault = self.fault
        if fault is None or fault.kind not in kinds:
            return None
        if chunk_index is not None and chunk_index != fault.after_chunks:
            return None
        fault.remaining -= 1
        if fault.remaining == 0:
            self.fault = None
        return fault

    def reset(self) -> None:
        """Close every live socket (code 1012), forget records, keys, and the fault."""
        for socket in tuple(self.sockets):
            socket.close_for_reset()
        self.sockets.clear()
        self.tts_requests.clear()
        self.api_keys = ()
        self.fault = None
