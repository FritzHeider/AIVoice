"""Tenant data access for pathway folders (specs/12-pathways-api.md §6; specs/03 §5).

Rules: specs/12 §6 Decision P07-T09c. :class:`FolderTree` is the pure view of an org's folder tree used for
the depth and cycle checks. :meth:`PathwayFolderRepo.lock_tree` locks every folder row of the org (id order,
so concurrent structural writes serialise without deadlocks) and then re-reads the tree, so a folder
committed while the lock was awaited is part of the checked tree. Deleting a folder re-parents its pathways
and direct sub-folders before the row goes, so the ``ON DELETE CASCADE`` of ``parent_id`` never removes
nested folders.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Pathway, PathwayFolder
from va_db.repositories.base import NotFoundError

__all__ = ["FOLDER_RESOURCE", "MAX_FOLDER_DEPTH", "FolderTree", "PathwayFolderRepo"]

FOLDER_RESOURCE: Final = "Folder"
"""The resource name of the folder ``NOT_FOUND`` message ("Folder not found.")."""
MAX_FOLDER_DEPTH: Final = 5
"""Folders on the path from the root to the deepest folder (a root folder has depth 1)."""


class FolderTree:
    """An org's folders as ``id → parent_id`` (``None`` = a root folder)."""

    def __init__(self, parents: Mapping[uuid.UUID, uuid.UUID | None]) -> None:
        self._parents = dict(parents)
        children: dict[uuid.UUID, list[uuid.UUID]] = {}
        for folder_id, parent_id in self._parents.items():
            if parent_id is not None:
                children.setdefault(parent_id, []).append(folder_id)
        self._children = children

    def __contains__(self, folder_id: object) -> bool:
        return folder_id in self._parents

    def _ancestry(self, folder_id: uuid.UUID) -> list[uuid.UUID]:
        """``folder_id`` and its ancestors up to the root (stops at a repeated id: stored data is acyclic)."""
        path: list[uuid.UUID] = []
        current: uuid.UUID | None = folder_id
        while current is not None and current in self._parents and current not in path:
            path.append(current)
            current = self._parents[current]
        return path

    def depth(self, folder_id: uuid.UUID) -> int:
        """Folders from the root to ``folder_id`` inclusive (0 for an unknown id)."""
        return len(self._ancestry(folder_id))

    def children(self, folder_id: uuid.UUID) -> list[uuid.UUID]:
        """The direct sub-folders of ``folder_id``."""
        return list(self._children.get(folder_id, ()))

    def height(self, folder_id: uuid.UUID) -> int:
        """Levels of the subtree rooted at ``folder_id``, itself included (1 for a leaf, 0 if unknown)."""
        if folder_id not in self._parents:
            return 0
        levels = 0
        frontier = [folder_id]
        seen: set[uuid.UUID] = set()
        while frontier:
            levels += 1
            seen.update(frontier)
            frontier = [child for parent in frontier for child in self.children(parent) if child not in seen]
        return levels

    def is_within(self, candidate: uuid.UUID, root: uuid.UUID) -> bool:
        """Whether ``candidate`` is ``root`` or one of its sub-folders (at any depth)."""
        return root in self._ancestry(candidate)


class PathwayFolderRepo:
    """Pathway folders of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def list_folders(self) -> list[PathwayFolder]:
        """Every folder of the org ordered by name, then id."""
        statement = (
            select(PathwayFolder)
            .where(PathwayFolder.org_id == self.org_id)
            .order_by(PathwayFolder.name, PathwayFolder.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def get(self, folder_id: uuid.UUID, *, key_share: bool = False) -> PathwayFolder:
        """The org's folder (``FOR KEY SHARE`` with ``key_share``: it cannot be deleted meanwhile)."""
        statement = select(PathwayFolder).where(
            PathwayFolder.org_id == self.org_id, PathwayFolder.id == folder_id
        )
        if key_share:
            statement = statement.with_for_update(key_share=True)
        folder = await self.session.scalar(statement)
        if folder is None:
            raise NotFoundError(FOLDER_RESOURCE)
        return folder

    async def lock_tree(self) -> FolderTree:
        """Lock every folder row of the org, then read the tree as committed now."""
        locked = (
            select(PathwayFolder.id)
            .where(PathwayFolder.org_id == self.org_id)
            .order_by(PathwayFolder.id)
            .with_for_update()
        )
        await self.session.execute(locked)
        current = select(PathwayFolder.id, PathwayFolder.parent_id).where(PathwayFolder.org_id == self.org_id)
        rows = (await self.session.execute(current)).all()
        return FolderTree({row_id: parent_id for row_id, parent_id in rows})  # noqa: C416 - Row pairs

    async def create(self, *, name: str, parent_id: uuid.UUID | None) -> PathwayFolder:
        """Insert a folder."""
        folder = PathwayFolder(org_id=self.org_id, name=name, parent_id=parent_id)
        self.session.add(folder)
        await self.session.flush()
        await self.session.refresh(folder)
        return folder

    async def update(self, folder: PathwayFolder, values: Mapping[str, Any]) -> None:
        """Apply column ``values`` (``name``, ``parent_id``) to ``folder``."""
        for key, value in values.items():
            setattr(folder, key, value)
        await self.session.flush()
        await self.session.refresh(folder)

    async def delete(self, folder: PathwayFolder) -> tuple[int, int]:
        """Move its pathways and sub-folders to its parent, then delete it; ``(pathways, folders)`` moved."""
        pathways = await self.session.execute(
            update(Pathway)
            .where(Pathway.org_id == self.org_id, Pathway.folder_id == folder.id)
            .values(folder_id=folder.parent_id)
            .returning(Pathway.id)
        )
        moved_pathways = len(pathways.all())
        folders = await self.session.execute(
            update(PathwayFolder)
            .where(PathwayFolder.org_id == self.org_id, PathwayFolder.parent_id == folder.id)
            .values(parent_id=folder.parent_id)
            .returning(PathwayFolder.id)
        )
        moved_folders = len(folders.all())
        await self.session.delete(folder)
        await self.session.flush()
        return moved_pathways, moved_folders

    async def live_pathway_ids(self, pathway_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        """The ids among ``pathway_ids`` that are live pathways of the org."""
        if not pathway_ids:
            return set()
        statement = select(Pathway.id).where(
            Pathway.org_id == self.org_id, Pathway.deleted_at.is_(None), Pathway.id.in_(list(pathway_ids))
        )
        return set((await self.session.scalars(statement)).all())

    async def move_pathways(self, folder_id: uuid.UUID, pathway_ids: Collection[uuid.UUID]) -> int:
        """Put the live pathways ``pathway_ids`` into ``folder_id``; the number whose folder changed."""
        if not pathway_ids:
            return 0
        result = await self.session.execute(
            update(Pathway)
            .where(
                Pathway.org_id == self.org_id,
                Pathway.deleted_at.is_(None),
                Pathway.id.in_(list(pathway_ids)),
                Pathway.folder_id.is_distinct_from(folder_id),
            )
            .values(folder_id=folder_id)
            .returning(Pathway.id)
        )
        return len(result.all())
