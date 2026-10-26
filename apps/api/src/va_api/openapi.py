"""OpenAPI 3.1 document of the public API (specs/26-sdks-cli-mcp-docs.md §1).

FastAPI's generated document is post-processed: the shared error components and the ``ApiKeyAuth`` security
scheme are added, FastAPI's 422 responses become 400, and every operation gets a ``default`` error response,
both referencing ``ErrorLegacy`` or ``ErrorEnvelope`` by the operation's ``x-envelope``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from va_api import __version__
from va_api.envelopes import ENVELOPE_KEY, Envelope
from va_api.errors import ErrorEnvelope, ErrorItem, ErrorLegacy
from va_api.settings import ApiSettings

__all__ = ["LOCAL_SERVER_URL", "TAGS", "TAG_NAMES", "TITLE", "build_openapi", "servers"]

TITLE: Final = "VoiceAgents API"
LOCAL_SERVER_URL: Final = "http://localhost:8000"
# specs/26 §1 tag order, then Health (GET /v1/health).
TAG_NAMES: Final[tuple[str, ...]] = (
    "Calls",
    "Batches",
    "Pathways",
    "Personas",
    "Agents",
    "Experiments",
    "Tools",
    "Knowledge",
    "Voices",
    "Numbers",
    "Inbound",
    "Accounts",
    "SMS",
    "Widgets",
    "Memory",
    "Contacts",
    "Analysis",
    "Citations",
    "Evals",
    "Dispositions",
    "Compliance",
    "Webhooks",
    "Billing",
    "Usage",
    "Analytics",
    "Org",
    "API Keys",
    "Audit Logs",
    "Prompts",
    "MCP",
    "Agent Onboarding",
    "Health",
)
TAGS: Final[list[dict[str, Any]]] = [{"name": name} for name in TAG_NAMES]
CONTACT: Final = {
    "name": "VoiceAgents Support",
    "url": "https://voiceagents.ai/support",
    "email": "support@voiceagents.ai",
}
LICENSE: Final = {"name": "Proprietary"}
SECURITY_SCHEMES: Final = {"ApiKeyAuth": {"type": "http", "scheme": "bearer"}}
ERROR_MODELS: Final[tuple[type[BaseModel], ...]] = (ErrorItem, ErrorLegacy, ErrorEnvelope)
_REF_TEMPLATE: Final = "#/components/schemas/{model}"
_FASTAPI_VALIDATION_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")
_HTTP_METHODS: Final = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_ERROR_COMPONENT: Final[Mapping[Envelope, str]] = {
    Envelope.LEGACY: "ErrorLegacy",
    Envelope.ENVELOPE: "ErrorEnvelope",
}


def servers(settings: ApiSettings) -> list[dict[str, str]]:
    """Production (``OPENAPI_PUBLIC_SERVER_URL``) then Local; a single Local entry when they are equal."""
    local = {"url": LOCAL_SERVER_URL, "description": "Local"}
    public = settings.openapi_public_server_url.rstrip("/")
    if public == LOCAL_SERVER_URL:
        return [local]
    return [{"url": public, "description": "Production"}, local]


def _error_schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    for model in ERROR_MODELS:
        schema = model.model_json_schema(ref_template=_REF_TEMPLATE)
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    return schemas


def _error_response(description: str, component: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{component}"}}},
    }


def _operations(document: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for path_item in document.get("paths", {}).values():
        for method, operation in path_item.items():
            if method in _HTTP_METHODS:
                yield operation


def _rewrite_error_responses(operation: dict[str, Any]) -> None:
    component = _ERROR_COMPONENT[Envelope(operation.get(ENVELOPE_KEY, Envelope.LEGACY.value))]
    responses: dict[str, Any] = operation.setdefault("responses", {})
    if responses.pop("422", None) is not None:
        responses["400"] = _error_response("Invalid input", component)
    responses["default"] = _error_response("Error", component)


def _drop_unreferenced_validation_schemas(document: dict[str, Any]) -> None:
    schemas: dict[str, Any] = document["components"]["schemas"]
    for name in _FASTAPI_VALIDATION_SCHEMAS:
        if name in schemas:
            remaining = {key: value for key, value in schemas.items() if key != name}
            others = json.dumps({**document, "components": {**document["components"], "schemas": remaining}})
            if f'"#/components/schemas/{name}"' not in others:
                del schemas[name]


def build_openapi(app: FastAPI, settings: ApiSettings) -> dict[str, Any]:
    """Generate (once, then cached on ``app.openapi_schema``) the post-processed public document."""
    if app.openapi_schema is not None:
        return app.openapi_schema
    document = get_openapi(
        title=TITLE,
        version=__version__,
        openapi_version="3.1.0",
        routes=app.routes,
        tags=TAGS,
        contact=CONTACT,
        license_info=LICENSE,
        servers=servers(settings),
    )
    components: dict[str, Any] = document.setdefault("components", {})
    components.setdefault("schemas", {}).update(_error_schemas())
    components["securitySchemes"] = dict(SECURITY_SCHEMES)
    for operation in _operations(document):
        _rewrite_error_responses(operation)
    _drop_unreferenced_validation_schemas(document)
    app.openapi_schema = document
    return document
