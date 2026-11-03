"""Pathway folders: list, get, create, update, delete, move pathways (specs/12-pathways-api.md §6).

Rules: specs/12 §6 Decision P07-T09c item 5. Every function runs in the caller's ``tenant_session``.
Structural writes (a create with a parent, a parent change, a delete) lock the org's whole folder tree first
(:meth:`va_db.repositories.PathwayFolderRepo.lock_tree`) and check depth and cycles on the locked tree, so
concurrent moves cannot build a cycle or nest deeper than :data:`~va_db.repositories.MAX_FOLDER_DEPTH`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit, changed
from va_api.auth.context import RequestContext
from va_api.services.pathways import FOLDER_NOT_FOUND_MESSAGE
from va_common.errors import AppError
from va_db.models import PathwayFolder
from va_db.repositories import MAX_FOLDER_DEPTH, PATHWAY_RESOURCE, FolderTree, PathwayFolderRepo

__all__ = [
    "CYCLE_MESSAGE",
    "FOLDER_DELETED_MESSAGE",
    "TOO_DEEP_MESSAGE",
    "create_folder",
    "delete_folder",
    "get_folder",
    "list_folders",
    "move_pathways",
    "update_folder",
]

FOLDER_DELETED_MESSAGE: Final = "Folder deleted."
TOO_DEEP_MESSAGE: Final = f"Folders nest at most {MAX_FOLDER_DEPTH} levels deep."
CYCLE_MESSAGE: Final = "A folder cannot move into itself or one of its sub-folders."
_PARENT_FIELD: Final = "parent_id"
_RESOURCE: Final = "pathway_folder"


def _summary(folder: PathwayFolder) -> dict[str, Any]:
    return {
        "name": folder.name,
        "parent_id": None if folder.parent_id is None else str(folder.parent_id),
    }


def _check_parent(tree: FolderTree, parent_id: uuid.UUID, *, levels: int) -> None:
    """``parent_id`` exists and can take a subtree of ``levels`` levels below it."""
    if parent_id not in tree:
        raise AppError("NOT_FOUND", FOLDER_NOT_FOUND_MESSAGE, field=_PARENT_FIELD)
    if tree.depth(parent_id) + levels > MAX_FOLDER_DEPTH:
        raise AppError("INVALID_INPUT", TOO_DEEP_MESSAGE, field=_PARENT_FIELD)


async def list_folders(session: AsyncSession, ctx: RequestContext) -> list[PathwayFolder]:
    """Every folder of the org ordered by name, then id."""
    return await PathwayFolderRepo(session, ctx.org_id).list_folders()


async def get_folder(session: AsyncSession, ctx: RequestContext, folder_id: uuid.UUID) -> PathwayFolder:
    """One folder of the org; ``NOT_FOUND`` "Folder not found." otherwise."""
    return await PathwayFolderRepo(session, ctx.org_id).get(folder_id)


async def create_folder(
    session: AsyncSession, ctx: RequestContext, *, name: str, parent_id: uuid.UUID | None
) -> PathwayFolder:
    """Create a folder at the root or under ``parent_id`` (depth checked on the locked tree)."""
    repo = PathwayFolderRepo(session, ctx.org_id)
    if parent_id is not None:
        _check_parent(await repo.lock_tree(), parent_id, levels=1)
    folder = await repo.create(name=name, parent_id=parent_id)
    await audit(session, ctx, "pathway_folder.create", _RESOURCE, folder.id, after=_summary(folder))
    return folder


async def update_folder(
    session: AsyncSession, ctx: RequestContext, folder_id: uuid.UUID, values: Mapping[str, Any]
) -> PathwayFolder:
    """Rename and/or move a folder (``parent_id`` ``None`` = root); a no-op writes no audit row."""
    repo = PathwayFolderRepo(session, ctx.org_id)
    if _PARENT_FIELD in values:
        tree = await repo.lock_tree()
        folder = await repo.get(folder_id)
        parent_id: uuid.UUID | None = values[_PARENT_FIELD]
        if parent_id is not None and parent_id != folder.parent_id:
            if parent_id in tree and tree.is_within(parent_id, folder.id):
                raise AppError("INVALID_INPUT", CYCLE_MESSAGE, field=_PARENT_FIELD)
            _check_parent(tree, parent_id, levels=tree.height(folder.id))
    else:
        folder = await repo.get(folder_id)
    before = _summary(folder)
    updates = {key: value for key, value in values.items() if getattr(folder, key) != value}
    if not updates:
        return folder
    await repo.update(folder, updates)
    old, new = changed(before, _summary(folder))
    await audit(session, ctx, "pathway_folder.update", _RESOURCE, folder.id, before=old, after=new)
    return folder


async def delete_folder(session: AsyncSession, ctx: RequestContext, folder_id: uuid.UUID) -> tuple[int, int]:
    """Delete a folder; its pathways and sub-folders move to its parent. ``(pathways, folders)`` moved."""
    repo = PathwayFolderRepo(session, ctx.org_id)
    await repo.lock_tree()
    folder = await repo.get(folder_id)
    before = _summary(folder)
    moved_pathways, moved_folders = await repo.delete(folder)
    after = {"moved_pathways": moved_pathways, "moved_folders": moved_folders}
    await audit(session, ctx, "pathway_folder.delete", _RESOURCE, folder_id, before=before, after=after)
    return moved_pathways, moved_folders


async def move_pathways(
    session: AsyncSession, ctx: RequestContext, folder_id: uuid.UUID, pathway_ids: Sequence[uuid.UUID]
) -> int:
    """Put live pathways into the folder; any unknown id is ``NOT_FOUND`` and nothing moves."""
    repo = PathwayFolderRepo(session, ctx.org_id)
    folder = await repo.get(folder_id, key_share=True)
    live = await repo.live_pathway_ids(pathway_ids)
    for index, pathway_id in enumerate(pathway_ids):
        if pathway_id not in live:
            raise AppError("NOT_FOUND", f"{PATHWAY_RESOURCE} not found.", field=f"pathway_ids[{index}]")
    moved = await repo.move_pathways(folder.id, pathway_ids)
    if moved:
        after = {"pathway_ids": [str(pathway_id) for pathway_id in pathway_ids], "moved": moved}
        await audit(session, ctx, "pathway_folder.move", _RESOURCE, folder.id, after=after)
    return moved
