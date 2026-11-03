"""Pathway folders: CRUD, depth and cycle checks, delete re-parenting, moves, audit (specs/12 §6;
Decision P07-T09c). Roles live in ``test_pathways_permissions.py``."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import Api, star_api
from session_helpers import SessionContainer, audit_rows, error_of

from va_api.services.pathway_folders import CYCLE_MESSAGE, TOO_DEEP_MESSAGE
from va_common.ids import new_id

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


async def nested(api: Api, depth: int) -> list[str]:
    """A chain of ``depth`` folders, root first."""
    ids: list[str] = []
    for level in range(depth):
        ids.append(await api.folder_created(f"Level {level + 1}", ids[-1] if ids else None))
    return ids


async def folder_in(api: Api, pathway_id: str) -> str | None:
    folder_id: str | None = (await api.get(pathway_id)).json()["folder_id"]
    return folder_id


async def test_create_get_list_and_rename(api: Api, identity: IdentityDb) -> None:
    root = await api.create_folder("  Restaurants  ")
    assert root.status_code == 200, root.text
    folder = root.json()["folder"]
    assert set(folder) == {"folder_id", "name", "parent_id", "created_at", "updated_at"}
    assert (folder["name"], folder["parent_id"]) == ("Restaurants", None)
    child_id = await api.folder_created("Brunch", folder["folder_id"])

    listed = await api.folders()
    assert [(item["name"], item["parent_id"]) for item in listed.json()["folders"]] == [
        ("Brunch", folder["folder_id"]),
        ("Restaurants", None),
    ]
    assert (await api.folder(child_id)).json()["folder"]["name"] == "Brunch"

    renamed = await api.patch_folder(child_id, name="Breakfast")
    assert renamed.json()["folder"]["name"] == "Breakfast"
    assert (await api.patch_folder(child_id, name="Breakfast")).status_code == 200
    rows = await audit_rows(identity, api.org_id, "pathway_folder.update")
    assert [(row.changes["before"], row.changes["after"]) for row in rows] == [
        ({"name": "Brunch"}, {"name": "Breakfast"})
    ]
    created = await audit_rows(identity, api.org_id, "pathway_folder.create")
    assert [row.changes["after"]["name"] for row in created] == ["Restaurants", "Brunch"]


async def test_unknown_folders_and_parents_are_not_found(api: Api) -> None:
    missing = str(new_id())
    assert error_of(await api.folder(missing)) == (404, "NOT_FOUND", None)
    assert error_of(await api.create_folder("Orphan", missing)) == (404, "NOT_FOUND", "parent_id")
    folder_id = await api.folder_created("Mine")
    assert error_of(await api.patch_folder(folder_id, parent_id=missing)) == (404, "NOT_FOUND", "parent_id")
    assert error_of(await api.patch_folder(missing, name="x")) == (404, "NOT_FOUND", None)
    assert error_of(await api.delete_folder(missing)) == (404, "NOT_FOUND", None)
    assert error_of(await api.move(missing, [])) == (404, "NOT_FOUND", None)
    assert error_of(await api.create_folder("   "))[:2] == (400, "INVALID_INPUT")


async def test_folders_nest_at_most_five_levels(api: Api) -> None:
    chain = await nested(api, 5)
    too_deep = await api.create_folder("Level 6", chain[-1])
    assert error_of(too_deep) == (400, "INVALID_INPUT", "parent_id")
    assert (
        too_deep.json()["errors"][0]["message"] == TOO_DEEP_MESSAGE == "Folders nest at most 5 levels deep."
    )

    # A three-level subtree fits under a depth-2 folder but not under a depth-3 one.
    subtree = await nested(api, 3)
    assert error_of(await api.patch_folder(subtree[0], parent_id=chain[2]))[:3] == (
        400,
        "INVALID_INPUT",
        "parent_id",
    )
    moved = await api.patch_folder(subtree[0], parent_id=chain[1])
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder"]["parent_id"] == chain[1]


async def test_a_folder_cannot_move_into_itself_or_a_descendant(api: Api) -> None:
    chain = await nested(api, 3)
    for target in (chain[0], chain[2]):
        response = await api.patch_folder(chain[0], parent_id=target)
        assert error_of(response) == (400, "INVALID_INPUT", "parent_id")
        assert response.json()["errors"][0]["message"] == CYCLE_MESSAGE
    to_root = await api.patch_folder(chain[2], parent_id=None)
    assert to_root.json()["folder"]["parent_id"] is None


async def test_concurrent_moves_cannot_build_a_cycle(api: Api) -> None:
    first = await api.folder_created("A")
    second = await api.folder_created("B")
    responses = await asyncio.gather(
        api.patch_folder(first, parent_id=second), api.patch_folder(second, parent_id=first)
    )
    assert sorted(response.status_code for response in responses) == [200, 400]
    parents = {item["folder_id"]: item["parent_id"] for item in (await api.folders()).json()["folders"]}
    assert None in (parents[first], parents[second])


async def test_delete_moves_pathways_and_sub_folders_to_the_parent(api: Api, identity: IdentityDb) -> None:
    root, middle, child = await nested(api, 3)
    first = await api.created(folder_id=middle)
    second = await api.created(folder_id=middle)
    deleted = await api.delete_folder(middle)
    assert deleted.json() == {
        "status": "success",
        "message": "Folder deleted.",
        "moved_pathways": 2,
        "moved_folders": 1,
    }
    assert [await folder_in(api, first), await folder_in(api, second)] == [root, root]
    assert (await api.folder(child)).json()["folder"]["parent_id"] == root
    (row,) = await audit_rows(identity, api.org_id, "pathway_folder.delete")
    assert row.resource_id == middle
    assert row.changes["after"] == {"moved_pathways": 2, "moved_folders": 1}

    root_listed = (await api.list(folder_id=root)).json()["pathways"]
    assert {item["pathway_id"] for item in root_listed} == {first, second}


async def test_move_pathways_into_a_folder(api: Api, identity: IdentityDb) -> None:
    target = await api.folder_created("Target")
    first = await api.created()
    second = await api.created()
    moved = await api.move(target, [first, second])
    assert moved.json() == {"status": "success", "moved": 2}
    assert {item["pathway_id"] for item in (await api.list(folder_id=target)).json()["pathways"]} == {
        first,
        second,
    }
    assert (await api.move(target, [first])).json()["moved"] == 0
    assert (await api.move(target, [])).json()["moved"] == 0
    rows = await audit_rows(identity, api.org_id, "pathway_folder.move")
    assert [row.changes["after"]["moved"] for row in rows] == [2]


async def test_move_rejects_unknown_deleted_and_foreign_pathways(
    api: Api, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    other = await star_api(client, identity, container)
    target = await api.folder_created("Target")
    live = await api.created()
    gone = await api.created()
    assert (await api.delete(gone)).status_code == 200
    foreign = await other.created()
    for stranger in (str(new_id()), gone, foreign):
        response = await api.move(target, [live, stranger])
        assert error_of(response) == (404, "NOT_FOUND", "pathway_ids[1]")
        assert response.json()["errors"][0]["message"] == "Pathway not found."
    assert await folder_in(api, live) is None
    assert error_of(await api.move(target, [live, live]))[:2] == (400, "INVALID_INPUT")


async def test_routes_do_not_collide_with_pathway_ids(api: Api) -> None:
    listed = await api.folders()
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"status": "success", "folders": []}
