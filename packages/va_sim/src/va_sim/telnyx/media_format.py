"""Audio formats of FakeTelnyx media streams (specs/30-testing-and-evals.md §3.2 media WS client, Decision
2026-10-29 P03-T03b rule 3).

The simulator streams caller audio as PCMU 8 kHz or L16 (big-endian) at 8 or 16 kHz and parses agent audio
in the bidirectional codec, which defaults to the stream encoding. Other Telnyx codecs (PCMA, G722, OPUS,
AMR-WB), other bidirectional rates, and the ``mp3`` bidirectional mode are rejected with ``10015`` before the
command changes any state. Call tracks are PCM16 little-endian at 8 kHz, so 16 kHz audio is decimated by
averaging sample pairs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray
from starlette.responses import JSONResponse

from va_sim.audio.codec import ULAW_SILENCE, l16be_to_pcm, pcm_to_l16be, ulaw_decode, ulaw_encode
from va_sim.telnyx.call_models import StreamFields
from va_sim.telnyx.errors import invalid

__all__ = [
    "FRAME_MS",
    "TRACK_RATE",
    "MediaFormat",
    "agent_format",
    "check_stream_params",
    "stream_format",
]

type Encoding = Literal["PCMU", "L16"]
type SampleRate = Literal[8000, 16000]

FRAME_MS: Final = 20
TRACK_RATE: Final = 8000
_SUPPORTED_CODECS: Final = frozenset({"PCMU", "L16", "default"})
_SUPPORTED_RATES: Final = frozenset({8000, 16000})
_L16_DEFAULT_RATE: Final[SampleRate] = 16000


@dataclass(frozen=True, slots=True)
class MediaFormat:
    """One direction of a media stream: its encoding and sample rate (mono)."""

    encoding: Encoding
    sample_rate: SampleRate

    @property
    def sample_bytes(self) -> int:
        return 1 if self.encoding == "PCMU" else 2

    @property
    def bytes_per_ms(self) -> int:
        return self.sample_rate // 1000 * self.sample_bytes

    @property
    def frame_bytes(self) -> int:
        """Bytes in one 20 ms frame: 160 (PCMU), 320 (L16 8 kHz), or 640 (L16 16 kHz)."""
        return FRAME_MS * self.bytes_per_ms

    def silence(self, size: int) -> bytes:
        """``size`` bytes of silence in this encoding."""
        return bytes([ULAW_SILENCE]) * size if self.encoding == "PCMU" else bytes(size)

    def silence_frame(self) -> bytes:
        return self.silence(self.frame_bytes)

    def to_pcm(self, wire: bytes) -> NDArray[np.int16]:
        """Samples at the native rate."""
        return ulaw_decode(wire) if self.encoding == "PCMU" else l16be_to_pcm(wire)

    def encode(self, pcm: NDArray[np.int16]) -> bytes:
        """Wire bytes for samples at the native rate: μ-law (PCMU) or big-endian int16 (L16)."""
        return ulaw_encode(pcm) if self.encoding == "PCMU" else pcm_to_l16be(pcm)

    def decode_pcm8k(self, wire: bytes) -> bytes:
        """PCM16 little-endian at 8 kHz for the call tracks (16 kHz: the mean of each sample pair)."""
        pcm = self.to_pcm(wire)
        if self.sample_rate != TRACK_RATE:
            pairs = pcm[: len(pcm) // 2 * 2].astype(np.int32).reshape(-1, 2)
            pcm = (pairs.sum(axis=1) // 2).astype(np.int16)
        return bytes(pcm.astype("<i2").tobytes())

    def media_format(self) -> dict[str, object]:
        """The ``start.media_format`` object."""
        return {"encoding": self.encoding, "sample_rate": self.sample_rate, "channels": 1}


def _rate(rate: object, default: SampleRate) -> SampleRate:
    if rate == TRACK_RATE:
        return 8000
    if rate == _L16_DEFAULT_RATE:
        return 16000
    return default


def _format(encoding: str, rate: object, default_rate: SampleRate) -> MediaFormat:
    if encoding == "L16":
        return MediaFormat("L16", _rate(rate, default_rate))
    return MediaFormat("PCMU", 8000)


def stream_format(params: Mapping[str, object]) -> MediaFormat:
    """The caller audio format of a stream: ``stream_codec`` (PCMU for ``default``) at the bidirectional
    rate when L16 (16 kHz unless that rate is 8 kHz)."""
    return _format(
        str(params["stream_codec"]), params["stream_bidirectional_sampling_rate"], _L16_DEFAULT_RATE
    )


def agent_format(params: Mapping[str, object]) -> MediaFormat:
    """The agent audio format: ``stream_bidirectional_codec`` (default: the stream encoding) at the
    bidirectional rate, else the stream rate."""
    stream = stream_format(params)
    codec = params["stream_bidirectional_codec"] or stream.encoding
    return _format(str(codec), params["stream_bidirectional_sampling_rate"], stream.sample_rate)


def check_stream_params(body: StreamFields) -> JSONResponse | None:
    """``10015`` when a stream asks for a codec, rate, or mode the simulator does not emulate."""
    if body.stream_url is None:
        return None
    if body.stream_codec not in _SUPPORTED_CODECS:
        return invalid(f"The simulator streams PCMU or L16, not {body.stream_codec}.", "/stream_codec")
    codec = body.stream_bidirectional_codec
    if codec is not None and codec not in _SUPPORTED_CODECS:
        return invalid(
            f"The simulator accepts PCMU or L16 agent audio, not {codec}.", "/stream_bidirectional_codec"
        )
    rate = body.stream_bidirectional_sampling_rate
    if rate is not None and rate not in _SUPPORTED_RATES:
        return invalid(
            f"The simulator streams at 8000 or 16000 Hz, not {rate}.", "/stream_bidirectional_sampling_rate"
        )
    if body.stream_bidirectional_mode == "mp3":
        return invalid(
            "The simulator emulates only the rtp bidirectional mode.", "/stream_bidirectional_mode"
        )
    return None
