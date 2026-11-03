"""Request and response bodies of the pathways API core (specs/12-pathways-api.md §3; legacy bodies).

Graph parts (``nodes``, ``edges``, ``global_config``) are plain JSON here; the engine schema
(specs/11-pathways-engine.md §3) checks them in :mod:`va_api.services.pathways`, so a schema problem names its
graph path (``nodes[3].data.name``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_db.models import Pathway, PathwayVersion
from va_db.models.pathways import VersionStatus
from va_db.repositories import PathwayListRow
from va_engine.validation import IssueItem, ValidationReport

__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "PathwayCreate",
    "PathwayCreated",
    "PathwayDeleted",
    "PathwayDetail",
    "PathwayList",
    "PathwayListItem",
    "PathwayUpdate",
    "PathwayUpdated",
    "PathwayValidate",
    "PathwayValidated",
    "ValidationView",
    "pathway_detail",
    "pathway_list_item",
    "validation_view",
]

MAX_NAME_LENGTH: Final = 200
MAX_DESCRIPTION_LENGTH: Final = 2_000

_PATHWAY_ID: Final = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70"
_HASH: Final = "4f1c2a9e0b7d3c5a8e6f1b2d4c9a7e3f5b8d1c6a2e9f4b7d0c3a5e8f1b2d4c6a"
_START: Final[dict[str, JsonValue]] = {
    "id": "start",
    "type": "Default",
    "data": {"name": "Start", "isStart": True, "prompt": "Greet the caller."},
}
_VALIDATION_EXAMPLE: Final[dict[str, JsonValue]] = {
    "valid": True,
    "errors": [],
    "warnings": [
        {
            "code": "PATHWAY_UNREACHABLE",
            "message": "Node 'Hours' cannot be reached from the start node.",
            "node_id": "node_hours",
        }
    ],
}
_LIST_ITEM_EXAMPLE: Final[dict[str, JsonValue]] = {
    "pathway_id": _PATHWAY_ID,
    "name": "Bella Pasta Orders",
    "description": "Takes phone orders",
    "folder_id": None,
    "production_version": 3,
    "staging_version": 4,
    "draft_version": 5,
    "latest_version": 5,
    "node_count": 12,
    "created_at": "2026-11-03T09:15:00Z",
    "updated_at": "2026-11-03T10:42:10Z",
}

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)]
Description = Annotated[str, StringConstraints(max_length=MAX_DESCRIPTION_LENGTH)]
GraphNodes = list[dict[str, JsonValue]]
GraphEdges = list[dict[str, JsonValue]]
GraphConfig = dict[str, JsonValue]

_NODES_DESCRIPTION: Final = "Every node of the graph (specs/11 §3.1); replaces the stored array."
_EDGES_DESCRIPTION: Final = "Every edge of the graph (specs/11 §3.3); replaces the stored array."
_CONFIG_DESCRIPTION: Final = "Graph-wide settings (specs/11 §3.4); replaces the stored object."


class ValidationView(BaseModel):
    """The validation result (specs/11 §11): ``valid`` is false only when an error-severity issue exists."""

    model_config = ConfigDict(json_schema_extra={"examples": [_VALIDATION_EXAMPLE]})

    valid: bool
    errors: list[dict[str, str]] = Field(description="Issues with `code`, `message`, `node_id`?, `edge_id`?.")
    warnings: list[dict[str, str]]


def _issue(item: IssueItem) -> dict[str, str]:
    return {key: str(value) for key, value in item.items()}


def validation_view(report: ValidationReport) -> ValidationView:
    """The response view of a validation report."""
    return ValidationView(
        valid=report["valid"],
        errors=[_issue(item) for item in report["errors"]],
        warnings=[_issue(item) for item in report["warnings"]],
    )


class PathwayCreate(BaseModel):
    """``POST /v1/pathway/create``; empty ``nodes`` and ``edges`` create a single start node."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "name": "Bella Pasta Orders",
                    "description": "Takes phone orders",
                    "nodes": [],
                    "edges": [],
                    "global_config": {"globalPrompt": "You are Ava, the friendly host of Bella Pasta."},
                    "folder_id": None,
                }
            ]
        },
    )

    name: Name = Field(description=f"Pathway name (1-{MAX_NAME_LENGTH} characters).")
    description: Description | None = Field(default=None, description="Free text; default empty.")
    nodes: GraphNodes | None = Field(default=None, description=_NODES_DESCRIPTION)
    edges: GraphEdges | None = Field(default=None, description=_EDGES_DESCRIPTION)
    global_config: GraphConfig | None = Field(default=None, description=_CONFIG_DESCRIPTION)
    folder_id: uuid.UUID | None = Field(default=None, description="Folder of the pathway; null = root.")


class PathwayCreated(BaseModel):
    """``POST /v1/pathway/create`` result."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"status": "success", "pathway_id": _PATHWAY_ID, "version_number": 1}]
        }
    )

    status: Literal["success"] = "success"
    pathway_id: uuid.UUID
    version_number: int = Field(description="The draft version number (1).")


class PathwayListItem(BaseModel):
    """One live pathway; ``node_count`` counts the draft's nodes."""

    model_config = ConfigDict(json_schema_extra={"examples": [_LIST_ITEM_EXAMPLE]})

    pathway_id: uuid.UUID
    name: str
    description: str
    folder_id: uuid.UUID | None
    production_version: int | None
    staging_version: int | None
    draft_version: int
    latest_version: int
    node_count: int
    created_at: datetime
    updated_at: datetime


def pathway_list_item(row: PathwayListRow) -> PathwayListItem:
    """The list item of a repository row."""
    pathway = row.pathway
    return PathwayListItem(
        pathway_id=pathway.id,
        name=pathway.name,
        description=pathway.description,
        folder_id=pathway.folder_id,
        production_version=pathway.production_version,
        staging_version=pathway.staging_version,
        draft_version=pathway.draft_version,
        latest_version=pathway.latest_version,
        node_count=row.node_count,
        created_at=pathway.created_at,
        updated_at=pathway.updated_at,
    )


class PathwayList(BaseModel):
    """``GET /v1/pathway`` result: newest first; ``next_cursor`` is null on the last page."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"status": "success", "pathways": [_LIST_ITEM_EXAMPLE], "next_cursor": None}]
        }
    )

    status: Literal["success"] = "success"
    pathways: list[PathwayListItem]
    next_cursor: str | None


class PathwayDetail(BaseModel):
    """``GET /v1/pathway/{pathway_id}`` result: the pathway with one version's graph."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "pathway_id": _PATHWAY_ID,
                    "name": "Bella Pasta Orders",
                    "description": "Takes phone orders",
                    "folder_id": None,
                    "version_number": 5,
                    "version_status": "draft",
                    "production_version": 3,
                    "staging_version": 4,
                    "nodes": [_START],
                    "edges": [],
                    "global_config": {"globalPrompt": "You are Ava, the friendly host of Bella Pasta."},
                    "graph_hash": _HASH,
                    "created_at": "2026-11-03T09:15:00Z",
                    "updated_at": "2026-11-03T10:42:10Z",
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    pathway_id: uuid.UUID
    name: str
    description: str
    folder_id: uuid.UUID | None
    version_number: int
    version_status: VersionStatus
    production_version: int | None
    staging_version: int | None
    nodes: list[Any]
    edges: list[Any]
    global_config: dict[str, Any]
    graph_hash: str
    created_at: datetime
    updated_at: datetime


def pathway_detail(pathway: Pathway, version: PathwayVersion) -> PathwayDetail:
    """The detail body of ``pathway`` at ``version``."""
    return PathwayDetail(
        pathway_id=pathway.id,
        name=pathway.name,
        description=pathway.description,
        folder_id=pathway.folder_id,
        version_number=version.version_number,
        version_status=version.status,
        production_version=pathway.production_version,
        staging_version=pathway.staging_version,
        nodes=list(version.nodes),
        edges=list(version.edges),
        global_config=dict(version.global_config),
        graph_hash=version.graph_hash,
        created_at=pathway.created_at,
        updated_at=pathway.updated_at,
    )


class PathwayUpdate(BaseModel):
    """``POST /v1/pathway/{pathway_id}``: every field optional; a sent graph part replaces the stored one."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"examples": [{"name": "Bella Pasta Orders", "description": "Takes phone orders"}]},
    )

    name: Name | None = Field(default=None, description=f"Pathway name (1-{MAX_NAME_LENGTH} characters).")
    description: Description | None = None
    folder_id: uuid.UUID | None = Field(default=None, description="Move to this folder; null = root.")
    nodes: GraphNodes | None = Field(default=None, description=_NODES_DESCRIPTION)
    edges: GraphEdges | None = Field(default=None, description=_EDGES_DESCRIPTION)
    global_config: GraphConfig | None = Field(default=None, description=_CONFIG_DESCRIPTION)
    expected_graph_hash: str | None = Field(
        default=None,
        description="Optimistic concurrency: the graph_hash of the draft as loaded; a stale hash is 409.",
    )


class PathwayUpdated(BaseModel):
    """``POST /v1/pathway/{pathway_id}`` result with the save-mode validation of the new draft."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "pathway_id": _PATHWAY_ID,
                    "version_number": 5,
                    "graph_hash": _HASH,
                    "validation": _VALIDATION_EXAMPLE,
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    pathway_id: uuid.UUID
    version_number: int
    graph_hash: str
    validation: ValidationView


class PathwayValidate(BaseModel):
    """``POST /v1/pathway/{pathway_id}/validate``: graph parts not sent come from the draft."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{"mode": "publish"}]})

    nodes: GraphNodes | None = Field(default=None, description=_NODES_DESCRIPTION)
    edges: GraphEdges | None = Field(default=None, description=_EDGES_DESCRIPTION)
    global_config: GraphConfig | None = Field(default=None, description=_CONFIG_DESCRIPTION)
    mode: Literal["save", "publish"] = Field(default="publish", description="Severity table to apply.")


class PathwayValidated(BaseModel):
    """``POST /v1/pathway/{pathway_id}/validate`` result (issues never fail the request)."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "validation": _VALIDATION_EXAMPLE}]}
    )

    status: Literal["success"] = "success"
    validation: ValidationView


class PathwayDeleted(BaseModel):
    """``DELETE /v1/pathway/{pathway_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "message": "Pathway deleted."}]}
    )

    status: Literal["success"] = "success"
    message: str
