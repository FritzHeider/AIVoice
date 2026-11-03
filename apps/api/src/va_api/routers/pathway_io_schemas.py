"""Request and response bodies of pathway import, export, and duplicate (specs/12-pathways-api.md §4, §5).

Rules: specs/12 §6 Decision P07-T09c. Graph parts are plain JSON here; the engine schema checks them in
:mod:`va_api.services.pathways`, so a schema problem names its graph path (``nodes[3].data.name``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue

from va_api.routers.pathways_schemas import (
    Description,
    GraphConfig,
    GraphEdges,
    GraphNodes,
    Name,
    ValidationView,
)
from va_api.services.pathway_io import EXPORT_FORMAT, PathwayExport

__all__ = [
    "DuplicateBody",
    "ExportBody",
    "ImportBody",
    "Imported",
    "export_body",
]

_PATHWAY_ID: Final = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70"
_START: Final[dict[str, JsonValue]] = {
    "id": "start",
    "type": "Default",
    "data": {"name": "Start", "isStart": True, "prompt": "Greet the caller."},
}
_KB_WARNING: Final[dict[str, JsonValue]] = {
    "code": "PATHWAY_KB_MISSING",
    "message": "Knowledge base 'KB-0193c5d27a417c3e9b0d5f2e8a1c4d72' not found.",
    "node_id": "menu",
}
_GLOBAL_CONFIG: Final[dict[str, JsonValue]] = {
    "globalPrompt": "You are Ava, the friendly host of Bella Pasta."
}

PositiveVersion = Annotated[int, Field(ge=1)]


class DuplicateBody(BaseModel):
    """``POST /v1/pathway/{pathway_id}/duplicate``: the copy's name and the version to copy."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"examples": [{"name": "Copy of Bella Pasta Orders", "version": "production"}]},
    )

    name: Name | None = Field(default=None, description="Name of the copy; default `Copy of <name>`.")
    version: PositiveVersion | Literal["draft", "production", "staging"] = Field(
        default="draft", description="Version number, `draft` (default), `production`, or `staging`."
    )


class ExportBody(BaseModel):
    """``GET /v1/pathway/{pathway_id}/export``: the ``voiceagents.pathway.v1`` document."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "format": EXPORT_FORMAT,
                    "name": "Bella Pasta Orders",
                    "description": "Takes phone orders",
                    "nodes": [_START],
                    "edges": [],
                    "global_config": _GLOBAL_CONFIG,
                    "exported_at": "2026-11-03T10:42:10Z",
                }
            ]
        }
    )

    format: Literal["voiceagents.pathway.v1"] = EXPORT_FORMAT
    name: str
    description: str
    nodes: list[Any]
    edges: list[Any]
    global_config: dict[str, Any]
    exported_at: datetime


def export_body(document: PathwayExport) -> ExportBody:
    """The response body of an export document."""
    return ExportBody(
        name=document.name,
        description=document.description,
        nodes=document.nodes,
        edges=document.edges,
        global_config=document.global_config,
        exported_at=document.exported_at,
    )


class ImportBody(BaseModel):
    """``POST /v1/pathway/import``: our export (``format`` set) or a Bland export (no ``format``).

    Other keys (``exported_at``, Bland fields) are ignored; Bland's ``globalConfig`` is read as
    ``global_config``.
    """

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "format": EXPORT_FORMAT,
                    "name": "Bella Pasta Orders",
                    "description": "Takes phone orders",
                    "nodes": [_START],
                    "edges": [],
                    "global_config": _GLOBAL_CONFIG,
                }
            ]
        },
    )

    format: Literal["voiceagents.pathway.v1"] | None = Field(
        default=None, description="`voiceagents.pathway.v1`; omit for a Bland export."
    )
    name: Name | None = Field(default=None, description="Pathway name; default `Imported pathway`.")
    description: Description | None = None
    nodes: GraphNodes = Field(description="Every node; Bland node type aliases are accepted (specs/11 §3.3).")
    edges: GraphEdges = Field(description="Every edge of the graph.")
    global_config: GraphConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("global_config", "globalConfig"),
        description="Graph-wide settings (`globalConfig` in Bland exports).",
    )
    folder_id: uuid.UUID | None = Field(default=None, description="Folder of the new pathway; null = root.")


class Imported(BaseModel):
    """``POST /v1/pathway/import`` result: like create, plus the validation and missing-reference warnings."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "pathway_id": _PATHWAY_ID,
                    "version_number": 1,
                    "validation": {"valid": True, "errors": [], "warnings": [_KB_WARNING]},
                    "warnings": [_KB_WARNING],
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    pathway_id: uuid.UUID
    version_number: int
    validation: ValidationView
    warnings: list[dict[str, str]] = Field(
        description="`PATHWAY_TOOL_MISSING`/`PATHWAY_KB_MISSING` warnings: references kept in the graph."
    )
