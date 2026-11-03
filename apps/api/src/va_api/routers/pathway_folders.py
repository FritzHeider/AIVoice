"""Pathway folders under ``/v1/pathway/folders`` (specs/12-pathways-api.md §6; legacy bodies).

Rules: specs/12 §6 Decision P07-T09c. :data:`va_api.main.ROUTERS` lists this router before the pathways
router, so the literal ``folders`` segment is never taken by the ``/v1/pathway/{pathway_id}`` routes.
Reads need ``pathways:read``; writes need ``pathways:write`` and an editing role.
"""

from __future__ import annotations

import uuid

from fastapi import Request

from va_api.audit import audited
from va_api.auth.session_auth import database_of
from va_api.envelopes import legacy_router
from va_api.routers.pathway_folders_schemas import (
    FolderCreate,
    FolderDeleted,
    FolderList,
    FolderMove,
    FolderMoved,
    FolderOne,
    FolderUpdate,
    folder_view,
)
from va_api.routers.pathways import EDITOR, READ_SCOPE, WRITE_SCOPE, ReadPathways, WritePathways
from va_api.services.pathway_folders import (
    FOLDER_DELETED_MESSAGE,
    create_folder,
    delete_folder,
    get_folder,
    list_folders,
    move_pathways,
    update_folder,
)

__all__ = [
    "pathway_folders_router",
    "pathways_folders_create",
    "pathways_folders_delete",
    "pathways_folders_get",
    "pathways_folders_list",
    "pathways_folders_move",
    "pathways_folders_update",
]

pathway_folders_router = legacy_router(prefix="/v1/pathway/folders", tags=["Pathways"])


@pathway_folders_router.get("", operation_id="pathways_folders_list", openapi_extra=READ_SCOPE)
async def pathways_folders_list(request: Request, ctx: ReadPathways) -> FolderList:
    """Every pathway folder of the org, ordered by name."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        return FolderList(folders=[folder_view(folder) for folder in await list_folders(session, ctx)])


@pathway_folders_router.post(
    "",
    operation_id="pathways_folders_create",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway_folder.create")},
)
async def pathways_folders_create(body: FolderCreate, request: Request, ctx: WritePathways) -> FolderOne:
    """Create a folder at the root or under ``parent_id`` (at most 5 levels deep)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        folder = await create_folder(session, ctx, name=body.name, parent_id=body.parent_id)
        return FolderOne(folder=folder_view(folder))


@pathway_folders_router.get("/{folder_id}", operation_id="pathways_folders_get", openapi_extra=READ_SCOPE)
async def pathways_folders_get(folder_id: uuid.UUID, request: Request, ctx: ReadPathways) -> FolderOne:
    """One pathway folder."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        return FolderOne(folder=folder_view(await get_folder(session, ctx, folder_id)))


@pathway_folders_router.patch(
    "/{folder_id}",
    operation_id="pathways_folders_update",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway_folder.update")},
)
async def pathways_folders_update(
    folder_id: uuid.UUID, body: FolderUpdate, request: Request, ctx: WritePathways
) -> FolderOne:
    """Rename a folder or move it under another folder (``parent_id: null`` = root)."""
    values = {key: getattr(body, key) for key in body.model_fields_set}
    if values.get("name", "") is None:
        del values["name"]
    async with database_of(request).tenant_session(ctx.org_id) as session:
        folder = await update_folder(session, ctx, folder_id, values)
        return FolderOne(folder=folder_view(folder))


@pathway_folders_router.delete(
    "/{folder_id}",
    operation_id="pathways_folders_delete",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway_folder.delete")},
)
async def pathways_folders_delete(
    folder_id: uuid.UUID, request: Request, ctx: WritePathways
) -> FolderDeleted:
    """Delete a folder; its pathways and sub-folders move to its parent (or the root)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        moved_pathways, moved_folders = await delete_folder(session, ctx, folder_id)
    return FolderDeleted(
        message=FOLDER_DELETED_MESSAGE, moved_pathways=moved_pathways, moved_folders=moved_folders
    )


@pathway_folders_router.post(
    "/{folder_id}/move",
    operation_id="pathways_folders_move",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway_folder.move")},
)
async def pathways_folders_move(
    folder_id: uuid.UUID, body: FolderMove, request: Request, ctx: WritePathways
) -> FolderMoved:
    """Put live pathways into the folder; nothing moves when any id is unknown."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        moved = await move_pathways(session, ctx, folder_id, body.pathway_ids)
    return FolderMoved(moved=moved)
