"""Request and response bodies of the pathway folders API (specs/12-pathways-api.md §6; legacy bodies).

Rules: specs/12 §6 Decision P07-T09c item 5.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from va_api.routers.pathways_schemas import Name
from va_db.models import PathwayFolder

__all__ = [
    "MAX_MOVE_IDS",
    "FolderCreate",
    "FolderDeleted",
    "FolderList",
    "FolderMove",
    "FolderMoved",
    "FolderOne",
    "FolderUpdate",
    "FolderView",
    "folder_view",
]

MAX_MOVE_IDS: Final = 100

_FOLDER: Final[dict[str, JsonValue]] = {
    "folder_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d73",
    "name": "Restaurants",
    "parent_id": None,
    "created_at": "2026-11-03T09:15:00Z",
    "updated_at": "2026-11-03T09:15:00Z",
}


def _unique(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(ids)) != len(ids):
        msg = "pathway_ids must not repeat an id"
        raise ValueError(msg)
    return ids


PathwayIds = Annotated[list[uuid.UUID], Field(max_length=MAX_MOVE_IDS), AfterValidator(_unique)]


class FolderView(BaseModel):
    """A pathway folder; ``parent_id`` null = a root folder."""

    model_config = ConfigDict(json_schema_extra={"examples": [_FOLDER]})

    folder_id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


def folder_view(folder: PathwayFolder) -> FolderView:
    """The response view of a folder row."""
    return FolderView(
        folder_id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


class FolderList(BaseModel):
    """``GET /v1/pathway/folders`` result: every folder ordered by name."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "folders": [_FOLDER]}]})

    status: Literal["success"] = "success"
    folders: list[FolderView]


class FolderOne(BaseModel):
    """One folder (get, create, update)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "folder": _FOLDER}]})

    status: Literal["success"] = "success"
    folder: FolderView


class FolderCreate(BaseModel):
    """``POST /v1/pathway/folders``."""

    model_config = ConfigDict(
        extra="ignore", json_schema_extra={"examples": [{"name": "Restaurants", "parent_id": None}]}
    )

    name: Name = Field(description="Folder name (1-200 characters).")
    parent_id: uuid.UUID | None = Field(default=None, description="Parent folder; null = root.")


class FolderUpdate(BaseModel):
    """``PATCH /v1/pathway/folders/{folder_id}``: only sent fields apply; ``parent_id: null`` = root."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{"name": "Restaurants"}]})

    name: Name | None = Field(default=None, description="New name (1-200 characters).")
    parent_id: uuid.UUID | None = Field(default=None, description="New parent folder; null = root.")


class FolderDeleted(BaseModel):
    """``DELETE /v1/pathway/folders/{folder_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "success", "message": "Folder deleted.", "moved_pathways": 3, "moved_folders": 1}
            ]
        }
    )

    status: Literal["success"] = "success"
    message: str
    moved_pathways: int = Field(description="Pathways moved to the parent folder (or the root).")
    moved_folders: int = Field(description="Sub-folders moved to the parent folder (or the root).")


class FolderMove(BaseModel):
    """``POST /v1/pathway/folders/{folder_id}/move``."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"examples": [{"pathway_ids": ["0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70"]}]},
    )

    pathway_ids: PathwayIds = Field(description=f"Live pathways to move (at most {MAX_MOVE_IDS}, unique).")


class FolderMoved(BaseModel):
    """``POST /v1/pathway/folders/{folder_id}/move`` result."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "moved": 1}]})

    status: Literal["success"] = "success"
    moved: int = Field(description="Pathways whose folder changed.")
