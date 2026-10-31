"""FakeElevenLabs voices and subscription REST (specs/30 §3.3 REST, specs/15 §2-§3, §7; Decision P03-T05c).

``GET /v2/voices`` (paged search), ``GET /v1/voices``, ``POST /v1/voices/add``, ``GET``/``DELETE
/v1/voices/{voice_id}``, ``GET /v1/voices/{voice_id}/settings``, ``POST .../settings/edit``, ``POST
.../edit``, and ``GET /v1/user/subscription``. Order of checks: ``xi-api-key`` (401), the armed voices fault
(500 / 429 / delay), validation (422), lookup (404), then the operation (400 for premade voices, samples, and
the voice limit). Sample parsing runs in a worker thread under ``hold_clock``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.elevenlabs.auth import API_KEY_HEADER, detail_error, rest_auth_error
from va_sim.elevenlabs.rest_io import InvalidBodyError, multipart_form, parse_json_body, unprocessable
from va_sim.elevenlabs.voice_json import (
    subscription_json,
    voice_json,
    voice_not_editable,
    voice_not_found,
)
from va_sim.elevenlabs.voice_params import (
    MAX_FILES,
    SettingsPatch,
    VoiceForm,
    VoiceListQuery,
    encode_page_token,
    parse_files,
    parse_list_query,
    parse_voice_form,
)
from va_sim.elevenlabs.voice_samples import SampleError, measure
from va_sim.elevenlabs.voice_store import SampleFile, VoiceCategory, VoiceChange, VoiceRecord, VoiceSample
from va_sim.tasks import hold_clock

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["voice_rest_routes"]

type Handler = Callable[[Request], Awaitable[Response]]

_MS_PER_S: Final = 1000
_DEFAULT_FILE_NAME: Final = "sample"
_DURATION_DECIMALS: Final = 3
_VOICE_TYPES: Final[dict[str, frozenset[VoiceCategory]]] = {
    "default": frozenset({"premade"}),
    "personal": frozenset({"cloned"}),
    "workspace": frozenset({"cloned"}),
    "non-default": frozenset({"cloned"}),
    "community": frozenset(),
    "saved": frozenset(),
}


def _search_hit(record: VoiceRecord, query: VoiceListQuery) -> bool:
    if query.search is None:
        return True
    needle = query.search.casefold()
    haystack = (record.name, record.description or "", record.category, *record.labels.values())
    return any(needle in text.casefold() for text in haystack)


def _type_hit(record: VoiceRecord, query: VoiceListQuery) -> bool:
    return query.voice_type is None or record.category in _VOICE_TYPES[query.voice_type]


def _category_hit(record: VoiceRecord, query: VoiceListQuery) -> bool:
    return query.category is None or record.category == query.category


def _id_hit(record: VoiceRecord, query: VoiceListQuery) -> bool:
    return not query.voice_ids or record.voice_id in query.voice_ids


_FILTERS: Final = (_search_hit, _type_hit, _category_hit, _id_hit)


def _sort_key(query: VoiceListQuery) -> Callable[[VoiceRecord], tuple[str, int]]:
    if query.sort == "name":
        return lambda record: (record.name.casefold(), record.seq)
    return lambda record: ("", record.seq)


def _measure_all(named: list[tuple[str, bytes]]) -> list[SampleFile]:
    """Every sample measured and hashed (worker thread); :class:`SampleError` for the first rejected one."""
    files: list[SampleFile] = []
    for file_name, data in named:
        info = measure(file_name, data)
        duration = None if info.duration_s is None else round(info.duration_s, _DURATION_DECIMALS)
        files.append(
            SampleFile(file_name, info.mime_type, len(data), hashlib.sha256(data).hexdigest(), duration)
        )
    return files


class _Voices:
    """The voices handlers over one simulator state."""

    def __init__(self, state: SimState) -> None:
        self.state = state
        self.store = state.elevenlabs.voices

    def guarded(self, handler: Handler) -> Handler:
        """``handler`` behind authentication and the armed voices fault."""

        async def endpoint(request: Request) -> Response:
            key = request.headers.get(API_KEY_HEADER)
            denied = rest_auth_error(key, self.state.elevenlabs.accepts(key))
            if denied is not None:
                return denied
            failed = await self.fault()
            return failed if failed is not None else await handler(request)

        return endpoint

    async def fault(self) -> Response | None:
        fault = self.store.take_fault()
        if fault is None:
            return None
        if fault.kind == "error":
            return detail_error(500, "internal_error", "Simulated voices failure")
        if fault.kind == "rate_limited":
            return detail_error(429, "too_many_concurrent_requests", "Simulated rate limit")
        await self.state.clock.sleep(fault.delay_ms / _MS_PER_S)
        return None

    def lookup(self, request: Request) -> VoiceRecord | Response:
        voice_id: str = request.path_params["voice_id"]
        record = self.store.get(voice_id)
        return voice_not_found(voice_id) if record is None else record

    def editable(self, request: Request) -> VoiceRecord | Response:
        record = self.lookup(request)
        if isinstance(record, VoiceRecord) and record.category == "premade":
            return voice_not_editable()
        return record

    async def samples(self, uploads: list[UploadFile]) -> list[VoiceSample]:
        named = [(upload.filename or _DEFAULT_FILE_NAME, await upload.read()) for upload in uploads]
        async with hold_clock(self.state.clock, self.state.tasks, name="voice-samples"):
            files = await asyncio.to_thread(_measure_all, named)
        return [VoiceSample(self.state.ids.alnum(), file) for file in files]

    @staticmethod
    def form(form: FormData, *, required: bool) -> tuple[VoiceForm, list[UploadFile]] | Response:
        fields = parse_voice_form(form)
        files = parse_files(form, required=required)
        errors = list(fields) if isinstance(fields, list) else []
        if isinstance(files, dict):
            errors.append(files)
        if errors or isinstance(fields, list) or isinstance(files, dict):
            return unprocessable(errors)
        return fields, files

    # Reading.

    async def list_v2(self, request: Request) -> Response:
        query = parse_list_query(request.query_params)
        if isinstance(query, list):
            return unprocessable(query)
        matches = [record for record in self.store.ordered() if all(hit(record, query) for hit in _FILTERS)]
        matches.sort(key=_sort_key(query), reverse=query.descending)
        offset = query.next_page_token or 0
        end = offset + query.page_size
        has_more = end < len(matches)
        base = str(request.base_url)
        return JSONResponse(
            {
                "voices": [voice_json(record, base) for record in matches[offset:end]],
                "has_more": has_more,
                "total_count": len(matches) if query.include_total_count else None,
                "next_page_token": encode_page_token(end) if has_more else None,
            }
        )

    async def list_v1(self, request: Request) -> Response:
        base = str(request.base_url)
        return JSONResponse({"voices": [voice_json(record, base) for record in self.store.ordered()]})

    async def get(self, request: Request) -> Response:
        record = self.lookup(request)
        if isinstance(record, Response):
            return record
        return JSONResponse(voice_json(record, str(request.base_url)))

    async def settings(self, request: Request) -> Response:
        record = self.lookup(request)
        if isinstance(record, Response):
            return record
        return JSONResponse(dict(record.settings))

    async def subscription(self, _request: Request) -> Response:
        now_unix = int(self.state.clock.now().timestamp())
        return JSONResponse(subscription_json(self.state.elevenlabs, now_unix))

    # Mutating.

    async def settings_edit(self, request: Request) -> Response:
        received_ms = self.state.clock.virtual_ms()
        patch = parse_json_body(await request.body(), SettingsPatch)
        if isinstance(patch, Response):
            return patch
        record = self.lookup(request)
        if isinstance(record, Response):
            return record
        merged = self.store.edit_settings(record, patch.sent())
        self.store.record("settings_edit", record.voice_id, received_ms=received_ms, settings=merged)
        return JSONResponse({"status": "ok"})

    async def add(self, request: Request) -> Response:
        received_ms = self.state.clock.virtual_ms()
        try:
            async with multipart_form(request, max_files=MAX_FILES + 1) as form:
                parsed = self.form(form, required=True)
                if isinstance(parsed, Response):
                    return parsed
                fields, uploads = parsed
                samples = await self.samples(uploads)
        except InvalidBodyError as exc:
            return exc.response
        except SampleError as exc:
            return detail_error(400, "invalid_sample", exc.message)
        limit = self.store.subscription.voice_limit
        if self.store.cloned_count() >= limit:
            message = f"You have reached your maximum amount of custom voices ({limit} / {limit})."
            return detail_error(400, "voice_limit_reached", message)
        change = VoiceChange(
            fields.name, fields.description, fields.labels, samples, fields.remove_background_noise
        )
        voice_id = self.state.ids.alnum()
        self.store.add(voice_id, change, created_at_unix=int(self.state.clock.now().timestamp()))
        self.store.record("add", voice_id, received_ms=received_ms, change=change)
        return JSONResponse({"voice_id": voice_id, "requires_verification": False})

    async def edit(self, request: Request) -> Response:
        received_ms = self.state.clock.virtual_ms()
        try:
            async with multipart_form(request, max_files=MAX_FILES + 1) as form:
                parsed = self.form(form, required=False)
                if isinstance(parsed, Response):
                    return parsed
                record = self.editable(request)
                if isinstance(record, Response):
                    return record
                fields, uploads = parsed
                samples = await self.samples(uploads) if uploads else None
        except InvalidBodyError as exc:
            return exc.response
        except SampleError as exc:
            return detail_error(400, "invalid_sample", exc.message)
        change = VoiceChange(
            fields.name, fields.description, fields.labels, samples, fields.remove_background_noise
        )
        self.store.edit(record, change)
        self.store.record("edit", record.voice_id, received_ms=received_ms, change=change)
        return JSONResponse({"status": "ok"})

    async def delete(self, request: Request) -> Response:
        received_ms = self.state.clock.virtual_ms()
        record = self.editable(request)
        if isinstance(record, Response):
            return record
        self.store.delete(record.voice_id)
        self.store.record("delete", record.voice_id, received_ms=received_ms)
        return JSONResponse({"status": "ok"})


def voice_rest_routes(state: SimState) -> list[BaseRoute]:
    voices = _Voices(state)
    guarded = voices.guarded
    voice = "/v1/voices/{voice_id}"
    return [
        Route("/v2/voices", guarded(voices.list_v2), methods=["GET"]),
        Route("/v1/voices", guarded(voices.list_v1), methods=["GET"]),
        Route("/v1/voices/add", guarded(voices.add), methods=["POST"]),
        Route(voice, guarded(voices.get), methods=["GET"]),
        Route(voice, guarded(voices.delete), methods=["DELETE"]),
        Route(f"{voice}/settings", guarded(voices.settings), methods=["GET"]),
        Route(f"{voice}/settings/edit", guarded(voices.settings_edit), methods=["POST"]),
        Route(f"{voice}/edit", guarded(voices.edit), methods=["POST"]),
        Route("/v1/user/subscription", guarded(voices.subscription), methods=["GET"]),
    ]
