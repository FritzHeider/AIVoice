"""``FolderTree``: depth, height, children, and containment of an org's folder tree (specs/12 §6;
Decision P07-T09c item 5)."""

from __future__ import annotations

import itertools
import uuid

from va_db.repositories import MAX_FOLDER_DEPTH, FolderTree


def chain(length: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=index + 1) for index in range(length)]


def chain_tree(ids: list[uuid.UUID]) -> FolderTree:
    parents: dict[uuid.UUID, uuid.UUID | None] = {ids[0]: None}
    parents.update({child: parent for parent, child in itertools.pairwise(ids)})
    return FolderTree(parents)


def test_depth_counts_folders_from_the_root() -> None:
    ids = chain(5)
    tree = chain_tree(ids)
    assert [tree.depth(folder_id) for folder_id in ids] == [1, 2, 3, 4, 5]
    assert tree.depth(uuid.UUID(int=99)) == 0
    assert MAX_FOLDER_DEPTH == 5


def test_height_counts_the_levels_of_a_subtree() -> None:
    ids = chain(4)
    sibling = uuid.UUID(int=50)
    tree = FolderTree({**dict(zip(ids, [None, *ids[:-1]], strict=True)), sibling: ids[0]})
    assert [tree.height(folder_id) for folder_id in ids] == [4, 3, 2, 1]
    assert tree.height(sibling) == 1
    assert tree.height(uuid.UUID(int=99)) == 0
    assert sorted(tree.children(ids[0])) == sorted([ids[1], sibling])
    assert tree.children(ids[-1]) == []


def test_is_within_matches_the_folder_and_its_descendants_only() -> None:
    ids = chain(3)
    other = uuid.UUID(int=60)
    tree = FolderTree({ids[0]: None, ids[1]: ids[0], ids[2]: ids[1], other: None})
    assert tree.is_within(ids[2], ids[0])
    assert tree.is_within(ids[0], ids[0])
    assert not tree.is_within(ids[0], ids[2])
    assert not tree.is_within(other, ids[0])
    assert ids[1] in tree
    assert uuid.UUID(int=99) not in tree


def test_a_cycle_in_stored_data_never_loops() -> None:
    first, second = chain(2)
    tree = FolderTree({first: second, second: first})
    assert tree.depth(first) == 2
    assert tree.height(first) == 2
    assert tree.is_within(first, second)
