"""ElevenLabs REST error parsing and the specs/15 §7 platform mapping."""

from __future__ import annotations

import httpx
import orjson
import pytest

from va_elevenlabs.rest_errors import (
    ElevenLabsApiError,
    ElevenLabsAuthError,
    ElevenLabsError,
    ElevenLabsNotFoundError,
    ElevenLabsProtocolError,
    ElevenLabsRateLimitedError,
    ElevenLabsTransportError,
    api_error,
    parse_error_body,
)


def body(document: object) -> bytes:
    return orjson.dumps(document)


def test_detail_object_shape() -> None:
    content = body({"detail": {"status": "voice_limit_reached", "message": "You have reached the limit"}})
    code, message, detail = parse_error_body(400, content)
    assert (code, message) == ("voice_limit_reached", "You have reached the limit")
    assert detail == {"status": "voice_limit_reached", "message": "You have reached the limit"}


def test_validation_list_shape_takes_the_first_message() -> None:
    content = body(
        {
            "detail": [
                {"loc": ["body", "name"], "msg": "Field required", "type": "missing"},
                {"loc": ["body", "files"], "msg": "Field required", "type": "missing"},
            ]
        }
    )
    code, message, detail = parse_error_body(422, content)
    assert (code, message) == ("validation_error", "Field required")
    assert isinstance(detail, list)
    assert len(detail) == 2


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"<html>Bad gateway</html>", ("http_502", "<html>Bad gateway</html>")),
        (body({"detail": "Not Found"}), ("http_502", "Not Found")),
        (body({"detail": {"message": "only a message"}}), ("http_502", "only a message")),
        (body({"detail": ["plain"]}), ("validation_error", "plain")),
        (body({"error": "other"}), ("http_502", '{"error":"other"}')),
        (b"x" * 500, ("http_502", "x" * 200)),
    ],
)
def test_other_bodies(content: bytes, expected: tuple[str, str]) -> None:
    code, message, _ = parse_error_body(502, content)
    assert (code, message) == expected


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ElevenLabsAuthError),
        (403, ElevenLabsAuthError),
        (404, ElevenLabsNotFoundError),
        (429, ElevenLabsRateLimitedError),
        (400, ElevenLabsApiError),
        (500, ElevenLabsApiError),
    ],
)
def test_status_selects_the_subclass(status: int, kind: type[ElevenLabsApiError]) -> None:
    error = api_error("get_voice", status, body({"detail": {"status": "x", "message": "m"}}))
    assert type(error) is kind
    assert (error.status, error.operation) == (status, "get_voice")
    assert str(error) == f"ElevenLabs get_voice failed with HTTP {status} (x): m"


def test_message_without_vendor_text() -> None:
    assert (
        str(api_error("delete_voice", 500, b"")) == "ElevenLabs delete_voice failed with HTTP 500 (http_500)"
    )


@pytest.mark.parametrize(
    ("status", "code", "http_status"),
    [
        (401, "INTERNAL_ERROR", 500),
        (403, "INTERNAL_ERROR", 500),
        (400, "VOICE_CLONE_FAILED", 502),
        (422, "VOICE_CLONE_FAILED", 502),
        (404, "VOICE_NOT_FOUND", 404),
        (429, "UPSTREAM_TIMEOUT", 504),
        (500, "UPSTREAM_TIMEOUT", 504),
        (503, "UPSTREAM_TIMEOUT", 504),
        (409, "INTERNAL_ERROR", 500),
    ],
)
def test_platform_mapping(status: int, code: str, http_status: int) -> None:
    app_error = api_error(
        "add_voice", status, body({"detail": {"status": "s", "message": "why"}})
    ).to_app_error()
    assert (app_error.code, app_error.http_status) == (code, http_status)


def test_clone_failure_carries_the_provider_message() -> None:
    content = body(
        {"detail": [{"loc": ["body", "files", 0], "msg": "Audio too short", "type": "value_error"}]}
    )
    app_error = api_error("add_voice", 422, content).to_app_error()
    assert app_error.details == {"provider_code": "validation_error", "provider_message": "Audio too short"}


def test_transport_and_protocol_errors() -> None:
    cause = httpx.ConnectTimeout("timed out")
    transport = ElevenLabsTransportError("batch_transcribe", cause)
    assert transport.cause is cause
    assert str(transport) == "ElevenLabs batch_transcribe failed: ConnectTimeout"
    assert transport.to_app_error().code == "UPSTREAM_TIMEOUT"
    protocol = ElevenLabsProtocolError("list_voices", "has_more without next_page_token")
    assert protocol.reason == "has_more without next_page_token"
    assert protocol.to_app_error().code == "INTERNAL_ERROR"
    assert isinstance(protocol, ElevenLabsError)
    assert ElevenLabsError("m", "op").to_app_error().code == "UPSTREAM_TIMEOUT"
