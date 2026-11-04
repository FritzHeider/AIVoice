"""Role matrix of the pathways API core (specs/12 §10; specs/04 §3.2; Decision P07-T09a).

Every role signs in to its own org through the dashboard session and tries to read, create, update, validate,
delete, and force-delete a pathway of that org, and to list, save, publish, promote, and revert its versions
(Decision P07-T09b).
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import seed_pathway
from session_helpers import SessionContainer, error_of, make_member, signed_in, store_api_key, write_headers

from va_api.auth.context import PrincipalType, RequestContext
from va_api.auth.pathway_roles import (
    FORCE_DELETE_MESSAGE,
    NOT_EDITOR_MESSAGE,
    NOT_PUBLISHER_MESSAGE,
    can_force_delete,
    is_pathway_editor,
    is_pathway_publisher,
)
from va_common.ids import new_id
from va_db.models import MemberRole
from va_db.repositories import PathwayFolderRepo, PathwayRepo

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
        pathway_id = str((await seed_pathway(identity, member.org.id, [])).id)

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


def assert_reads(role: MemberRole, reads: list[httpx.Response]) -> None:
    """Readers get every read, other roles are refused."""
    if role in READERS:
        assert [response.status_code for response in reads] == [200] * len(reads)
    else:
        for response in reads:
            assert error_of(response)[:2] == (403, "FORBIDDEN")


PUBLISHERS: Final[frozenset[MemberRole]] = frozenset({"owner", "admin", "developer"})


@pytest.mark.parametrize("role", ROLES)
async def test_version_role_matrix(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container, role)
    pathway = await seed_pathway(identity, member.org.id)
    base = f"{BASE}/{pathway.id}"
    reads = [await client.get(f"{base}/versions"), await client.get(f"{base}/version/1")]
    assert_reads(role, reads)

    headers = write_headers(client)
    saved = await client.post(f"{base}/version", json={"name": "v1"}, headers=headers)
    if role in EDITORS:
        assert saved.status_code == 200, saved.text
    else:
        assert error_of(saved)[:2] == (403, "FORBIDDEN")
    publishes = [
        await client.post(
            f"{base}/publish", json={"version_number": 1, "environment": "staging"}, headers=headers
        ),
        await client.post(f"{base}/promote", headers=headers),
        await client.post(f"{base}/revert", json={"version_number": 1}, headers=headers),
    ]
    if role not in PUBLISHERS:
        for response in publishes:
            assert error_of(response)[:2] == (403, "FORBIDDEN")
        assert publishes[0].json()["errors"][0]["message"] == NOT_PUBLISHER_MESSAGE
        return
    assert [response.status_code for response in publishes] == [200, 200, 200], publishes[0].text
    async with identity.database.tenant_session(member.org.id) as session:
        version = await PathwayRepo(session, member.org.id).version(pathway, 1)
        assert version is not None
        assert version.published_by == member.user.id


@pytest.mark.parametrize(
    ("scopes", "status"), [(("pathways:read", "pathways:write"), 200), (("pathways:read",), 403)]
)
async def test_api_keys_publish_with_pathways_write(
    scopes: tuple[str, ...],
    status: int,
    bare: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id, scopes)
    pathway = await seed_pathway(identity, member.org.id)
    response = await bare.post(f"{BASE}/{pathway.id}/publish", headers={"Authorization": key})
    assert response.status_code == status, response.text


def test_publisher_helper_for_principals() -> None:
    assert is_pathway_publisher(_ctx("api_key", None, frozenset({"pathways:write"})))
    assert is_pathway_publisher(_ctx("user", "developer", frozenset()))
    assert not is_pathway_publisher(_ctx("user", "analyst", frozenset()))
    assert not is_pathway_publisher(_ctx("user", None, frozenset()))


@pytest.mark.parametrize("role", ROLES)
async def test_io_and_folder_role_matrix(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container, role)
    pathway = await seed_pathway(identity, member.org.id)
    async with identity.database.tenant_session(member.org.id) as session:
        folder = await PathwayFolderRepo(session, member.org.id).create(name="Seeded", parent_id=None)
    reads = [
        await client.get(f"{BASE}/{pathway.id}/export"),
        await client.get(f"{BASE}/folders"),
        await client.get(f"{BASE}/folders/{folder.id}"),
    ]
    if role in READERS:
        assert [response.status_code for response in reads] == [200, 200, 200]
    else:
        for response in reads:
            assert error_of(response)[:2] == (403, "FORBIDDEN")

    headers = write_headers(client)
    nodes = [{"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "text": "Hi"}}]
    writes = [
        await client.post(f"{BASE}/import", json={"nodes": nodes, "edges": []}, headers=headers),
        await client.post(f"{BASE}/{pathway.id}/duplicate", headers=headers),
        await client.post(f"{BASE}/folders", json={"name": "New"}, headers=headers),
        await client.patch(f"{BASE}/folders/{folder.id}", json={"name": "Renamed"}, headers=headers),
        await client.post(
            f"{BASE}/folders/{folder.id}/move", json={"pathway_ids": [str(pathway.id)]}, headers=headers
        ),
        await client.delete(f"{BASE}/folders/{folder.id}", headers=headers),
    ]
    if role in EDITORS:
        assert [response.status_code for response in writes] == [200] * 6, [r.text for r in writes]
    else:
        for response in writes:
            assert error_of(response)[:2] == (403, "FORBIDDEN")
            assert response.json()["errors"][0]["message"] == NOT_EDITOR_MESSAGE


@pytest.mark.parametrize("role", ROLES)
async def test_chat_role_matrix(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    """The test chat needs an editing role (specs/12 §10; Decision P07-T10); the seeded start node speaks a
    static line, so creating a chat needs no model."""
    member = await signed_in(client, identity, container, role)
    pathway = await seed_pathway(identity, member.org.id)
    headers = write_headers(client)
    created = await client.post(f"{BASE}/chat/create", json={"pathway_id": str(pathway.id)}, headers=headers)
    if role in EDITORS:
        assert created.status_code == 200, created.text
        assert created.json()["assistant_messages"] == ["Hi"]
        deleted = await client.delete(f"{BASE}/chat/{created.json()['chat_id']}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        return
    chat_id = new_id()
    refused = [
        created,
        await client.post(f"{BASE}/chat/{chat_id}", json={"message": "Hi"}, headers=headers),
        await client.delete(f"{BASE}/chat/{chat_id}", headers=headers),
    ]
    for response in refused:
        assert error_of(response)[:2] == (403, "FORBIDDEN")
        assert response.json()["errors"][0]["message"] == NOT_EDITOR_MESSAGE


async def test_chat_needs_pathways_write(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id, scopes=("pathways:read",))
    pathway = await seed_pathway(identity, member.org.id)
    response = await client.post(
        f"{BASE}/chat/create", json={"pathway_id": str(pathway.id)}, headers={"Authorization": key}
    )
    assert error_of(response)[:2] == (403, "FORBIDDEN")


TESTERS: Final[frozenset[MemberRole]] = frozenset({"owner", "admin", "developer", "analyst"})
_UNIT_TEST: Final = {"name": "Greets", "node_id": "start", "user_message": "Hi"}


@pytest.mark.parametrize("role", ROLES)
async def test_unit_test_role_matrix(
    role: MemberRole, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    """Analysts manage unit tests (specs/04 §3.2 row "Pathway tests / simulations"; Decision P07-T11b)."""
    member = await signed_in(client, identity, container, role)
    pathway = await seed_pathway(identity, member.org.id)
    base = f"{BASE}/{pathway.id}"
    reads = [await client.get(f"{base}/tests"), await client.get(f"{base}/runs")]
    assert_reads(role, reads)

    headers = write_headers(client)
    created = await client.post(f"{base}/tests", json=_UNIT_TEST, headers=headers)
    if role not in TESTERS:
        assert error_of(created)[:2] == (403, "FORBIDDEN")
        return
    assert created.status_code == 200, created.text
    test_url = f"{base}/tests/{created.json()['test']['test_id']}"
    patched = await client.patch(test_url, json={"name": "Greets back"}, headers=headers)
    deleted = await client.delete(test_url, headers=headers)
    assert (patched.status_code, deleted.status_code) == (200, 200)
