"""``PathwayFolderRepo`` on a migrated database: create, list, update, delete with re-parenting, moves, and
org isolation (specs/12 §6; Decision P07-T09c)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from pathway_rows import GRAPH
from tenant_rows import OrgFactory, open_database

from va_db.models import Pathway, PathwayFolder
from va_db.repositories import NotFoundError, PathwayFolderRepo, PathwayRepo
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def folder(
    database: Database, org_id: uuid.UUID, name: str, parent_id: uuid.UUID | None = None
) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        return (await PathwayFolderRepo(session, org_id).create(name=name, parent_id=parent_id)).id


async def pathway(database: Database, org_id: uuid.UUID, folder_id: uuid.UUID | None = None) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        row, _ = await PathwayRepo(session, org_id).create(
            name="Orders", description="", folder_id=folder_id, graph=GRAPH
        )
        return row.id


async def folder_of(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID) -> uuid.UUID | None:
    async with database.tenant_session(org_id) as session:
        row = await session.get(Pathway, pathway_id)
        assert row is not None
        return row.folder_id


async def test_create_list_update_and_isolation(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    beta = await folder(database, org_id, "Beta")
    alpha = await folder(database, org_id, "Alpha", beta)
    foreign = await folder(database, other, "Foreign")
    async with database.tenant_session(org_id) as session:
        repo = PathwayFolderRepo(session, org_id)
        assert [(row.name, row.parent_id) for row in await repo.list_folders()] == [
            ("Alpha", beta),
            ("Beta", None),
        ]
        row = await repo.get(alpha)
        await repo.update(row, {"name": "Alpha 2", "parent_id": None})
        assert (row.name, row.parent_id) == ("Alpha 2", None)
        with pytest.raises(NotFoundError, match="Folder not found"):
            await repo.get(foreign)
        tree = await repo.lock_tree()
        assert alpha in tree
        assert foreign not in tree


async def test_delete_moves_pathways_and_sub_folders_to_the_parent(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    root = await folder(database, org_id, "Root")
    middle = await folder(database, org_id, "Middle", root)
    child = await folder(database, org_id, "Child", middle)
    grandchild = await folder(database, org_id, "Grandchild", child)
    live = await pathway(database, org_id, middle)
    deleted = await pathway(database, org_id, middle)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        await repo.soft_delete(await repo.get(deleted))
    async with database.tenant_session(org_id) as session:
        folders = PathwayFolderRepo(session, org_id)
        assert await folders.delete(await folders.get(middle)) == (2, 1)
    assert await folder_of(database, org_id, live) == root
    assert await folder_of(database, org_id, deleted) == root
    async with database.tenant_session(org_id) as session:
        parents = {row.id: row.parent_id for row in await PathwayFolderRepo(session, org_id).list_folders()}
    assert parents == {root: None, child: root, grandchild: child}


async def test_delete_of_a_root_folder_moves_everything_to_the_root(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    root = await folder(database, org_id, "Root")
    child = await folder(database, org_id, "Child", root)
    inside = await pathway(database, org_id, root)
    async with database.tenant_session(org_id) as session:
        repo = PathwayFolderRepo(session, org_id)
        assert await repo.delete(await repo.get(root)) == (1, 1)
        assert (await repo.get(child)).parent_id is None
    assert await folder_of(database, org_id, inside) is None


async def test_move_pathways_counts_changes_and_ignores_other_orgs(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    target = await folder(database, org_id, "Target")
    first = await pathway(database, org_id)
    already = await pathway(database, org_id, target)
    foreign = await pathway(database, other)
    async with database.tenant_session(org_id) as session:
        repo = PathwayFolderRepo(session, org_id)
        assert await repo.live_pathway_ids([first, already, foreign]) == {first, already}
        assert await repo.live_pathway_ids([]) == set()
        assert await repo.move_pathways(target, [first, already, foreign]) == 1
        assert await repo.move_pathways(target, []) == 0
    assert await folder_of(database, org_id, first) == target
    assert await folder_of(database, other, foreign) is None


async def test_folder_rows_hold_the_org(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    folder_id = await folder(database, org_id, "Mine")
    async with database.tenant_session(org_id) as session:
        row = await session.get(PathwayFolder, folder_id)
        assert row is not None
        assert row.org_id == org_id
