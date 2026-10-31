"""FakeElevenLabs voices request parameters (Decision P03-T05c rules 4-8).

``GET /v2/voices`` query parameters, the multipart fields of ``POST /v1/voices/add`` and
``POST /v1/voices/{voice_id}/edit``, and the ``settings/edit`` JSON body. Failures are ElevenLabs 422 detail
items (``{"loc", "msg", "type"}``); unknown parameters and fields are ignored.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Final, Literal

import orjson
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, ValidationError
from starlette.datastructures import FormData, QueryParams, UploadFile

from va_sim.elevenlabs.rest_io import error_item, validation_errors
from va_sim.elevenlabs.stt_batch_params import FormBool

__all__ = [
    "MAX_FILES",
    "SettingsPatch",
    "VoiceForm",
    "VoiceListQuery",
    "encode_page_token",
    "parse_files",
    "parse_list_query",
    "parse_voice_form",
]

MAX_FILES: Final = 25
_MAX_PAGE_SIZE: Final = 100
_MAX_VOICE_IDS: Final = 100
_MAX_NAME: Final = 256
_MAX_DESCRIPTION: Final = 500
_QUERY_SCALARS: Final = (
    "page_size",
    "next_page_token",
    "search",
    "sort",
    "sort_direction",
    "voice_type",
    "category",
    "include_total_count",
)
_FORM_SCALARS: Final = ("name", "description", "labels", "remove_background_noise")


def encode_page_token(offset: int) -> str:
    """The ``next_page_token`` for ``offset``: unpadded url-safe base64 of its decimal text."""
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).rstrip(b"=").decode("ascii")


def _decode_page_token(value: object) -> object:
    """The offset a ``next_page_token`` encodes (non-strings pass through for pydantic to reject)."""
    if not isinstance(value, str):
        return value
    try:
        text = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode(
            "ascii"
        )
    except (binascii.Error, UnicodeDecodeError) as exc:
        msg = "next_page_token is not a valid page token"
        raise ValueError(msg) from exc
    if not text.isascii() or not text.isdigit():
        msg = "next_page_token is not a valid page token"
        raise ValueError(msg)
    return int(text)


def _json_object(value: object) -> object:
    """A form field holding JSON text, parsed (pydantic then checks the shape)."""
    if not isinstance(value, str):
        return value
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError as exc:
        msg = "labels must be a JSON object of strings"
        raise ValueError(msg) from exc


type PageOffset = Annotated[int, BeforeValidator(_decode_page_token)]
type Labels = Annotated[dict[str, str], BeforeValidator(_json_object)]
type Unit = Annotated[float, Field(ge=0.0, le=1.0)]
type ListSort = Literal["created_at_unix", "name"]
type VoiceType = Literal["personal", "community", "default", "workspace", "non-default", "saved"]
type ListCategory = Literal["premade", "cloned", "generated", "professional"]


class VoiceListQuery(BaseModel):
    """``GET /v2/voices`` query; ``next_page_token`` holds the decoded offset."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    page_size: Annotated[int, Field(ge=1, le=_MAX_PAGE_SIZE)] = 10
    next_page_token: PageOffset | None = None
    search: str | None = None
    sort: ListSort = "created_at_unix"
    sort_direction: Literal["asc", "desc"] | None = None
    voice_type: VoiceType | None = None
    category: ListCategory | None = None
    voice_ids: Annotated[list[str], Field(max_length=_MAX_VOICE_IDS)] = Field(default_factory=list)
    include_total_count: FormBool = True

    @property
    def descending(self) -> bool:
        """Sort direction: ``desc`` by default for ``created_at_unix``, ``asc`` for ``name``."""
        if self.sort_direction is not None:
            return self.sort_direction == "desc"
        return self.sort == "created_at_unix"


class VoiceForm(BaseModel):
    """The text fields of an add or edit form."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=_MAX_NAME)]
    description: Annotated[str, Field(max_length=_MAX_DESCRIPTION)] | None = None
    labels: Labels | None = None
    remove_background_noise: FormBool = False


class SettingsPatch(BaseModel):
    """The ``settings/edit`` body: any subset of the five voice settings (``null`` values are ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    stability: Unit | None = None
    similarity_boost: Unit | None = None
    style: Unit | None = None
    use_speaker_boost: StrictBool | None = None
    speed: Annotated[float, Field(ge=0.7, le=1.2)] | None = None

    def sent(self) -> dict[str, float | bool]:
        values: dict[str, float | bool] = self.model_dump(exclude_none=True)
        return values


def parse_list_query(query: QueryParams) -> VoiceListQuery | list[dict[str, object]]:
    raw: dict[str, object] = {name: query[name] for name in _QUERY_SCALARS if name in query}
    if "voice_ids" in query:
        raw["voice_ids"] = query.getlist("voice_ids")
    try:
        return VoiceListQuery.model_validate(raw)
    except ValidationError as exc:
        return validation_errors(exc, "query")


def parse_voice_form(form: FormData) -> VoiceForm | list[dict[str, object]]:
    raw: dict[str, object] = {name: form[name] for name in _FORM_SCALARS if name in form}
    try:
        return VoiceForm.model_validate(raw)
    except ValidationError as exc:
        return validation_errors(exc)


def parse_files(form: FormData, *, required: bool) -> list[UploadFile] | dict[str, object]:
    """The ``files`` uploads (0..25; at least one when ``required``), or their 422 detail item."""
    uploads = form.getlist("files")
    files = [upload for upload in uploads if isinstance(upload, UploadFile)]
    if len(files) != len(uploads):
        return error_item(["body", "files"], "files must be uploaded files", "value_error")
    if required and not files:
        return error_item(["body", "files"], "Field required", "missing")
    if len(files) > MAX_FILES:
        return error_item(["body", "files"], f"at most {MAX_FILES} files are allowed", "too_long")
    return files
