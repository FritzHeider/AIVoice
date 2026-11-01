"""Post-call batch transcription (specs/08-stt-elevenlabs.md §8; Decision 2026-11-01 P03-T12 rules 5, 6).

``POST /v1/speech-to-text`` multipart (always multipart, also for a URL source), 300 s timeout, two retries
after 5 s and 20 s on 429/5xx/transport failures. The three response shapes (single channel, ``separate``
transcripts, ``combined``) normalise into one :class:`BatchTranscript`; ``separate`` channels are merged by
``(start, channel_index, original order)``. :func:`group_utterances` turns the words into speaker turns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from va_elevenlabs.rest_errors import ElevenLabsProtocolError
from va_elevenlabs.rest_retry import BATCH_RETRY
from va_elevenlabs.rest_transport import ElevenLabsTransport, FormPart, RestOperation, RestResponse

__all__ = [
    "BATCH_TRANSCRIBE",
    "MAX_BATCH_KEYTERMS",
    "UTTERANCE_GAP_S",
    "AudioUpload",
    "BatchEntity",
    "BatchOptions",
    "BatchSttCommands",
    "BatchTranscript",
    "BatchWord",
    "SourceUrl",
    "Utterance",
    "batch_form",
    "group_utterances",
    "parse_transcript",
]

type OutputStyle = Literal["separate", "combined"]
type Granularity = Literal["none", "word", "character"]
type Role = Literal["user", "assistant"]

BATCH_TRANSCRIBE: Final = RestOperation("batch_transcribe", "POST", 300.0, BATCH_RETRY)
MAX_BATCH_KEYTERMS: Final = 1000
UTTERANCE_GAP_S: Final = 0.7
_ROLE_BY_SPEAKER: Final[Mapping[str, Role]] = {"speaker_0": "user", "speaker_1": "assistant"}
_ROLE_BY_CHANNEL: Final[Mapping[int, Role]] = {0: "user", 1: "assistant"}
_REDACTION_ENTITIES: Final = ("pii", "pci")
_OPERATION: Final = BATCH_TRANSCRIBE.name


def _flag(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True, slots=True)
class AudioUpload:
    """Audio bytes uploaded as the ``file`` part."""

    filename: str
    data: bytes = field(repr=False)
    content_type: str = "audio/wav"


@dataclass(frozen=True, slots=True)
class SourceUrl:
    """Audio ElevenLabs fetches itself (``cloud_storage_url``, e.g. a presigned S3 URL)."""

    url: str


@dataclass(frozen=True, slots=True)
class BatchOptions:
    """The batch request fields (specs/08 §8)."""

    model_id: str
    use_multi_channel: bool = False
    multichannel_output_style: OutputStyle = "separate"
    timestamps_granularity: Granularity = "word"
    tag_audio_events: bool = False
    diarize: bool = False
    num_speakers: int | None = None
    detect_speaker_roles: bool = False
    language_code: str | None = None
    keyterms: tuple[str, ...] = ()
    entity_detection: tuple[str, ...] = ()
    zero_retention: bool = False

    def __post_init__(self) -> None:
        if len(self.keyterms) > MAX_BATCH_KEYTERMS:
            msg = f"at most {MAX_BATCH_KEYTERMS} keyterms are allowed, got {len(self.keyterms)}"
            raise ValueError(msg)

    @classmethod
    def for_recording(  # noqa: PLR0913 - the recording facts, keyword-only
        cls,
        model_id: str,
        *,
        stereo: bool = True,
        language_code: str | None = None,
        keyterms: Iterable[str] = (),
        redact: bool = False,
        zero_retention: bool = False,
    ) -> BatchOptions:
        """specs/08 §8 options for a call recording. Stereo (channel 0 caller, 1 agent): multi-channel
        ``combined``, or ``separate`` when ``redact`` asks for ``pii``/``pci`` entities (ElevenLabs rejects
        entities with ``combined``). Mono: ``diarize``, two speakers, speaker roles."""
        entities = _REDACTION_ENTITIES if redact else ()
        common: dict[str, Any] = {
            "model_id": model_id,
            "timestamps_granularity": "word",
            "tag_audio_events": False,
            "language_code": language_code,
            "keyterms": tuple(keyterms),
            "entity_detection": entities,
            "zero_retention": zero_retention,
        }
        if stereo:
            style: OutputStyle = "separate" if entities else "combined"
            return cls(**common, use_multi_channel=True, multichannel_output_style=style)
        return cls(**common, diarize=True, num_speakers=2, detect_speaker_roles=True)


def batch_form(source: AudioUpload | SourceUrl, options: BatchOptions) -> list[FormPart]:
    """The multipart parts: options first (keyterms and entities repeated), then the source."""
    parts: list[FormPart] = [
        ("model_id", (None, options.model_id)),
        ("use_multi_channel", (None, _flag(options.use_multi_channel))),
        ("timestamps_granularity", (None, options.timestamps_granularity)),
        ("tag_audio_events", (None, _flag(options.tag_audio_events))),
        ("diarize", (None, _flag(options.diarize))),
    ]
    if options.use_multi_channel:
        parts.append(("multichannel_output_style", (None, options.multichannel_output_style)))
    if options.num_speakers is not None:
        parts.append(("num_speakers", (None, str(options.num_speakers))))
    if options.detect_speaker_roles:
        parts.append(("detect_speaker_roles", (None, "true")))
    if options.language_code is not None:
        parts.append(("language_code", (None, options.language_code)))
    parts.extend(("keyterms", (None, term)) for term in options.keyterms)
    parts.extend(("entity_detection", (None, category)) for category in options.entity_detection)
    if isinstance(source, SourceUrl):
        parts.append(("cloud_storage_url", (None, source.url)))
    else:
        parts.append(("file", (source.filename, source.data, source.content_type)))
    return parts


@dataclass(frozen=True, slots=True)
class BatchWord:
    """One ``words[]`` entry (``type`` is ``word``, ``spacing``, or ``audio_event``)."""

    text: str
    start: float
    end: float
    type: str
    speaker_id: str | None
    channel_index: int | None
    logprob: float | None


@dataclass(frozen=True, slots=True)
class BatchEntity:
    """A detected entity; offsets index the text of its channel (``channel_index`` set for ``separate``)."""

    text: str
    entity_type: str
    start_char: int
    end_char: int
    channel_index: int | None


@dataclass(frozen=True, slots=True)
class BatchTranscript:
    language_code: str | None
    text: str
    words: tuple[BatchWord, ...]
    entities: tuple[BatchEntity, ...]
    transcription_id: str | None
    audio_duration_secs: float | None


@dataclass(frozen=True, slots=True)
class Utterance:
    role: Role
    text: str
    start: float
    end: float
    words: tuple[BatchWord, ...]


def _number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ElevenLabsProtocolError(_OPERATION, f"words[].{key} is not a number")
    return float(value)


def _word(raw: object, channel: int | None) -> BatchWord:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
        raise ElevenLabsProtocolError(_OPERATION, "words[] entry without text")
    index = raw.get("channel_index", channel)
    logprob = raw.get("logprob")
    speaker = raw.get("speaker_id")
    return BatchWord(
        text=raw["text"],
        start=_number(raw, "start"),
        end=_number(raw, "end"),
        type=str(raw.get("type", "word")),
        speaker_id=speaker if isinstance(speaker, str) else None,
        channel_index=index if isinstance(index, int) else None,
        logprob=float(logprob) if isinstance(logprob, int | float) else None,
    )


def _words(body: Mapping[str, Any], channel: int | None) -> list[BatchWord]:
    raw = body.get("words", [])
    if not isinstance(raw, list):
        raise ElevenLabsProtocolError(_OPERATION, "words is not a list")
    return [_word(item, channel) for item in raw]


def _entities(body: Mapping[str, Any], channel: int | None) -> list[BatchEntity]:
    raw = body.get("entities")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ElevenLabsProtocolError(_OPERATION, "entities is not a list")
    entities: list[BatchEntity] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ElevenLabsProtocolError(_OPERATION, "entities[] entry is not an object")
        entities.append(
            BatchEntity(
                text=_entity_text(item, "text"),
                entity_type=_entity_text(item, "entity_type"),
                start_char=_entity_offset(item, "start_char"),
                end_char=_entity_offset(item, "end_char"),
                channel_index=channel,
            )
        )
    return entities


def _entity_text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ElevenLabsProtocolError(_OPERATION, f"entities[].{key} is not a string")
    return value


def _entity_offset(item: Mapping[str, Any], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ElevenLabsProtocolError(_OPERATION, f"entities[].{key} is not an integer")
    return value


def _optional_str(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    return value if isinstance(value, str) else None


def _optional_float(document: Mapping[str, Any], key: str) -> float | None:
    value = document.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _merged(transcripts: list[Any]) -> tuple[str | None, list[BatchWord], list[BatchEntity]]:
    """``separate`` transcripts as one ordered word list (start, channel, original order)."""
    keyed: list[tuple[float, int, int, BatchWord]] = []
    entities: list[BatchEntity] = []
    language: str | None = None
    for position, transcript in enumerate(transcripts):
        if not isinstance(transcript, Mapping):
            raise ElevenLabsProtocolError(_OPERATION, "transcripts[] entry is not an object")
        raw_channel = transcript.get("channel_index", position)
        channel = raw_channel if isinstance(raw_channel, int) else position
        language = language or _optional_str(transcript, "language_code")
        keyed.extend(
            (word.start, channel, order, word) for order, word in enumerate(_words(transcript, channel))
        )
        entities.extend(_entities(transcript, channel))
    keyed.sort(key=lambda item: item[:3])
    return language, [word for *_, word in keyed], entities


def parse_transcript(document: Mapping[str, Any]) -> BatchTranscript:
    """Any of the three response shapes as a :class:`BatchTranscript`."""
    transcripts = document.get("transcripts")
    if transcripts is not None:
        if not isinstance(transcripts, list):
            raise ElevenLabsProtocolError(_OPERATION, "transcripts is not a list")
        language, words, entities = _merged(transcripts)
        text = " ".join(word.text for word in words if word.type == "word")
    else:
        language = _optional_str(document, "language_code")
        words = _words(document, None)
        entities = _entities(document, None)
        text = _optional_str(document, "text") or ""
    return BatchTranscript(
        language_code=language,
        text=text,
        words=tuple(words),
        entities=tuple(entities),
        transcription_id=_optional_str(document, "transcription_id"),
        audio_duration_secs=_optional_float(document, "audio_duration_secs"),
    )


def _role(word: BatchWord) -> Role:
    if word.channel_index is not None:
        return _ROLE_BY_CHANNEL.get(word.channel_index, "assistant")
    return _ROLE_BY_SPEAKER.get(word.speaker_id or "speaker_0", "assistant")


def _utterance(role: Role, words: list[BatchWord]) -> Utterance:
    return Utterance(
        role=role,
        text=" ".join(word.text for word in words),
        start=words[0].start,
        end=max(word.end for word in words),
        words=tuple(words),
    )


def group_utterances(words: Iterable[BatchWord], gap_s: float = UTTERANCE_GAP_S) -> tuple[Utterance, ...]:
    """Speaker turns: ``word`` entries sorted by start (stable), split on a role change or a silence longer
    than ``gap_s`` (compared at microsecond precision). Channel 0 / ``speaker_0`` is the caller (``user``),
    anything else the agent (``assistant``)."""
    spoken = sorted((word for word in words if word.type == "word"), key=lambda word: word.start)
    groups: list[tuple[Role, list[BatchWord]]] = []
    for word in spoken:
        role = _role(word)
        if groups and groups[-1][0] == role and round(word.start - groups[-1][1][-1].end, 6) <= gap_s:
            groups[-1][1].append(word)
        else:
            groups.append((role, [word]))
    return tuple(_utterance(role, group) for role, group in groups)


def _transcript_of(response: RestResponse) -> BatchTranscript:
    return parse_transcript(response.json_object(BATCH_TRANSCRIBE))


class BatchSttCommands:
    """Batch transcription operation of :class:`va_elevenlabs.rest.ElevenLabsRest`."""

    _transport: ElevenLabsTransport

    async def batch_transcribe(
        self, source: AudioUpload | SourceUrl, options: BatchOptions
    ) -> BatchTranscript:
        params = {"enable_logging": "false"} if options.zero_retention else None
        return await self._transport.call(
            BATCH_TRANSCRIBE,
            "/v1/speech-to-text",
            _transcript_of,
            files=batch_form(source, options),
            params=params,
        )
