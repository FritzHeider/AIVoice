"""Request and response bodies of the pathway versions API (specs/12-pathways-api.md §4; legacy bodies)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_api.routers.pathways_schemas import MAX_NAME_LENGTH, ValidationView
from va_db.models import Pathway, PathwayVersion
from va_db.models.pathways import VersionStatus
from va_db.repositories import PinEnvironment

__all__ = [
    "PathwayVersions",
    "Promoted",
    "PublishBody",
    "Published",
    "RevertBody",
    "Reverted",
    "VersionItem",
    "VersionSave",
    "VersionSaved",
    "version_item",
]

_HASH: Final = "4f1c2a9e0b7d3c5a8e6f1b2d4c9a7e3f5b8d1c6a2e9f4b7d0c3a5e8f1b2d4c6a"
_USER_ID: Final = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71"
_DRAFT_ITEM: Final[dict[str, JsonValue]] = {
    "version_number": 5,
    "name": None,
    "status": "draft",
    "is_production": False,
    "is_staging": False,
    "graph_hash": _HASH,
    "published_at": None,
    "published_by": None,
    "created_at": "2026-11-03T10:42:10Z",
}
_PRODUCTION_ITEM: Final[dict[str, JsonValue]] = {
    "version_number": 4,
    "name": "v4 - new greeting",
    "status": "published",
    "is_production": True,
    "is_staging": True,
    "graph_hash": _HASH,
    "published_at": "2026-11-03T10:40:00Z",
    "published_by": _USER_ID,
    "created_at": "2026-11-03T09:15:00Z",
}

VersionName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)
]
VersionNumber = Annotated[int, Field(ge=1)]


class VersionItem(BaseModel):
    """One version of a pathway with its environment pins."""

    model_config = ConfigDict(json_schema_extra={"examples": [_DRAFT_ITEM]})

    version_number: int
    name: str | None
    status: VersionStatus = Field(description="`draft`, `published`, or `archived` (saved, unpublished).")
    is_production: bool
    is_staging: bool
    graph_hash: str
    published_at: datetime | None = Field(description="Time of the version's first publish.")
    published_by: uuid.UUID | None = Field(description="User who first published it (null for keyless).")
    created_at: datetime


def version_item(pathway: Pathway, version: PathwayVersion) -> VersionItem:
    """The list item of ``version``."""
    return VersionItem(
        version_number=version.version_number,
        name=version.name,
        status=version.status,
        is_production=pathway.production_version == version.version_number,
        is_staging=pathway.staging_version == version.version_number,
        graph_hash=version.graph_hash,
        published_at=version.published_at,
        published_by=version.published_by,
        created_at=version.created_at,
    )


class PathwayVersions(BaseModel):
    """``GET /v1/pathway/{pathway_id}/versions``: every version, newest first."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "versions": [_DRAFT_ITEM, _PRODUCTION_ITEM]}]}
    )

    status: Literal["success"] = "success"
    versions: list[VersionItem]


class VersionSave(BaseModel):
    """``POST /v1/pathway/{pathway_id}/version``: freeze the draft with an optional label."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{"name": "v4 - new greeting"}]})

    name: VersionName | None = Field(
        default=None, description=f"Label of the saved version (1-{MAX_NAME_LENGTH} characters)."
    )


class VersionSaved(BaseModel):
    """``POST /v1/pathway/{pathway_id}/version`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "saved_version": 4, "draft_version": 5}]}
    )

    status: Literal["success"] = "success"
    saved_version: int
    draft_version: int


class PublishBody(BaseModel):
    """``POST /v1/pathway/{pathway_id}/publish``."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [{"version_number": 4, "environment": "production", "run_regression": False}]
        },
    )

    version_number: VersionNumber | None = Field(
        default=None, description="Version to publish; default the draft."
    )
    environment: PinEnvironment = Field(default="production", description="Environment pin to move.")
    run_regression: bool | None = Field(
        default=None, description="Run the pathway's unit tests before publishing (specs/12 §4 step 2)."
    )


class Published(BaseModel):
    """``POST /v1/pathway/{pathway_id}/publish`` result."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "success", "published_version": 4, "environment": "production", "draft_version": 5}
            ]
        }
    )

    status: Literal["success"] = "success"
    published_version: int
    environment: PinEnvironment
    draft_version: int


class Promoted(BaseModel):
    """``POST /v1/pathway/{pathway_id}/promote`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "production_version": 4}]}
    )

    status: Literal["success"] = "success"
    production_version: int


class RevertBody(BaseModel):
    """``POST /v1/pathway/{pathway_id}/revert``."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{"version_number": 2}]})

    version_number: VersionNumber = Field(description="Version whose graph replaces the draft's.")


class Reverted(BaseModel):
    """``POST /v1/pathway/{pathway_id}/revert`` result with the save-mode validation of the new draft."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "draft_version": 5,
                    "graph_hash": _HASH,
                    "validation": {"valid": True, "errors": [], "warnings": []},
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    draft_version: int
    graph_hash: str
    validation: ValidationView
