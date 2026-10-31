"""Batch STT request parameters (specs/30 §3.3; Decision P03-T05b rule 1).

``POST /v1/speech-to-text`` takes multipart form fields and the ``enable_logging`` query parameter. Exactly
one audio source is required: an uploaded ``file``, a ``cloud_storage_url``, or a ``source_url``. Failures
are ElevenLabs 422 detail items (``{"loc", "msg", "type"}``); unknown fields are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from starlette.datastructures import FormData, QueryParams, UploadFile

from va_sim.elevenlabs.rest_io import error_item, validation_errors

__all__ = ["BatchForm", "BatchParams", "FormBool", "form_bool", "parse_batch_form", "parse_enable_logging"]

MAX_KEYTERMS: Final = 1000
_MAX_KEYTERM_CHARS: Final = 49
_MAX_KEYTERM_WORDS: Final = 5
_KEYTERM_FORBIDDEN: Final = frozenset("<>{}[]\\")
_MAX_SPEAKERS: Final = 32
_URL_SCHEMES: Final = frozenset({"http", "https"})
_SCALAR_FIELDS: Final = (
    "model_id",
    "cloud_storage_url",
    "source_url",
    "language_code",
    "timestamps_granularity",
    "diarize",
    "use_multi_channel",
    "tag_audio_events",
    "num_speakers",
    "multichannel_output_style",
    "file_format",
)
_LIST_FIELDS: Final = ("keyterms", "entity_detection")


def form_bool(value: object) -> object:
    """``true``/``false`` in any case; booleans pass through (pydantic reports everything else)."""
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if isinstance(value, bool):
        return value
    msg = "must be true or false"
    raise ValueError(msg)


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in _URL_SCHEMES or not parts.netloc:
        msg = "must be an http or https URL"
        raise ValueError(msg)
    return value


def _keyterm(value: str) -> str:
    if len(value.split()) > _MAX_KEYTERM_WORDS:
        msg = f"a keyterm has at most {_MAX_KEYTERM_WORDS} words"
        raise ValueError(msg)
    if _KEYTERM_FORBIDDEN & set(value):
        msg = "a keyterm must not contain any of < > { } [ ] \\"
        raise ValueError(msg)
    return value


type FormBool = Annotated[bool, BeforeValidator(form_bool)]
type HttpUrl = Annotated[str, AfterValidator(_http_url)]
type Keyterm = Annotated[str, Field(min_length=1, max_length=_MAX_KEYTERM_CHARS), AfterValidator(_keyterm)]
type EntityCategory = Literal["all", "pii", "phi", "pci", "other", "offensive_language"]
type Granularity = Literal["none", "word", "character"]
type OutputStyle = Literal["separate", "combined"]
type FileFormat = Literal["pcm_s16le_16", "other"]


class BatchForm(BaseModel):
    """The validated form fields (unknown fields ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    model_id: str = Field(min_length=1)
    cloud_storage_url: HttpUrl | None = None
    source_url: HttpUrl | None = None
    language_code: Annotated[str, Field(pattern=r"^[a-z]{2,3}$")] | None = None
    timestamps_granularity: Granularity = "word"
    diarize: FormBool = False
    use_multi_channel: FormBool = False
    tag_audio_events: FormBool = True
    num_speakers: Annotated[int, Field(ge=1, le=_MAX_SPEAKERS)] | None = None
    multichannel_output_style: OutputStyle = "separate"
    keyterms: Annotated[list[Keyterm], Field(max_length=MAX_KEYTERMS)] = Field(default_factory=list)
    entity_detection: list[EntityCategory] = Field(default_factory=list)
    file_format: FileFormat = "other"

    @model_validator(mode="after")
    def _combined_needs_timestamps_without_entities(self) -> BatchForm:
        if self.multichannel_output_style != "combined":
            return self
        if self.timestamps_granularity == "none":
            msg = (
                "multichannel_output_style 'combined' requires timestamps (timestamps_granularity != 'none')"
            )
            raise ValueError(msg)
        if self.entity_detection:
            msg = "multichannel_output_style 'combined' does not support entity_detection"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class BatchParams:
    """A valid batch request: the form fields, the upload (when the source is a file), and logging."""

    form: BatchForm
    upload: UploadFile | None
    enable_logging: bool

    @property
    def url(self) -> str | None:
        """The ``cloud_storage_url`` or ``source_url`` source (``None`` for an upload)."""
        return self.form.cloud_storage_url or self.form.source_url


def parse_enable_logging(query: QueryParams) -> bool | dict[str, object]:
    """The ``enable_logging`` query parameter (default true), or its 422 detail item."""
    raw = query.get("enable_logging")
    if raw is None:
        return True
    try:
        value = form_bool(raw)
    except ValueError as exc:
        return error_item(["query", "enable_logging"], str(exc), "bool_parsing")
    return bool(value)


def _source_errors(form: FormData, fields: BatchForm | None) -> list[dict[str, object]]:
    upload = form.get("file")
    if upload is not None and not isinstance(upload, UploadFile):
        return [error_item(["body", "file"], "file must be an uploaded file", "value_error")]
    if fields is None:
        return []
    count = sum(source is not None for source in (upload, fields.cloud_storage_url, fields.source_url))
    if count != 1:
        msg = "exactly one of file, cloud_storage_url, or source_url must be provided"
        return [error_item(["body"], msg, "value_error")]
    return []


def _fields(form: FormData) -> BatchForm | list[dict[str, object]]:
    """The validated form fields, or their 422 detail items."""
    raw: dict[str, object] = {name: form[name] for name in _SCALAR_FIELDS if name in form}
    raw.update({name: form.getlist(name) for name in _LIST_FIELDS if name in form})
    try:
        return BatchForm.model_validate(raw)
    except ValidationError as exc:
        return validation_errors(exc)


def parse_batch_form(form: FormData, query: QueryParams) -> BatchParams | list[dict[str, object]]:
    """The request parameters, or every 422 detail item found."""
    fields = _fields(form)
    errors = list(fields) if isinstance(fields, list) else []
    errors.extend(_source_errors(form, None if isinstance(fields, list) else fields))
    logging = parse_enable_logging(query)
    if isinstance(logging, dict):
        errors.append(logging)
    if errors or isinstance(fields, list) or isinstance(logging, dict):
        return errors
    upload = form.get("file")
    return BatchParams(fields, upload if isinstance(upload, UploadFile) else None, logging)
