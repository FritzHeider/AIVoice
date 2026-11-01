"""Telnyx error parsing and the mapping to platform error codes (specs/06 §2.2; specs/31)."""

from __future__ import annotations

import httpx
import orjson

from va_telnyx.errors import (
    CallAlreadyEnded,
    TelnyxApiError,
    TelnyxError,
    TelnyxErrorItem,
    TelnyxRateLimited,
    TelnyxTransportError,
    parse_error_body,
)


def test_parse_telnyx_error_document() -> None:
    body = orjson.dumps(
        {
            "errors": [
                {"code": "10015", "title": "Invalid value", "detail": "bad to", "source": {"pointer": "/to"}},
                {"code": 90018, "title": "Call has already ended"},
                "not an object",
            ]
        }
    )
    first, second = parse_error_body(422, "Unprocessable Entity", body)
    assert first == TelnyxErrorItem("10015", "Invalid value", "bad to")
    assert first.source == {"pointer": "/to"}
    assert second == TelnyxErrorItem("90018", "Call has already ended")
    assert second.source is None


def test_non_telnyx_bodies_become_one_synthetic_error() -> None:
    html = b"<html>" + b"x" * 500
    [item] = parse_error_body(502, "Bad Gateway", html)
    assert (item.code, item.title, item.detail) == ("http_502", "Bad Gateway", html[:200].decode())
    assert parse_error_body(500, "", b"") == (TelnyxErrorItem("http_500", "HTTP 500"),)
    assert parse_error_body(400, "Bad Request", b'{"errors": []}')[0].code == "http_400"
    assert parse_error_body(400, "Bad Request", b'["errors"]')[0].code == "http_400"


def test_api_error_maps_to_carrier_error_with_telnyx_errors() -> None:
    error = TelnyxApiError(
        422, (TelnyxErrorItem("10015", "Invalid value", "bad to", {"pointer": "/to"}),), "dial"
    )
    assert str(error) == "Telnyx dial failed with HTTP 422: Invalid value"
    app_error = error.to_app_error()
    assert (app_error.code, app_error.http_status) == ("CARRIER_ERROR", 502)
    assert app_error.details == {
        "telnyx_errors": [{"code": "10015", "title": "Invalid value", "detail": "bad to"}]
    }
    assert TelnyxApiError(500, (), "hangup").to_app_error().details == {"telnyx_errors": []}
    assert str(TelnyxApiError(500, (), "hangup")) == "Telnyx hangup failed with HTTP 500"


def test_call_already_ended_is_an_api_error() -> None:
    error = CallAlreadyEnded(422, (TelnyxErrorItem("90018", "Call has already ended"),), "record_start")
    assert isinstance(error, TelnyxApiError)
    assert error.to_app_error().code == "CARRIER_ERROR"


def test_rate_limited_maps_to_service_unavailable() -> None:
    error = TelnyxRateLimited(account="platform", category="number_search", retry_after_s=0.2, operation="x")
    app_error = error.to_app_error()
    assert (app_error.code, app_error.http_status) == ("SERVICE_UNAVAILABLE", 503)
    assert app_error.headers == {"Retry-After": "1"}
    assert (error.account, error.category, error.operation) == ("platform", "number_search", "x")


def test_transport_error_maps_to_carrier_error() -> None:
    error = TelnyxTransportError("dial", httpx.ConnectTimeout("timed out"))
    assert str(error) == "Telnyx dial failed: ConnectTimeout"
    app_error = error.to_app_error()
    assert app_error.code == "CARRIER_ERROR"
    assert app_error.details == {"telnyx_errors": [], "transport": "ConnectTimeout"}


def test_base_error_maps_to_carrier_error() -> None:
    error = TelnyxError("Telnyx dial failed", "dial")
    assert (error.to_app_error().code, error.operation) == ("CARRIER_ERROR", "dial")
