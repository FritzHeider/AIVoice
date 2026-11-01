"""Background tracks for the outbound mix (specs/07-media-pipeline.md §10.2).

The procedural loops under ``assets/audio`` are stored at their playback level (``static`` -40 dBFS,
``office``/``cafe``/``restaurant`` -22 dBFS RMS), so the mixer applies unit gain. 8 kHz legs read the μ-law
``.ulaw`` file, 16 kHz legs the little-endian ``.pcm16k`` file. Decoded samples are cached per (track, rate)
and shared read-only; every :meth:`BackgroundLibrary.loop` call returns a fresh :class:`BackgroundLoop` with
its own position, so calls never disturb each other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

from va_voice.audio import AudioFormat, pcm16le_to_pcm, ulaw_decode

__all__ = [
    "MIXED_TRACKS",
    "STATIC_TRACK",
    "BackgroundAssetError",
    "BackgroundLibrary",
    "BackgroundLoop",
    "BackgroundTrack",
    "resolve_background",
]

type BackgroundTrack = Literal["office", "cafe", "restaurant", "none", "phone_static"] | None
"""Agent/call ``background_track`` values; ``phone_static`` is the org-settings spelling of the default."""

STATIC_TRACK: Final = "static"
"""Asset played for ``null`` (the default) and ``phone_static``."""

MIXED_TRACKS: Final = frozenset({"office", "cafe", "restaurant"})
"""Tracks named after their asset."""

_NO_TRACK: Final = "none"
_DEFAULT_ALIASES: Final = frozenset({"phone_static"})
_ULAW_RATE: Final = 8000
_SUFFIX_BY_RATE: Final = {_ULAW_RATE: ".ulaw", 16000: ".pcm16k"}


class BackgroundAssetError(Exception):
    """A background asset is missing, empty, or not whole samples."""


def resolve_background(track: str | None) -> str | None:
    """The asset name for a ``background_track`` value (``None`` for ``"none"``; unknown values raise)."""
    if track is None or track in _DEFAULT_ALIASES:
        return STATIC_TRACK
    if track == _NO_TRACK:
        return None
    if track in MIXED_TRACKS:
        return track
    message = f"unknown background_track {track!r}"
    raise ValueError(message)


class BackgroundLoop:
    """Frames of a looping track: ``next_frame`` walks the samples and wraps at the seam."""

    __slots__ = ("_frame_samples", "_position", "_samples")

    def __init__(self, samples: NDArray[np.int16], frame_samples: int) -> None:
        if samples.size == 0:
            message = "a background loop needs samples"
            raise ValueError(message)
        if frame_samples <= 0:
            message = f"frame_samples must be > 0, got {frame_samples}"
            raise ValueError(message)
        self._samples = samples
        self._frame_samples = frame_samples
        self._position = 0

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    @property
    def position(self) -> int:
        """Index of the next sample ``next_frame`` returns."""
        return self._position

    @property
    def samples(self) -> NDArray[np.int16]:
        return self._samples

    def next_frame(self) -> NDArray[np.int16]:
        """The next ``frame_samples`` samples (a read-only view, or a copy across the seam)."""
        start = self._position
        end = start + self._frame_samples
        size = self._samples.size
        if end <= size:
            frame = self._samples[start:end]
        else:
            frame = np.take(self._samples, np.arange(start, end), mode="wrap")
        self._position = end % size
        return frame

    def reset(self) -> None:
        """Back to the start of the loop."""
        self._position = 0


def _read_asset(path: Path, sample_rate: int) -> NDArray[np.int16]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        message = f"background asset {path} is unreadable: {exc.strerror}"
        raise BackgroundAssetError(message) from exc
    if not data:
        message = f"background asset {path} is empty"
        raise BackgroundAssetError(message)
    if sample_rate == _ULAW_RATE:
        samples = ulaw_decode(data)
    elif len(data) % 2:
        message = f"background asset {path} has an odd byte count"
        raise BackgroundAssetError(message)
    else:
        samples = pcm16le_to_pcm(data)
    samples.flags.writeable = False
    return samples


class BackgroundLibrary:
    """Loads background loops from an assets directory (``assets/audio`` in the repository)."""

    __slots__ = ("_assets_dir", "_cache")

    def __init__(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir
        self._cache: dict[tuple[str, int], NDArray[np.int16]] = {}

    async def loop(self, track: str | None, fmt: AudioFormat) -> BackgroundLoop | None:
        """A fresh loop of ``track`` framed for ``fmt`` (``None`` for ``"none"``)."""
        name = resolve_background(track)
        if name is None:
            return None
        return BackgroundLoop(await self.samples(name, fmt.sample_rate), fmt.frame_samples)

    async def samples(self, name: str, sample_rate: int) -> NDArray[np.int16]:
        """The decoded, read-only samples of asset ``name`` at ``sample_rate`` (cached)."""
        suffix = _SUFFIX_BY_RATE.get(sample_rate)
        if suffix is None:
            message = f"no background assets at {sample_rate} Hz"
            raise ValueError(message)
        key = (name, sample_rate)
        cached = self._cache.get(key)
        if cached is None:
            cached = await asyncio.to_thread(_read_asset, self._assets_dir / f"{name}{suffix}", sample_rate)
            self._cache[key] = cached
        return cached
