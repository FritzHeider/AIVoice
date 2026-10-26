"""Error rendering in both envelopes (specs/31-error-catalog.md §1, §4)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from api_helpers import make_client, make_settings, sample_app
from fastapi.exceptions import RequestValidationError
from sample_routes import ENVELOPE_PREFIX, LEGACY_PREFIX

from va_api.envelopes import Envelope
from va_api.errors import ErrorEnvelope, ErrorLegacy, error_body, validation_items
from va_common.logging import configure_logging

VALID = {"phone_number": "+14155550123", "voicemail": {"action": "hangup"}}


async def get_json(path: str, method: str = "GET", **kwargs: Any) -> tuple[int, dict[str, Any], Any]:
    async with make_client(sample_app()) as client:
        response = await client.request(method, path, **kwargs)
    return response.status_code, response.json(), response.headers


async def test_app_error_legacy_shape() -> None:
    status, body, headers = await get_json(f"{LEGACY_PREFIX}/app_error")
    assert status == 403
    assert body == {
        "status": "error",
        "message": "This number is blocked.",
        "errors": [
            {
                "error": "NUMBER_BLOCKED",
                "message": "This number is blocked.",
                "field": "phone_number",
                "details": {"reason": "dnc"},
            }
        ],
        "request_id": headers["x-request-id"],
    }


async def test_app_error_envelope_shape_with_details() -> None:
    status, body, headers = await get_json(f"{ENVELOPE_PREFIX}/app_error")
    assert status == 403
    assert body == {
        "data": None,
        "errors": [
            {
                "error": "NUMBER_BLOCKED",
                "message": "This number is blocked.",
                "field": "phone_number",
                "details": {"reason": "dnc"},
            }
        ],
        "request_id": headers["x-request-id"],
    }


def test_legacy_message_equals_first_error() -> None:
    items = [{"error": "INVALID_INPUT", "message": "first"}, {"error": "INVALID_INPUT", "message": "second"}]
    body = error_body(Envelope.LEGACY, items, "req_1")
    assert body["message"] == "first"
    assert body["errors"] == items
    assert error_body(Envelope.ENVELOPE, items, "req_1") == {
        "data": None,
        "errors": items,
        "request_id": "req_1",
    }


async def test_validation_400_legacy_one_item_per_field() -> None:
    payload = {"phone_number": "4155550123", "voicemail": {"action": "explode"}}
    status, body, _ = await get_json(f"{LEGACY_PREFIX}/body", "POST", json=payload)
    assert status == 400
    assert body["status"] == "error"
    assert [item["field"] for item in body["errors"]] == ["phone_number", "voicemail.action"]
    assert {item["error"] for item in body["errors"]} == {"INVALID_INPUT"}
    assert body["message"] == body["errors"][0]["message"]


async def test_validation_400_envelope() -> None:
    status, body, _ = await get_json(f"{ENVELOPE_PREFIX}/body", "POST", json={"phone_number": "+1"})
    assert status == 400
    assert body["data"] is None
    assert body["errors"] == [{"error": "INVALID_INPUT", "message": "Field required", "field": "voicemail"}]


async def test_validation_query_field() -> None:
    status, body, _ = await get_json(f"{ENVELOPE_PREFIX}/query", params={"limit": "500"})
    assert status == 400
    assert body["errors"][0]["field"] == "limit"
    assert body["errors"][0]["error"] == "INVALID_INPUT"


async def test_invalid_json_body_has_no_field() -> None:
    status, body, _ = await get_json(
        f"{LEGACY_PREFIX}/body", "POST", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert status == 400
    assert len(body["errors"]) == 1
    assert "field" not in body["errors"][0]
    assert body["errors"][0]["error"] == "INVALID_INPUT"


async def test_value_error_prefix_stripped() -> None:
    payload = {**VALID, "phone_number": "+1415abc"}
    _, body, _ = await get_json(f"{LEGACY_PREFIX}/body", "POST", json=payload)
    assert body["errors"] == [{"error": "INVALID_INPUT", "message": "must be E.164", "field": "phone_number"}]


async def test_valid_body_succeeds() -> None:
    status, body, _ = await get_json(f"{LEGACY_PREFIX}/body", "POST", json=VALID)
    assert status == 200
    assert body == {"status": "success", "limit": 12}


@pytest.mark.parametrize(
    ("path", "envelope"),
    [
        ("/v1/knowledge/x", Envelope.ENVELOPE),
        ("/internal/anything", Envelope.ENVELOPE),
        ("/v1/calls/x/y", Envelope.LEGACY),
        ("/nope", Envelope.LEGACY),
    ],
)
async def test_unknown_path_uses_family(path: str, envelope: Envelope) -> None:
    status, body, _ = await get_json(path)
    assert status == 404
    item = {"error": "NOT_FOUND", "message": "Not found."}
    if envelope is Envelope.LEGACY:
        assert body["status"] == "error"
        assert body["errors"] == [item]
    else:
        assert body["data"] is None
        assert body["errors"] == [item]


async def test_method_not_allowed_keeps_allow_header() -> None:
    status, body, headers = await get_json(f"{ENVELOPE_PREFIX}/query", "DELETE")
    assert status == 405
    assert headers["allow"] == "GET"
    assert body["errors"] == [{"error": "INVALID_INPUT", "message": "Method not allowed."}]
    assert body["data"] is None


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (400, "INVALID_INPUT", "detail 400"),
        (401, "AUTH_FAILURE", "detail 401"),
        (403, "FORBIDDEN", "detail 403"),
        (404, "NOT_FOUND", "Not found."),
        (409, "CONFLICT", "detail 409"),
        (418, "INVALID_INPUT", "detail 418"),
        (429, "RATE_LIMITED", "detail 429"),
        (502, "INTERNAL_ERROR", "detail 502"),
        (503, "SERVICE_UNAVAILABLE", "detail 503"),
        (504, "UPSTREAM_TIMEOUT", "detail 504"),
    ],
)
async def test_http_exception_mapping(status: int, code: str, message: str) -> None:
    got, body, headers = await get_json(f"{LEGACY_PREFIX}/http/{status}")
    assert got == status
    assert body["errors"] == [{"error": code, "message": message}]
    assert body["message"] == message
    if status == 429:
        assert headers["retry-after"] == "7"


async def test_unhandled_exception_is_500_without_leak() -> None:
    stream = io.StringIO()
    configure_logging(make_settings(log_format="json"), stream=stream, environ={}, pod="p")
    status, body, headers = await get_json(f"{LEGACY_PREFIX}/boom")
    assert status == 500
    assert body["errors"] == [{"error": "INTERNAL_ERROR", "message": "Internal server error"}]
    raw = json.dumps(body)
    assert "secret detail" not in raw
    assert "Traceback" not in raw
    assert headers["x-request-id"] == body["request_id"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    crash = next(line for line in lines if line["event"] == "api.unhandled_exception")
    assert crash["request_id"] == body["request_id"]
    assert crash["route"] == f"{LEGACY_PREFIX}/boom"
    assert crash["level"] == "error"
    assert "secret detail" in crash["exception"]


@pytest.mark.parametrize("prefix", [LEGACY_PREFIX, ENVELOPE_PREFIX])
async def test_unhandled_exception_envelope_family(prefix: str) -> None:
    _, body, _ = await get_json(f"{prefix}/boom")
    if prefix == LEGACY_PREFIX:
        ErrorLegacy.model_validate(body)
    else:
        ErrorEnvelope.model_validate(body)


@pytest.mark.parametrize(
    "path",
    ["app_error", "kb_error", "http/409", "boom", "nope"],
)
async def test_bodies_validate_against_models(path: str) -> None:
    _, legacy, _ = await get_json(f"{LEGACY_PREFIX}/{path}")
    _, envelope, _ = await get_json(f"{ENVELOPE_PREFIX}/{path}")
    ErrorLegacy.model_validate(legacy)
    ErrorEnvelope.model_validate(envelope)


async def test_kb_error_alternate_status_rendered() -> None:
    status, body, _ = await get_json(f"{ENVELOPE_PREFIX}/kb_error")
    assert status == 500
    assert body["errors"] == [{"error": "KB_ERROR", "message": "Embedding failed."}]


def test_validation_items_field_paths() -> None:
    exc = RequestValidationError(
        [
            {"type": "missing", "loc": ("body", "items", 0, "name"), "msg": "Field required"},
            {"type": "missing", "loc": ("body",), "msg": "Field required"},
            {"type": "value_error", "loc": ("custom", "x"), "msg": "Value error, bad"},
        ]
    )
    assert validation_items(exc) == [
        {"error": "INVALID_INPUT", "message": "Field required", "field": "items.0.name"},
        {"error": "INVALID_INPUT", "message": "Field required"},
        {"error": "INVALID_INPUT", "message": "bad", "field": "custom.x"},
    ]
    assert validation_items(RequestValidationError([])) == [
        {"error": "INVALID_INPUT", "message": "Invalid input"}
    ]
