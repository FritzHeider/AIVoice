"""openai SDK exceptions map to gateway errors (Decision P03-T13a rule 8)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import openai
import pytest

from va_common.time import FrozenClock
from va_llm import (
    LlmConnectionError,
    LlmError,
    LlmHttpError,
    LlmProtocolError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from va_llm.errors import map_openai_error

# openai >= 3 is typed against httpx2 but takes httpx objects at runtime (as the gateway's clients do).
REQUEST: Any = httpx.Request("POST", "http://llm.test/v1/chat/completions")
NOW = FrozenClock().now()


def _status(status: int, headers: dict[str, str] | None = None) -> openai.APIStatusError:
    response: Any = httpx.Response(
        status, headers=headers, request=REQUEST, json={"error": {"message": "nope"}}
    )
    return openai.APIStatusError("Error code: %d - nope" % status, response=response, body=None)  # noqa: UP031


def _map(exc: openai.OpenAIError) -> LlmError:
    return map_openai_error(exc, provider_id="platform", model="m", now=NOW)


def test_timeouts_and_connection_errors() -> None:
    timeout = _map(openai.APITimeoutError(request=REQUEST))
    assert isinstance(timeout, LlmTimeoutError)
    assert (timeout.error_kind, timeout.breaker_failure) == ("timeout", True)
    connection = _map(openai.APIConnectionError(request=REQUEST))
    assert isinstance(connection, LlmConnectionError)
    assert (connection.error_kind, connection.breaker_failure) == ("http", True)


@pytest.mark.parametrize(
    ("status", "breaker_failure"), [(500, True), (503, True), (400, False), (401, False)]
)
def test_status_errors(status: int, breaker_failure: bool) -> None:
    error = _map(_status(status))
    assert isinstance(error, LlmHttpError)
    assert not isinstance(error, LlmRateLimitedError)
    assert (error.status, error.error_kind, error.breaker_failure) == (status, "http", breaker_failure)
    assert "nope" in error.message
    assert error.message.startswith("platform m:")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "1"}, 1.0),
        ({"retry-after": "abc"}, None),
        ({}, None),
        ({"retry-after": (NOW + timedelta(seconds=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")}, 3.0),
    ],
)
def test_rate_limits_parse_retry_after(headers: dict[str, str], expected: float | None) -> None:
    error = _map(_status(429, headers))
    assert isinstance(error, LlmRateLimitedError)
    assert (error.status, error.retry_after_s, error.breaker_failure) == (429, expected, False)


def test_other_sdk_errors_are_protocol_errors() -> None:
    response: Any = httpx.Response(200, request=REQUEST)
    validation = openai.APIResponseValidationError(response=response, body=None)
    error = _map(validation)
    assert isinstance(error, LlmProtocolError)
    assert (error.error_kind, error.breaker_failure) == ("parse", False)


def test_unavailable_takes_its_kind_from_the_last_error() -> None:
    last = LlmTimeoutError("t", provider_id="p", model="m")
    error = LlmUnavailableError("exhausted", provider_id="p", model="m", last=last)
    assert (error.error_kind, error.reason, error.last) == ("timeout", "exhausted", last)
    assert str(error) == "LLM unavailable (exhausted): t"
    bare = LlmUnavailableError("breaker_open", provider_id="p", model="m", last=None)
    assert (bare.error_kind, str(bare)) == ("http", "LLM unavailable (breaker_open)")


def test_api_rendering_is_upstream_timeout() -> None:
    app_error = LlmTimeoutError("t", provider_id="p", model="m").to_app_error()
    assert (app_error.code, app_error.http_status) == ("UPSTREAM_TIMEOUT", 504)
