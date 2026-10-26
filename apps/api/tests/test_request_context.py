"""Request id, log binding, and security headers (specs/31 §1, specs/27 §1, specs/28 §5)."""

from __future__ import annotations

import io
import json
import re
import uuid
from typing import Any

import httpx
import pytest
import structlog
from api_helpers import make_client, make_settings, sample_app
from fastapi.routing import APIRoute
from sample_routes import ENVELOPE_PREFIX, LEGACY_PREFIX
from starlette.types import ASGIApp, Receive, Scope, Send

from va_api.middleware import merge_headers, new_request_id, resolve_request_id
from va_common.logging import configure_logging

GENERATED = r"^req_[0-9a-f]{32}$"


async def fetch(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> httpx.Response:
    async with make_client(sample_app()) as client:
        return await client.request(method, path, headers=headers)


def test_generated_id_format() -> None:
    value = new_request_id()
    assert value.startswith("req_")
    assert len(value) == 36
    assert uuid.UUID(hex=value[4:]).version == 7


async def test_generated_id_on_response() -> None:
    response = await fetch("/v1/health")
    assert re.fullmatch(GENERATED, response.headers["x-request-id"])


@pytest.mark.parametrize("incoming", ["abc", "A.b_c:d-1", "x" * 128, "req_0192f3a4b5c67d8e"])
async def test_valid_incoming_id_echoed(incoming: str) -> None:
    response = await fetch("/v1/health", headers={"X-Request-Id": incoming})
    assert response.headers["x-request-id"] == incoming


@pytest.mark.parametrize("incoming", ["x" * 129, "has space", "<script>", ""])
async def test_invalid_incoming_id_replaced(incoming: str) -> None:
    response = await fetch("/v1/health", headers={"X-Request-Id": incoming})
    assert response.headers["x-request-id"] != incoming
    assert response.headers["x-request-id"].startswith("req_")


def test_resolve_request_id_none_generates() -> None:
    assert resolve_request_id(None).startswith("req_")


async def test_ids_unique_per_request() -> None:
    async with make_client(sample_app()) as client:
        ids = {(await client.get("/v1/health")).headers["x-request-id"] for _ in range(20)}
    assert len(ids) == 20


@pytest.mark.parametrize(
    "path", [f"{LEGACY_PREFIX}/app_error", f"{ENVELOPE_PREFIX}/http/409", "/nope", f"{LEGACY_PREFIX}/boom"]
)
async def test_error_body_request_id_matches_header(path: str) -> None:
    response = await fetch(path, headers={"X-Request-Id": "trace-me"})
    assert response.json()["request_id"] == "trace-me"
    assert response.headers["x-request-id"] == "trace-me"


async def test_request_id_bound_in_logs_and_cleared() -> None:
    stream = io.StringIO()
    configure_logging(make_settings(log_format="json"), stream=stream, environ={}, pod="p")
    await fetch("/v1/health", headers={"X-Request-Id": "bound-1"})
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    access = next(line for line in lines if line["event"] == "http.request")
    assert access["request_id"] == "bound-1"
    assert access["path"] == "/v1/health"
    assert access["status"] == 200
    assert access["method"] == "GET"
    assert access["duration_ms"] >= 0
    assert "request_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/v1/health", {}, 200),
        ("GET", "/nope", {}, 404),
        ("GET", f"{LEGACY_PREFIX}/boom", {}, 500),
        (
            "OPTIONS",
            "/v1/health",
            {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
            200,
        ),
    ],
)
async def test_security_headers_on_all_responses(
    method: str, path: str, headers: dict[str, str], status: int
) -> None:
    response = await fetch(path, method, headers)
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize(
    "path", ["/v1/health", "/nope", f"{LEGACY_PREFIX}/boom", f"{LEGACY_PREFIX}/app_error"]
)
async def test_no_duplicate_headers(path: str) -> None:
    response = await fetch(path)
    for name in ("x-request-id", "x-content-type-options", "referrer-policy"):
        assert len(response.headers.get_list(name)) == 1, name


def test_merge_headers_keeps_existing_values() -> None:
    merged = merge_headers([(b"X-Request-Id", b"route")], [("x-request-id", "mw"), ("referrer-policy", "no")])
    assert merged == [(b"X-Request-Id", b"route"), (b"referrer-policy", b"no")]


async def test_scope_route_is_set_by_fastapi() -> None:
    seen: dict[str, Any] = {}

    class Capture:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)
            if scope["type"] == "http":
                seen["route"] = scope.get("route")

    app = sample_app()
    app.add_middleware(Capture)
    async with make_client(app) as client:
        await client.get(f"{ENVELOPE_PREFIX}/query")
    route = seen["route"]
    assert isinstance(route, APIRoute)
    assert route.path_format == f"{ENVELOPE_PREFIX}/query"
    assert route.openapi_extra is not None
    assert route.openapi_extra["x-envelope"] == "envelope"


async def test_non_http_scope_passes_through() -> None:
    app = sample_app()
    async with make_client(app) as client:  # lifespan scopes traverse both middlewares untouched
        assert (await client.get("/healthz")).status_code == 200
