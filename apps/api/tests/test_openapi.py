"""OpenAPI document rules (specs/26-sdks-cli-mcp-docs.md §1)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from api_helpers import make_settings, sample_app
from openapi_spec_validator import validate

import check_openapi_drift
from va_api import __version__
from va_api.main import create_app
from va_api.openapi import TAG_NAMES, servers

ROOT = Path(__file__).resolve().parents[3]
SPEC_26 = ROOT / "specs" / "26-sdks-cli-mcp-docs.md"
COMMITTED = ROOT / "openapi" / "openapi.json"
OPERATION_ID = re.compile(r"^[a-z]+(_[a-z0-9]+)+$")
METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


def resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    resolved: dict[str, Any] = document["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return resolved


def test_openapi_31_and_info() -> None:
    document = create_app(make_settings(), init_observability=False).openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["title"] == "VoiceAgents API"
    assert document["info"]["version"] == __version__
    assert document["info"]["license"] == {"name": "Proprietary"}
    assert document["info"]["contact"]["email"] == "support@voiceagents.ai"


def test_document_is_cached() -> None:
    app = create_app(make_settings(), init_observability=False)
    assert app.openapi() is app.openapi()


def test_servers_default_single_local() -> None:
    assert servers(make_settings()) == [{"url": "http://localhost:8000", "description": "Local"}]
    assert servers(make_settings(openapi_public_server_url="http://localhost:8000/")) == [
        {"url": "http://localhost:8000", "description": "Local"}
    ]


def test_servers_production_then_local() -> None:
    settings = make_settings(openapi_public_server_url="https://api.voiceagents.ai")
    assert servers(settings) == [
        {"url": "https://api.voiceagents.ai", "description": "Production"},
        {"url": "http://localhost:8000", "description": "Local"},
    ]
    document = create_app(settings, init_observability=False).openapi()
    assert document["servers"][0]["url"] == "https://api.voiceagents.ai"


def spec_tags() -> list[str]:
    text = SPEC_26.read_text(encoding="utf-8")
    match = re.search(r"\*\*tags\*\*: one per resource family, in this order:(.*?)\.\n", text, re.DOTALL)
    assert match is not None
    return [tag.strip() for tag in " ".join(match.group(1).split()).split(",")]


def test_tags_order_matches_spec() -> None:
    assert list(TAG_NAMES) == [*spec_tags(), "Health"]
    document = create_app(make_settings(), init_observability=False).openapi()
    assert [tag["name"] for tag in document["tags"]] == list(TAG_NAMES)


@pytest.mark.parametrize("with_samples", [False, True])
def test_operations_contract(with_samples: bool) -> None:
    app = sample_app() if with_samples else create_app(make_settings(), init_observability=False)
    document = app.openapi()
    ids = [operation["operationId"] for _, _, operation in operations(document)]
    assert len(ids) == len(set(ids))
    for path, method, operation in operations(document):
        where = f"{method.upper()} {path}"
        assert OPERATION_ID.fullmatch(operation["operationId"]), where
        assert len(operation["tags"]) == 1, where
        assert operation["tags"][0] in TAG_NAMES, where
        assert operation["x-envelope"] in {"legacy", "envelope"}, where
        assert operation["x-scope"], where
        success = [code for code in operation["responses"] if code.startswith("2")]
        assert success, where
        for code in success:
            schema = operation["responses"][code]["content"]["application/json"]["schema"]
            assert resolve(document, schema).get("examples"), where


def test_internal_routes_are_not_in_the_public_document() -> None:
    app = create_app(make_settings(), init_observability=False)
    paths = {getattr(route, "path", "") for router in app.state.routers for route in router.routes}
    internal = {path for path in paths if path.startswith("/internal/")}
    assert {"/internal/auth/login", "/internal/auth/session"} <= internal
    assert not [path for path in app.openapi()["paths"] if path.startswith("/internal/")]


def test_no_422_and_400_refs_envelope_component() -> None:
    document = sample_app().openapi()
    for path, method, operation in operations(document):
        responses = operation["responses"]
        assert "422" not in responses, f"{method} {path}"
        component = "ErrorLegacy" if operation["x-envelope"] == "legacy" else "ErrorEnvelope"
        ref = {"$ref": f"#/components/schemas/{component}"}
        assert responses["default"]["content"]["application/json"]["schema"] == ref
        if "400" in responses:
            assert responses["400"]["content"]["application/json"]["schema"] == ref
    body_op = document["paths"]["/v1/knowledge/_t/body"]["post"]
    assert body_op["responses"]["400"]["description"] == "Invalid input"
    assert "HTTPValidationError" not in json.dumps(document)
    assert "ValidationError" not in document["components"]["schemas"]
    validate(document)


def test_error_components_and_security_scheme() -> None:
    document = create_app(make_settings(), init_observability=False).openapi()
    schemas = document["components"]["schemas"]
    assert {"ErrorItem", "ErrorLegacy", "ErrorEnvelope"} <= set(schemas)
    assert schemas["ErrorLegacy"]["properties"]["errors"]["items"] == {
        "$ref": "#/components/schemas/ErrorItem"
    }
    assert schemas["ErrorLegacy"]["required"] == ["status", "message", "errors", "request_id"]
    assert schemas["ErrorEnvelope"]["required"] == ["data", "errors", "request_id"]
    assert document["components"]["securitySchemes"] == {"ApiKeyAuth": {"type": "http", "scheme": "bearer"}}
    assert "HTTPValidationError" not in schemas


def test_validation_schemas_kept_when_still_referenced() -> None:
    from fastapi import FastAPI  # noqa: PLC0415

    from va_api.openapi import build_openapi  # noqa: PLC0415

    app = FastAPI()

    @app.get(
        "/x", responses={409: {"model": dict[str, Any], "description": "c"}}, openapi_extra={"x-ref": True}
    )
    async def x(q: int) -> dict[str, int]:
        return {"q": q}

    app.router.routes[-1].responses[418] = {  # type: ignore[attr-defined]
        "description": "teapot",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}},
    }
    document = build_openapi(app, make_settings())
    assert "HTTPValidationError" in document["components"]["schemas"]
    assert "ValidationError" in document["components"]["schemas"]


def test_committed_document_is_current() -> None:
    document = create_app(make_settings(), init_observability=False).openapi()
    assert check_openapi_drift.render(document) == COMMITTED.read_text(encoding="utf-8")


def test_committed_document_is_valid_31() -> None:
    document = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert document["openapi"] == "3.1.0"
    validate(document)
