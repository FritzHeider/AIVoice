"""ElevenLabs REST request and response models (Decision 2026-11-01 P03-T12 rules 2, 5).

Response models ignore unknown fields (ElevenLabs adds fields freely); voice settings keep only the five
fields of specs/15 §5 and fall back to their defaults for missing or ``null`` values.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Self

import anyio
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from va_domain.voices import VoiceSettings

__all__ = [
    "AddedVoice",
    "RestAlignment",
    "TtsRestAudio",
    "TtsTimestamped",
    "VendorSample",
    "VendorVoice",
    "VerifiedLanguage",
    "VoiceFile",
    "optional_int",
    "vendor_settings",
]

_DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"


def vendor_settings(raw: object) -> object:
    """The five known voice settings of a vendor settings object (unknown and ``null`` values dropped)."""
    if not isinstance(raw, dict):
        return raw
    return {name: raw[name] for name in VoiceSettings.model_fields if raw.get(name) is not None}


type LenientSettings = Annotated[VoiceSettings, BeforeValidator(vendor_settings)]


class _Vendor(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class VerifiedLanguage(_Vendor):
    language: str
    model_id: str | None = None
    accent: str | None = None
    locale: str | None = None
    preview_url: str | None = None


class VendorSample(_Vendor):
    sample_id: str
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    hash: str | None = None
    duration_secs: float | None = None


class VendorVoice(_Vendor):
    """An ElevenLabs voice object (``GET /v2/voices`` item, ``GET /v1/voices/{voice_id}``)."""

    voice_id: str
    name: str
    category: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    preview_url: str | None = None
    settings: LenientSettings | None = None
    verified_languages: list[VerifiedLanguage] = Field(default_factory=list)
    samples: list[VendorSample] | None = None
    created_at_unix: int | None = None


class AddedVoice(_Vendor):
    """``POST /v1/voices/add`` response."""

    voice_id: str
    requires_verification: bool = False


class RestAlignment(_Vendor):
    """Character alignment of a ``with-timestamps`` response (absolute seconds)."""

    characters: list[str]
    character_start_times_seconds: list[float]
    character_end_times_seconds: list[float]

    @model_validator(mode="after")
    def _same_lengths(self) -> Self:
        lengths = {
            len(self.characters),
            len(self.character_start_times_seconds),
            len(self.character_end_times_seconds),
        }
        if len(lengths) != 1:
            msg = "alignment arrays must have equal lengths"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class TtsRestAudio:
    """Synthesized audio: bytes, their ``Content-Type``, billed characters, and the vendor request id."""

    audio: bytes
    content_type: str
    character_count: int | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class TtsTimestamped:
    """A ``with-timestamps`` synthesis: decoded audio and its alignments (``None`` when none was sent)."""

    audio: bytes
    alignment: RestAlignment | None
    normalized_alignment: RestAlignment | None
    character_count: int | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class VoiceFile:
    """One voice sample to upload."""

    filename: str
    data: bytes = field(repr=False)
    content_type: str = _DEFAULT_CONTENT_TYPE

    @classmethod
    async def from_path(cls, path: Path, *, content_type: str | None = None) -> VoiceFile:
        """The file at ``path`` (read without blocking the loop); type guessed from the extension."""
        data = await anyio.Path(path).read_bytes()
        guessed = content_type or mimetypes.guess_type(path.name)[0] or _DEFAULT_CONTENT_TYPE
        return cls(path.name, data, guessed)


def optional_int(value: Any) -> int | None:
    """An integer header value, or ``None`` when absent or not a number."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
