"""ElevenLabs voices REST (specs/15-voices.md §2-§3; Decision 2026-11-01 P03-T12 rules 2, 3, 5).

Reads (``list_voices`` over ``GET /v2/voices`` pages, ``get_voice``, ``get_voice_settings``), the full
settings write, and ``delete_voice`` retry 0.5/1/2 s with a 10 s timeout. ``add_voice`` and ``edit_voice``
upload multipart samples with a 60 s timeout and are never retried by the client (the clone workflow
owns retries).
Multipart fields go out in this order: ``name``, ``description``, ``labels`` (compact JSON object),
``remove_background_noise`` (``true``/``false``), then one ``files`` part per sample.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict

from va_domain.voices import VoiceSettings
from va_elevenlabs.rest_errors import ElevenLabsNotFoundError, ElevenLabsProtocolError
from va_elevenlabs.rest_models import AddedVoice, VendorVoice, VoiceFile, vendor_settings
from va_elevenlabs.rest_retry import NO_RETRY, READ_RETRY
from va_elevenlabs.rest_transport import (
    ElevenLabsTransport,
    Exchange,
    FormPart,
    RestOperation,
    RestResponse,
    no_body,
    parse_model,
    segment,
)

__all__ = [
    "ADD_VOICE",
    "DELETE_VOICE",
    "EDIT_VOICE",
    "GET_VOICE",
    "GET_VOICE_SETTINGS",
    "LIST_VOICES",
    "MAX_VOICE_PAGES",
    "UPDATE_VOICE_SETTINGS",
    "VoiceCommands",
    "voice_form",
]

LIST_VOICES: Final = RestOperation("list_voices", "GET", 10.0, READ_RETRY)
GET_VOICE: Final = RestOperation("get_voice", "GET", 10.0, READ_RETRY)
GET_VOICE_SETTINGS: Final = RestOperation("get_voice_settings", "GET", 10.0, READ_RETRY)
UPDATE_VOICE_SETTINGS: Final = RestOperation("update_voice_settings", "POST", 10.0, READ_RETRY)
DELETE_VOICE: Final = RestOperation("delete_voice", "DELETE", 10.0, READ_RETRY)
ADD_VOICE: Final = RestOperation("add_voice", "POST", 60.0, NO_RETRY)
EDIT_VOICE: Final = RestOperation("edit_voice", "POST", 60.0, NO_RETRY)
MAX_VOICE_PAGES: Final = 100
_MAX_PAGE_SIZE: Final = 100


class _VoicePage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    voices: list[VendorVoice]
    has_more: bool = False
    next_page_token: str | None = None


def voice_form(
    name: str,
    files: Sequence[VoiceFile],
    *,
    description: str | None,
    labels: Mapping[str, str] | None,
    remove_background_noise: bool,
) -> list[FormPart]:
    """The multipart parts of an add or edit request, in the documented order."""
    parts: list[FormPart] = [("name", (None, name))]
    if description is not None:
        parts.append(("description", (None, description)))
    if labels is not None:
        parts.append(("labels", (None, orjson.dumps(dict(labels)).decode("utf-8"))))
    parts.append(("remove_background_noise", (None, "true" if remove_background_noise else "false")))
    parts.extend(("files", (file.filename, file.data, file.content_type)) for file in files)
    return parts


def _model[M: BaseModel](operation: RestOperation, model: type[M]) -> Callable[[RestResponse], M]:
    """A response parser reading the JSON body as ``model``."""

    def parse(response: RestResponse) -> M:
        return parse_model(operation, model, response.json_object(operation))

    return parse


class VoiceCommands:
    """Voice library and cloning operations of :class:`va_elevenlabs.rest.ElevenLabsRest`."""

    _transport: ElevenLabsTransport

    async def list_voices(
        self,
        *,
        category: str | None = None,
        search: str | None = None,
        voice_type: str | None = None,
        page_size: int = _MAX_PAGE_SIZE,
    ) -> list[VendorVoice]:
        """Every voice matching the filters, following ``next_page_token`` until ``has_more`` is false
        (at most :data:`MAX_VOICE_PAGES` pages)."""
        if not 1 <= page_size <= _MAX_PAGE_SIZE:
            msg = f"page_size must be between 1 and {_MAX_PAGE_SIZE}, got {page_size}"
            raise ValueError(msg)
        base = {"page_size": str(page_size), "include_total_count": "false"}
        filters = {"category": category, "search": search, "voice_type": voice_type}
        base.update({name: value for name, value in filters.items() if value is not None})

        async def pages(exchange: Exchange) -> list[VendorVoice]:
            voices: list[VendorVoice] = []
            token: str | None = None
            for _ in range(MAX_VOICE_PAGES):
                params = base if token is None else {**base, "next_page_token": token}
                response = await exchange.send(exchange.request("/v2/voices", params=params))
                page = parse_model(LIST_VOICES, _VoicePage, response.json_object(LIST_VOICES))
                voices.extend(page.voices)
                if not page.has_more:
                    return voices
                if not page.next_page_token:
                    raise ElevenLabsProtocolError(LIST_VOICES.name, "has_more without next_page_token")
                token = page.next_page_token
            raise ElevenLabsProtocolError(LIST_VOICES.name, f"more than {MAX_VOICE_PAGES} pages")

        return await self._transport.observed(LIST_VOICES, pages)

    async def get_voice(self, voice_id: str) -> VendorVoice:
        return await self._transport.call(
            GET_VOICE, f"/v1/voices/{segment(voice_id)}", _model(GET_VOICE, VendorVoice), voice_id=voice_id
        )

    async def add_voice(
        self,
        name: str,
        files: Sequence[VoiceFile],
        *,
        description: str | None = None,
        labels: Mapping[str, str] | None = None,
        remove_background_noise: bool = False,
    ) -> AddedVoice:
        """Create an instant voice clone from ``files`` (at least one)."""
        if not files:
            msg = "add_voice needs at least one sample file"
            raise ValueError(msg)
        form = voice_form(
            name,
            files,
            description=description,
            labels=labels,
            remove_background_noise=remove_background_noise,
        )
        return await self._transport.call(
            ADD_VOICE, "/v1/voices/add", _model(ADD_VOICE, AddedVoice), files=form
        )

    async def edit_voice(  # noqa: PLR0913 - the vendor form fields, keyword-only
        self,
        voice_id: str,
        name: str,
        files: Sequence[VoiceFile] = (),
        *,
        description: str | None = None,
        labels: Mapping[str, str] | None = None,
        remove_background_noise: bool = False,
    ) -> None:
        """Rename / re-describe a clone; non-empty ``files`` replace its samples. ElevenLabs clears a
        description that is not sent, so pass the current one to keep it."""
        form = voice_form(
            name,
            files,
            description=description,
            labels=labels,
            remove_background_noise=remove_background_noise,
        )
        await self._transport.call(
            EDIT_VOICE, f"/v1/voices/{segment(voice_id)}/edit", no_body, files=form, voice_id=voice_id
        )

    async def get_voice_settings(self, voice_id: str) -> VoiceSettings:
        def settings(response: RestResponse) -> VoiceSettings:
            document = vendor_settings(response.json_object(GET_VOICE_SETTINGS))
            return parse_model(GET_VOICE_SETTINGS, VoiceSettings, document)

        return await self._transport.call(
            GET_VOICE_SETTINGS, f"/v1/voices/{segment(voice_id)}/settings", settings, voice_id=voice_id
        )

    async def update_voice_settings(self, voice_id: str, settings: VoiceSettings) -> None:
        """Write all five settings (a full write, so retrying is safe)."""
        await self._transport.call(
            UPDATE_VOICE_SETTINGS,
            f"/v1/voices/{segment(voice_id)}/settings/edit",
            no_body,
            json=settings.model_dump(mode="json"),
            voice_id=voice_id,
        )

    async def delete_voice(self, voice_id: str, *, missing_ok: bool = True) -> bool:
        """Delete a voice; ``False`` when it was already gone and ``missing_ok`` (specs/15 §3.4)."""
        try:
            await self._transport.call(
                DELETE_VOICE, f"/v1/voices/{segment(voice_id)}", no_body, voice_id=voice_id
            )
        except ElevenLabsNotFoundError:
            if missing_ok:
                return False
            raise
        return True
