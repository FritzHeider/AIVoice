"""The REST phrase cache (specs/09-tts-elevenlabs.md §9; specs/03 §11 ``tts:cache:*``; Decision 2026-11-01
P04-T11).

Fixed phrases (fillers, the templated first sentence, tool ``speech``, reprompts, transfer lines) are rendered
once through ElevenLabs ``with-timestamps`` and kept in Redis under
``tts:cache:{voice_id}:{sha256(model|format|lang|settings_json|text)}`` as
``{"audio_b64", "alignment", "duration_ms"}`` for 30 days (per-call entries: 1 hour). Values over 2 MB are
returned but not stored. Redis failures fail open (rendered without the cache, logged
``tts.phrase_cache_unavailable``); a REST failure raises :class:`PhraseUnavailableError` so the caller can
speak the phrase through the live socket instead.
Concurrent misses of one key in one process share one render.

va_voice never imports va_elevenlabs: the renderer is the structural :class:`TimestampedSynthesizer`, which
``va_elevenlabs.ElevenLabsRest`` satisfies as is.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_domain.voices import VoiceSettings
from va_voice.audio.formats import PCM16LE_16000, ULAW_8000, AudioFormat

__all__ = [
    "KEY_PREFIX",
    "MAX_VALUE_BYTES",
    "PER_CALL_TTL_S",
    "SHARED_TTL_S",
    "STANDARD_FILLERS",
    "SUPPORTED_FORMATS",
    "CachedPhrase",
    "PhraseAlignment",
    "PhraseCache",
    "PhraseCacheMetrics",
    "PhraseUnavailableError",
    "PhraseVoice",
    "RestAlignmentLike",
    "TimestampedAudioLike",
    "TimestampedSynthesizer",
    "decode_entry",
    "encode_entry",
    "phrase_cache_key",
]

KEY_PREFIX: Final = "tts:cache:"
SHARED_TTL_S: Final = 30 * 86_400
PER_CALL_TTL_S: Final = 3_600
MAX_VALUE_BYTES: Final = 2 * 1024 * 1024
STANDARD_FILLERS: Final = ("Sure.", "Got it.", "One moment.", "Let me check that for you.")
SUPPORTED_FORMATS: Final = {fmt.name: fmt for fmt in (ULAW_8000, PCM16LE_16000)}

_log = get_logger(__name__)


class RestAlignmentLike(Protocol):
    """Character alignment of a ``with-timestamps`` response in absolute seconds (``RestAlignment``)."""

    @property
    def characters(self) -> Sequence[str]: ...

    @property
    def character_start_times_seconds(self) -> Sequence[float]: ...

    @property
    def character_end_times_seconds(self) -> Sequence[float]: ...


class TimestampedAudioLike(Protocol):
    """A ``with-timestamps`` synthesis (``TtsTimestamped``)."""

    @property
    def audio(self) -> bytes: ...

    @property
    def alignment(self) -> RestAlignmentLike | None: ...


class TimestampedSynthesizer(Protocol):
    """``va_elevenlabs.ElevenLabsRest.tts_with_timestamps``."""

    async def tts_with_timestamps(  # noqa: PLR0913 - mirrors the vendor request fields
        self,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        output_format: str,
        voice_settings: VoiceSettings | None = ...,
        language_code: str | None = ...,
        apply_text_normalization: Literal["auto", "on", "off"] = ...,
    ) -> TimestampedAudioLike: ...


@dataclass(frozen=True, slots=True)
class PhraseVoice:
    """The voice a phrase is rendered with; every field is part of the cache key."""

    voice_id: str
    model_id: str
    settings: VoiceSettings
    language_code: str | None = None


@dataclass(frozen=True, slots=True)
class PhraseAlignment:
    """Integer-ms character timings of a whole phrase (a ``ChunkAlignmentLike`` of one chunk)."""

    chars: tuple[str, ...]
    char_start_ms: tuple[int, ...]
    char_duration_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CachedPhrase:
    """A rendered phrase; ``cached`` is true when Redis served it."""

    text: str
    fmt: AudioFormat
    audio: bytes
    alignment: PhraseAlignment | None
    duration_ms: int
    cached: bool


class PhraseUnavailableError(Exception):
    """The phrase could not be rendered over REST; speak it through the live socket instead."""

    def __init__(self, text: str, cause: Exception) -> None:
        super().__init__(f"phrase synthesis failed: {type(cause).__name__}")
        self.text = text
        self.__cause__ = cause


class PhraseCacheMetrics:
    """``va_tts_cache_hits_total`` / ``va_tts_cache_misses_total`` of the media-worker catalog."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._hits = metrics.counter("va_tts_cache_hits_total")
        self._misses = metrics.counter("va_tts_cache_misses_total")

    def hit(self) -> None:
        self._hits.inc()

    def miss(self) -> None:
        self._misses.inc()


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _ms(seconds: float) -> int:
    """Seconds to integer ms, rounding half up."""
    return _round_half_up(seconds * 1000)


def _format(fmt: AudioFormat) -> AudioFormat:
    if SUPPORTED_FORMATS.get(fmt.name) != fmt:
        msg = f"unsupported phrase format {fmt.name!r}; expected one of {sorted(SUPPORTED_FORMATS)}"
        raise ValueError(msg)
    return fmt


def phrase_cache_key(voice: PhraseVoice, text: str, fmt: AudioFormat) -> str:
    """``tts:cache:{voice_id}:{sha256(model|format|lang|settings_json|text)}`` (hex digest).

    ``settings_json`` is every :class:`VoiceSettings` field (defaults included) as compact JSON with sorted
    keys, so a partial settings object and its explicit-default twin share a key; a missing
    language is ``""``.
    """
    settings_json = orjson.dumps(voice.settings.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    material = "|".join(
        (voice.model_id, fmt.name, voice.language_code or "", settings_json.decode("utf-8"), text)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}{voice.voice_id}:{digest}"


def _alignment_of(rest: RestAlignmentLike | None) -> PhraseAlignment | None:
    if rest is None:
        return None
    starts = tuple(_ms(start) for start in rest.character_start_times_seconds)
    ends = (_ms(end) for end in rest.character_end_times_seconds)
    durations = tuple(max(0, end - start) for start, end in zip(starts, ends, strict=True))
    return PhraseAlignment(tuple(rest.characters), starts, durations)


def encode_entry(phrase: CachedPhrase) -> bytes:
    """The Redis value of ``phrase``: ``{"audio_b64", "alignment", "duration_ms"}``."""
    alignment = phrase.alignment
    return orjson.dumps(
        {
            "audio_b64": base64.b64encode(phrase.audio).decode("ascii"),
            "alignment": None
            if alignment is None
            else {
                "chars": list(alignment.chars),
                "char_start_ms": list(alignment.char_start_ms),
                "char_duration_ms": list(alignment.char_duration_ms),
            },
            "duration_ms": phrase.duration_ms,
        }
    )


def _ints(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list) or not all(type(item) is int and item >= 0 for item in value):
        return None
    return tuple(value)


def _decode_alignment(value: object) -> PhraseAlignment | Literal[False] | None:
    """The stored alignment, ``None`` when stored as null, ``False`` when malformed."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return False
    chars = value.get("chars")
    starts = _ints(value.get("char_start_ms"))
    durations = _ints(value.get("char_duration_ms"))
    if not isinstance(chars, list) or not all(isinstance(char, str) for char in chars):
        return False
    if starts is None or durations is None or not len(chars) == len(starts) == len(durations):
        return False
    return PhraseAlignment(tuple(chars), starts, durations)


def decode_entry(raw: bytes | str, text: str, fmt: AudioFormat) -> CachedPhrase | None:
    """The phrase stored in ``raw`` (``None`` when the value is malformed)."""
    try:
        document: Any = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    encoded = document.get("audio_b64")
    duration_ms = document.get("duration_ms")
    if not isinstance(encoded, str) or type(duration_ms) is not int or duration_ms < 0:
        return None
    try:
        audio = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None
    alignment = _decode_alignment(document.get("alignment"))
    if alignment is False:
        return None
    return CachedPhrase(text, fmt, audio, alignment, duration_ms, cached=True)


class PhraseCache:
    """Serves phrases from Redis, rendering misses through ``synth`` (one render per key at a time)."""

    def __init__(
        self,
        synth: TimestampedSynthesizer,
        redis: Redis,
        *,
        metrics: PhraseCacheMetrics | None = None,
    ) -> None:
        self._synth = synth
        self._redis = redis
        self._metrics = metrics
        self._flights: dict[str, asyncio.Task[CachedPhrase]] = {}

    async def get_or_render(
        self, voice: PhraseVoice, text: str, fmt: AudioFormat, *, ttl_s: int = SHARED_TTL_S
    ) -> CachedPhrase:
        """The phrase from Redis, else rendered over REST and stored for ``ttl_s`` seconds.

        Raises :class:`PhraseUnavailableError` when the REST render fails (nothing is stored).
        """
        if not text.strip():
            msg = "phrase text is empty"
            raise ValueError(msg)
        if ttl_s <= 0:
            msg = f"ttl_s must be positive, got {ttl_s}"
            raise ValueError(msg)
        _format(fmt)
        key = phrase_cache_key(voice, text, fmt)
        flight = self._flights.get(key)
        if flight is None:
            cached = await self._read(key, text, fmt)
            if cached is not None:
                self._count(hit=True)
                return cached
            flight = self._flights.get(key) or self._start(key, voice, text, fmt, ttl_s)
        self._count(hit=False)
        return await asyncio.shield(flight)

    async def prefetch(
        self,
        voice: PhraseVoice,
        fmt: AudioFormat,
        texts: Iterable[str] = STANDARD_FILLERS,
        *,
        ttl_s: int = SHARED_TTL_S,
    ) -> dict[str, CachedPhrase | None]:
        """Ensure ``texts`` are cached; a phrase that cannot be rendered maps to ``None`` (logged)."""
        unique = list(dict.fromkeys(texts))
        results = await asyncio.gather(
            *(self.get_or_render(voice, text, fmt, ttl_s=ttl_s) for text in unique), return_exceptions=True
        )
        phrases: dict[str, CachedPhrase | None] = {}
        for text, result in zip(unique, results, strict=True):
            if isinstance(result, PhraseUnavailableError):
                _log.warning(
                    "tts.phrase_prefetch_failed",
                    entry=phrase_cache_key(voice, text, fmt),
                    error=type(result.__cause__).__name__,
                )
                phrases[text] = None
            elif isinstance(result, BaseException):
                raise result
            else:
                phrases[text] = result
        return phrases

    async def aclose(self) -> None:
        """Cancel renders still in flight (their waiters see ``CancelledError``)."""
        flights = list(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        self._flights.clear()

    def _count(self, *, hit: bool) -> None:
        if self._metrics is None:
            return
        if hit:
            self._metrics.hit()
        else:
            self._metrics.miss()

    def _start(
        self, key: str, voice: PhraseVoice, text: str, fmt: AudioFormat, ttl_s: int
    ) -> asyncio.Task[CachedPhrase]:
        flight = asyncio.create_task(self._render(key, voice, text, fmt, ttl_s), name=f"phrase-render:{key}")
        self._flights[key] = flight

        def done(task: asyncio.Task[CachedPhrase]) -> None:
            # A flight stays registered until it is done, so no newer flight of ``key`` can be replaced here.
            self._flights.pop(key, None)
            if not task.cancelled():
                task.exception()  # retrieved here, so a flight whose waiters all left logs nothing

        flight.add_done_callback(done)
        return flight

    async def _render(
        self, key: str, voice: PhraseVoice, text: str, fmt: AudioFormat, ttl_s: int
    ) -> CachedPhrase:
        try:
            result = await self._synth.tts_with_timestamps(
                voice.voice_id,
                text,
                model_id=voice.model_id,
                output_format=fmt.name,
                voice_settings=voice.settings,
                language_code=voice.language_code,
                apply_text_normalization="auto",
            )
            alignment = _alignment_of(result.alignment)
        except Exception as exc:
            raise PhraseUnavailableError(text, exc) from exc
        audio = result.audio
        duration_ms = _round_half_up(fmt.duration_ms(len(audio)))
        phrase = CachedPhrase(text, fmt, audio, alignment, duration_ms, cached=False)
        await self._write(key, phrase, ttl_s)
        return phrase

    async def _read(self, key: str, text: str, fmt: AudioFormat) -> CachedPhrase | None:
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError) as exc:
            _log.warning("tts.phrase_cache_unavailable", operation="get", entry=key, error=type(exc).__name__)
            return None
        if raw is None:
            return None
        phrase = decode_entry(raw, text, fmt)
        if phrase is None:
            _log.warning("tts.phrase_cache_malformed", entry=key)
        return phrase

    async def _write(self, key: str, phrase: CachedPhrase, ttl_s: int) -> None:
        value = encode_entry(phrase)
        if len(value) > MAX_VALUE_BYTES:
            _log.warning("tts.phrase_cache_oversize", entry=key, bytes=len(value), limit=MAX_VALUE_BYTES)
            return
        try:
            await self._redis.set(key, value, ex=ttl_s)
        except (RedisError, OSError) as exc:
            _log.warning("tts.phrase_cache_unavailable", operation="set", entry=key, error=type(exc).__name__)
