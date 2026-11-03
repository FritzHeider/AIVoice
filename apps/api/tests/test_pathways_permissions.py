"""Role matrix of the pathways API core (specs/12 §10; specs/04 §3.2; Decision P07-T09a).

Every role signs in to its own org through the dashboard session and tries to read, create, update, validate,
delete, and force-delete a pathway of that org.
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import SessionContainer, error_of, signed_in, write_headers

from va_api.auth.context import PrincipalType, RequestContext
from va_api.auth.pathway_roles import (
    FORCE_DELETE_MESSAGE,
    NOT_EDITOR_MESSAGE,
    can_force_delete,
    is_pathway_editor,
)
from va_common.ids import new_id
from va_db.models import MemberRole
from va_db.repositories import PathwayGraphJson, PathwayRepo

pytestmark = pytest.mark.integration

BASE: Final = "/v1/pathway"
READERS: Final[frozenset[MemberRole]] = frozenset({"owner", "admin", "developer", "analyst", "viewer"})
EDITORS: Final[frozenset[MemberRole]] = frozenset({"owner", "admin", "developer"})
FORCERS: Final[frozenset[MemberRole]] = frozenset({"owner", "admin"})
ROLES: Final[tuple[MemberRole, ...]] = ("owner", "admin", "developer", "analyst", "billing", "viewer")


async def _create(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{BASE}/create", json={"name": "Orders"}, headers=write_headers(client))


@pytest.mark.parametrize("role", ROLES)
async def test_role_matrix(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container, role)
    if role in EDITORS:
        created = await _create(client)
        assert created.status_code == 200, created.text
        pathway_id = created.json()["pathway_id"]
    else:
        assert error_of(await _create(client))[:2] == (403, "FORBIDDEN")
        async with identity.database.tenant_session(member.org.id) as session:
            pathway, _ = await PathwayRepo(session, member.org.id).create(
                name="Orders",
                description="",
                folder_id=None,
                graph=PathwayGraphJson(nodes=[], edges=[], global_config={}, graph_hash="0" * 64),
            )
            pathway_id = str(pathway.id)

    listed = await client.get(BASE)
    got = await client.get(f"{BASE}/{pathway_id}")
    if role in READERS:
        assert (listed.status_code, got.status_code) == (200, 200), listed.text
    else:
        assert error_of(listed)[:2] == error_of(got)[:2] == (403, "FORBIDDEN")

    headers = write_headers(client)
    writes = [
        await client.post(f"{BASE}/{pathway_id}", json={"name": "Orders v2"}, headers=headers),
        await client.post(f"{BASE}/{pathway_id}/validate", headers=headers),
    ]
    forced = await client.delete(f"{BASE}/{pathway_id}", params={"force": "true"}, headers=headers)
    if role in EDITORS:
        assert [response.status_code for response in writes] == [200, 200]
    else:
        for response in writes:
            assert error_of(response)[:2] == (403, "FORBIDDEN")
    if role in FORCERS:
        assert forced.status_code == 200, forced.text
        return
    assert error_of(forced)[:2] == (403, "FORBIDDEN")
    deleted = await client.delete(f"{BASE}/{pathway_id}", headers=headers)
    if role in EDITORS:
        assert deleted.status_code == 200, deleted.text
    else:
        assert error_of(deleted)[:2] == (403, "FORBIDDEN")


@pytest.mark.parametrize("role", ["analyst", "viewer", "billing"])
async def test_non_editors_get_the_editor_message(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container, role)
    response = await _create(client)
    assert response.json()["errors"][0]["message"] == NOT_EDITOR_MESSAGE


def _ctx(principal_type: PrincipalType, role: MemberRole | None, scopes: frozenset[str]) -> RequestContext:
    return RequestContext(
        request_id="req_test",
        org_id=new_id(),
        principal_type=principal_type,
        principal_id=new_id(),
        role=role,
        scopes=scopes,
        environment="live",
        plan_id="build",
        ip=None,
        user_agent=None,
        acting_parent_org_id=None,
    )


def test_role_helpers_for_api_keys_and_widgets() -> None:
    star = _ctx("api_key", None, frozenset({"*"}))
    narrow = _ctx("api_key", None, frozenset({"pathways:write"}))
    widget = _ctx("widget", None, frozenset())
    assert is_pathway_editor(star) and is_pathway_editor(narrow)
    assert can_force_delete(star)
    assert not can_force_delete(narrow)
    assert not can_force_delete(widget)
    assert is_pathway_editor(_ctx("platform_admin", "admin", frozenset()))
    assert not is_pathway_editor(_ctx("user", None, frozenset()))
    assert FORCE_DELETE_MESSAGE == "Force delete requires an owner or admin."
