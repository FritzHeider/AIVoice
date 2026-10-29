"""The §3.2 RBAC matrix in code against the markdown table (specs/04-auth-tenancy-rbac.md §3.2, §3.3)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from va_api.auth import rbac_matrix
from va_api.auth.permissions import PERMISSIONS, ROLE_PERMISSIONS
from va_api.auth.rbac import ROLE_SCOPES
from va_api.auth.rbac_matrix import (
    MATRIX,
    ROLES,
    ROW_GRANTS,
    Cell,
    Matrix,
    cell,
    role_permissions,
    role_scopes,
)
from va_api.auth.scopes import SCOPES
from va_db.models import MemberRole

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"


def _cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _spec_table() -> tuple[list[str], list[list[str]]]:
    lines = SPEC_04.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("### 3.2"))
    table: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    header, _separator, *rows = table
    return _cells(header), [_cells(row) for row in rows]


SPEC_HEADER, SPEC_ROWS = _spec_table()


def test_header_roles_follow_the_spec() -> None:
    assert tuple(SPEC_HEADER[1:]) == ROLES


def test_row_labels_and_order_follow_the_spec() -> None:
    assert len(SPEC_ROWS) == 47
    assert list(MATRIX) == [row[0] for row in SPEC_ROWS]


@pytest.mark.parametrize("row", SPEC_ROWS, ids=[row[0] for row in SPEC_ROWS])
def test_cells_match_the_spec(row: list[str]) -> None:
    label, *cells = row
    assert {role: cell(label, role) for role in ROLES} == dict(zip(ROLES, cells, strict=True))


def test_every_row_has_a_grant() -> None:
    assert list(ROW_GRANTS) == list(MATRIX)


def test_every_permission_is_granted_by_a_known_row() -> None:
    granted = {permission for grant in ROW_GRANTS.values() for permission in grant.permissions}
    assert granted == PERMISSIONS


def test_every_grant_resource_has_a_scope() -> None:
    for label, grant in ROW_GRANTS.items():
        if grant.resource is not None:
            assert {f"{grant.resource}:read", f"{grant.resource}:write"} & SCOPES, label


@pytest.mark.parametrize("role", ROLES)
def test_role_maps_are_derived_from_the_matrix(role: MemberRole) -> None:
    assert role_scopes(role) == ROLE_SCOPES[role]
    assert role_permissions(role) == ROLE_PERMISSIONS[role]


def _flipped(label: str, role: MemberRole, value: Cell) -> Matrix:
    changed: dict[MemberRole, Cell] = {**MATRIX[label], role: value}
    rows: dict[str, Mapping[MemberRole, Cell]] = {**MATRIX, label: MappingProxyType(changed)}
    return MappingProxyType(rows)


def test_derivation_follows_a_changed_cell() -> None:
    matrix = _flipped("Org settings (general)", "developer", "W")
    assert "org:write" in rbac_matrix._derive_scopes(matrix, ROW_GRANTS, "developer")
    assert "org:write" not in role_scopes("developer")
    matrix = _flipped("Audit logs", "analyst", "R")
    assert "audit.read" in rbac_matrix._derive_permissions(matrix, ROW_GRANTS, "analyst")
    assert role_permissions("analyst") == frozenset()


def test_rows_without_a_resource_grant_no_scope() -> None:
    matrix = _flipped("API keys: list (prefix only)", "viewer", "W")
    assert rbac_matrix._derive_scopes(matrix, ROW_GRANTS, "viewer") == role_scopes("viewer")


def test_a_row_needs_one_cell_per_role() -> None:
    with pytest.raises(ValueError, match="needs 6 cells"):
        rbac_matrix._row("R", "R")
