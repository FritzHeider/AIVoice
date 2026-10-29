"""Org ``ip_allowlist`` enforcement against a real database (specs/04-auth-tenancy-rbac.md §7 Decision
"Enforcing ip_allowlist and session_max_age_days (P02-T12c)")."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from identity_db import IdentityDb
from peer import PeerSwitch
from session_helpers import (
    SESSION_PROBE_PATH,
    SessionContainer,
    error_of,
    login,
    make_member,
    session_app,
    session_client,
    set_org,
    signed_in,
    store_api_key,
    sub_account,
    write_headers,
)

from va_api.auth import ip_allowlist
from va_api.auth.ip_allowlist import IP_NOT_ALLOWED_MESSAGE
from va_api.routers.org_settings import IP_ALLOWLIST_NO_CLIENT_MESSAGE
from va_db.models import ApiKey

ME = "/v1/me"
SETTINGS = "/v1/org/settings"
OFFICE = "203.0.113.7"
OUTSIDE = "198.51.100.9"
OFFICE_NET = "203.0.113.0/24"


@pytest.fixture
def peer(container: SessionContainer) -> PeerSwitch:
    return PeerSwitch(session_app(container), host=OFFICE)


@pytest.fixture
async def client(peer: PeerSwitch) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(peer) as http:
        yield http


async def allow(container: SessionContainer, org_id: uuid.UUID, networks: list[str]) -> None:
    """Store ``networks`` (already normalized) as the org's allowlist and drop the cached settings."""
    await set_org(container.identity, org_id, settings={"ip_allowlist": networks})
    await container.org_settings.invalidate(org_id)


async def keyed_org(container: SessionContainer, networks: list[str]) -> tuple[uuid.UUID, str, ApiKey]:
    """A fresh org with allowlist ``networks`` and a ``*`` key of it: the org id, the key, its row."""
    member = await make_member(container.identity, container.password_hasher)
    key, row = await store_api_key(container, member.org.id)
    await allow(container, member.org.id, networks)
    return member.org.id, key, row


def bearer(key: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **headers}


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


def assert_ip_denied(response: httpx.Response) -> None:
    assert error_of(response) == (403, "FORBIDDEN", None), response.text
    assert message_of(response) == IP_NOT_ALLOWED_MESSAGE


async def test_api_keys_are_accepted_only_from_listed_networks(
    client: httpx.AsyncClient, peer: PeerSwitch, container: SessionContainer
) -> None:
    org_id, key, _ = await keyed_org(container, [OFFICE_NET])
    assert (await client.get(ME, headers=bearer(key))).status_code == 200
    peer.host = OUTSIDE
    assert_ip_denied(await client.get(ME, headers=bearer(key)))
    peer.host = "testclient"
    assert_ip_denied(await client.get(ME, headers=bearer(key)))
    await allow(container, org_id, [])
    assert (await client.get(ME, headers=bearer(key))).status_code == 200


async def test_a_denied_key_is_logged_and_neither_touched_nor_claims_its_idempotency_key(
    client: httpx.AsyncClient,
    peer: PeerSwitch,
    container: SessionContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, key, row = await keyed_org(container, [OFFICE_NET])
    warnings: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def warning(self, event: str, **values: Any) -> None:
            warnings.append((event, values))

    monkeypatch.setattr(ip_allowlist, "_log", Recorder())
    peer.host = OUTSIDE
    headers = bearer(key, **{"Idempotency-Key": "ip-allowlist-1"})
    assert_ip_denied(await client.post(SESSION_PROBE_PATH, json={"attempt": 1}, headers=headers))
    assert container.store.touches == []
    assert warnings == [("ip_not_allowed", {"org_id": str(org_id), "ip": OUTSIDE})]
    peer.host = OFFICE
    # A different body under the same key would be an idempotency conflict had the denied request claimed it.
    allowed = await client.post(SESSION_PROBE_PATH, json={"attempt": 2}, headers=headers)
    assert allowed.status_code == 200, allowed.text
    assert [key_id for key_id, _ in container.store.touches] == [row.id]


async def test_ipv6_peers_match_ipv6_networks(
    client: httpx.AsyncClient, peer: PeerSwitch, container: SessionContainer
) -> None:
    _, key, _ = await keyed_org(container, ["2001:db8::/32"])
    peer.host = "2001:db8:5::1"
    assert (await client.get(ME, headers=bearer(key))).status_code == 200
    peer.host = "2001:db9::1"
    assert_ip_denied(await client.get(ME, headers=bearer(key)))


async def test_sessions_are_gated_but_session_routes_still_work(
    client: httpx.AsyncClient, peer: PeerSwitch, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await allow(container, member.org.id, [OFFICE_NET])
    assert (await client.get(ME)).status_code == 200
    peer.host = OUTSIDE
    assert_ip_denied(await client.get(ME))
    assert (await client.get("/internal/auth/session")).status_code == 200
    logout = await client.post("/internal/auth/logout", headers=write_headers(client))
    assert logout.status_code in {200, 204}, logout.text


async def test_a_settings_change_applies_on_the_next_request(
    client: httpx.AsyncClient, peer: PeerSwitch, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    key, _ = await store_api_key(container, member.org.id)
    assert (await client.get(ME, headers=bearer(key))).status_code == 200  # caches the empty allowlist
    patched = await client.patch(SETTINGS, json={"ip_allowlist": [OFFICE]}, headers=write_headers(client))
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["ip_allowlist"] == [f"{OFFICE}/32"]
    peer.host = OUTSIDE
    assert_ip_denied(await client.get(ME, headers=bearer(key)))


@pytest.mark.parametrize("target", [(SETTINGS, "ip_allowlist"), ("/v1/org", "settings.ip_allowlist")])
async def test_an_allowlist_must_admit_the_request_setting_it(
    client: httpx.AsyncClient, peer: PeerSwitch, container: SessionContainer, target: tuple[str, str]
) -> None:
    path, field = target
    await signed_in(client, container.identity, container)

    async def send(networks: list[str]) -> httpx.Response:
        body: dict[str, Any] = {"ip_allowlist": networks}
        payload = body if path == SETTINGS else {"settings": body}
        return await client.patch(path, json=payload, headers=write_headers(client))

    locked_out = await send(["198.51.100.0/24"])
    assert error_of(locked_out) == (400, "INVALID_INPUT", field)
    assert message_of(locked_out) == f"ip_allowlist must include the address of this request ({OFFICE})."
    assert (await send(["198.51.100.0/24", OFFICE_NET])).status_code == 200
    peer.host = "::ffff:203.0.113.7"
    assert (await send([OFFICE_NET])).status_code == 200
    peer.host = OFFICE
    assert (await send([])).status_code == 200
    peer.host = "testclient"
    unknown = await send([OFFICE_NET])
    assert error_of(unknown) == (400, "INVALID_INPUT", field)
    assert message_of(unknown) == IP_ALLOWLIST_NO_CLIENT_MESSAGE
    assert (await send([])).status_code == 200


async def test_a_parent_key_acting_on_a_child_needs_both_allowlists(
    client: httpx.AsyncClient, peer: PeerSwitch, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    key, _ = await store_api_key(container, member.org.id)
    acting = bearer(key, **{"X-VA-Org-Id": str(child_id)})
    await allow(container, child_id, ["198.51.100.0/24"])
    assert (await client.get(ME, headers=bearer(key))).status_code == 200
    assert_ip_denied(await client.get(ME, headers=acting))
    await allow(container, child_id, [OFFICE_NET])
    assert (await client.get(ME, headers=acting)).status_code == 200
    await allow(container, member.org.id, ["198.51.100.0/24"])
    assert_ip_denied(await client.get(ME, headers=acting))
    # The parent's list is checked before the header is looked at, even for an org that is not a child.
    stranger = bearer(key, **{"X-VA-Org-Id": str(uuid.uuid4())})
    assert_ip_denied(await client.get(ME, headers=stranger))


async def test_a_session_acting_in_a_child_through_its_parent_needs_the_parent_allowlist(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    switched = await client.post(
        "/internal/auth/switch-org", json={"org_id": str(child_id)}, headers=write_headers(client)
    )
    assert switched.status_code == 200, switched.text
    me = await client.get(ME)
    assert me.json()["data"]["acting_parent_org_id"] == str(member.org.id)
    await allow(container, member.org.id, ["198.51.100.0/24"])
    assert_ip_denied(await client.get(ME))


async def test_the_default_peer_is_unaffected_without_a_list(
    container: SessionContainer, identity: IdentityDb
) -> None:
    member = await make_member(identity, container.password_hasher)
    async with session_client(session_app(container)) as plain:
        await login(plain, member.user.email)
        assert (await plain.get(ME)).status_code == 200


async def test_a_corrupt_stored_setting_neither_blocks_the_org_nor_disables_its_list(
    client: httpx.AsyncClient, peer: PeerSwitch, container: SessionContainer
) -> None:
    member = await signed_in(client, container.identity, container)
    broken = {"timezone": "Mars/Olympus_Mons", "ip_allowlist": [OFFICE_NET]}
    await set_org(container.identity, member.org.id, settings=broken)
    await container.org_settings.invalidate(member.org.id)
    repaired = await client.patch(
        SETTINGS, json={"timezone": "America/Denver"}, headers=write_headers(client)
    )
    assert repaired.status_code == 200, repaired.text
    peer.host = OUTSIDE
    assert_ip_denied(await client.get(ME))
