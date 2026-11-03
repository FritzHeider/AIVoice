"""Org webhook endpoints, test sends, deliveries, and replay (specs/19 §10 Decision P06-T04b).

Roles and scopes follow specs/04 §3.2 "Webhook endpoints / deliveries / replay".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import orjson
import pytest
from call_helpers import bearer
from identity_db import IdentityDb
from session_helpers import (
    SessionContainer,
    audit_rows,
    error_of,
    session_app,
    session_client,
    signed_in,
    store_api_key,
    stored_org,
    write_headers,
)
from sqlalchemy import func, select
from webhook_receiver import Receiver, Reply, served_receiver, signature_ok

from va_common.ids import new_id
from va_db.models import MemberRole, WebhookDelivery, WebhookEndpoint
from va_db.repositories import ENDPOINT_DELETED_ERROR

pytestmark = pytest.mark.integration

ENDPOINTS = "/v1/webhooks/endpoints"
DELIVERIES = "/v1/webhooks/deliveries"
HOOK_HOST = "hooks.test"


@dataclass(frozen=True, slots=True)
class Hooks:
    receiver: Receiver
    port: int

    def url(self, path: str = "/hook") -> str:
        return f"http://{HOOK_HOST}:{self.port}{path}"


@pytest.fixture
async def hooks(container: SessionContainer) -> AsyncIterator[Hooks]:
    """A real receiver on 127.0.0.1 that ``hooks.test`` resolves to."""
    receiver = Receiver()
    container.host_resolver.answers[HOOK_HOST] = ["127.0.0.1"]
    async with served_receiver(receiver) as port:
        yield Hooks(receiver, port)


@pytest.fixture
async def owner(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> httpx.AsyncClient:
    """``client`` signed in as an org owner."""
    await signed_in(client, identity, container)
    return client


async def owner_org(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> uuid.UUID:
    return (await signed_in(client, identity, container)).org.id


async def create(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post(ENDPOINTS, json=body, headers=write_headers(client))


async def created(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await create(client, **body)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def patch(client: httpx.AsyncClient, endpoint_id: str, **body: Any) -> httpx.Response:
    return await client.patch(f"{ENDPOINTS}/{endpoint_id}", json=body, headers=write_headers(client))


async def send_test(client: httpx.AsyncClient, endpoint_id: str, **body: Any) -> httpx.Response:
    return await client.post(f"{ENDPOINTS}/{endpoint_id}/test", json=body, headers=write_headers(client))


async def deliveries_count(identity: IdentityDb, org_id: uuid.UUID) -> int:
    async with identity.database.system_session() as session:
        statement = select(func.count()).where(WebhookDelivery.org_id == org_id)
        return int(await session.scalar(statement) or 0)


async def seed_delivery(
    identity: IdentityDb, container: SessionContainer, org_id: uuid.UUID, age_s: int, **values: Any
) -> uuid.UUID:
    """A delivery of ``org_id`` created ``age_s`` seconds before the container clock."""
    columns: dict[str, Any] = {
        "event_id": f"evt_{new_id().hex}",
        "event_type": "call.completed",
        "url": "https://hooks.acme.test/hook",
        "payload": {"secret_note": "never shown"},
        "status": "dead",
        "attempts": 9,
        "last_error": "http_status",
        "last_status_code": 500,
        "created_at": container.clock.now() - timedelta(seconds=age_s),
        **values,
    }
    async with identity.database.tenant_session(org_id) as session:
        row = WebhookDelivery(org_id=org_id, **columns)
        session.add(row)
        await session.flush()
        return row.id


# ---- endpoints ---------------------------------------------------------------------------------------------


async def test_create_defaults_audit_and_list_oldest_first(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    first = await created(client, url=hooks.url("/a"))
    assert {key: first[key] for key in ("url", "description", "event_types", "enabled", "phi_allowed")} == {
        "url": hooks.url("/a"),
        "description": "",
        "event_types": ["*"],
        "enabled": True,
        "phi_allowed": False,
    }
    container.clock.advance(1)
    second = await created(client, url=hooks.url("/b"), event_types=["call.*", "sms.received"], enabled=False)
    listed = (await client.get(ENDPOINTS)).json()["data"]
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert listed[1]["event_types"] == ["call.*", "sms.received"]
    (row, _) = await audit_rows(identity, org_id, "webhook_endpoint.create")
    assert (row.resource_type, row.resource_id) == ("webhook_endpoint", first["id"])
    assert row.changes["after"]["url"] == hooks.url("/a")


async def test_patch_changes_a_subset_and_audits_only_real_changes(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    endpoint = await created(client, url=hooks.url(), description="CRM")
    response = await patch(client, endpoint["id"], enabled=False, description="CRM sync")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["enabled"], data["description"], data["url"]) == (False, "CRM sync", hooks.url())
    assert (await patch(client, endpoint["id"], enabled=False)).status_code == 200
    (row,) = await audit_rows(identity, org_id, "webhook_endpoint.update")
    assert row.changes == {
        "before": {"description": "CRM", "enabled": True},
        "after": {"description": "CRM sync", "enabled": False},
    }
    assert error_of(await patch(client, endpoint["id"], url=None)) == (400, "INVALID_INPUT", "url")
    assert error_of(await patch(client, endpoint["id"], color="red")) == (400, "INVALID_INPUT", "color")
    assert error_of(await patch(client, str(new_id()), enabled=True))[:2] == (404, "NOT_FOUND")


async def test_delete_cancels_only_the_endpoints_queued_deliveries(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    doomed = await created(client, url=hooks.url("/doomed"))
    kept = await created(client, url=hooks.url("/kept"))
    doomed_id, kept_id = uuid.UUID(doomed["id"]), uuid.UUID(kept["id"])
    pending = await seed_delivery(identity, container, org_id, 5, status="pending", endpoint_id=doomed_id)
    delivering = await seed_delivery(
        identity, container, org_id, 4, status="delivering", endpoint_id=doomed_id
    )
    done = await seed_delivery(identity, container, org_id, 3, status="succeeded", endpoint_id=doomed_id)
    other = await seed_delivery(identity, container, org_id, 2, status="pending", endpoint_id=kept_id)
    response = await client.delete(f"{ENDPOINTS}/{doomed['id']}", headers=write_headers(client))
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"deleted": True}
    async with identity.database.system_session() as session:
        rows = {
            row.id: (row.status, row.last_error, row.endpoint_id)
            for row in await session.scalars(select(WebhookDelivery).where(WebhookDelivery.org_id == org_id))
        }
    assert rows[pending] == rows[delivering] == ("dead", ENDPOINT_DELETED_ERROR, None)
    assert rows[done] == ("succeeded", "http_status", None)
    assert rows[other] == ("pending", "http_status", kept_id)
    (row,) = await audit_rows(identity, org_id, "webhook_endpoint.delete")
    assert row.changes["after"] == {"cancelled_deliveries": 2}
    assert [item["id"] for item in (await client.get(ENDPOINTS)).json()["data"]] == [kept["id"]]
    again = await client.delete(f"{ENDPOINTS}/{doomed['id']}", headers=write_headers(client))
    assert error_of(again)[:2] == (404, "NOT_FOUND")


async def test_urls_are_vetted_on_create_and_patch(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    await owner_org(client, identity, container)
    metadata = await create(client, url="http://169.254.169.254/latest/meta-data")
    assert error_of(metadata) == (400, "WEBHOOK_URL_REJECTED", "url")
    assert metadata.json()["errors"][0]["message"] == "The url URL resolves to a blocked address."
    assert error_of(await create(client, url="ftp://hooks.test/x")) == (400, "WEBHOOK_URL_REJECTED", "url")
    assert error_of(await create(client, url="https://" + "a" * 2048)) == (400, "INVALID_INPUT", "url")
    endpoint = await created(client, url=hooks.url())
    container.host_resolver.answers["gone.test"] = []
    assert error_of(await patch(client, endpoint["id"], url="https://gone.test/x")) == (
        400,
        "WEBHOOK_URL_REJECTED",
        "url",
    )


async def test_private_targets_need_webhook_allow_private(
    identity: IdentityDb, container: SessionContainer
) -> None:
    container.host_resolver.answers["intranet.test"] = ["10.0.0.7"]
    async with session_client(session_app(container, webhook_allow_private=False)) as client:
        await owner_org(client, identity, container)
        response = await create(client, url="https://intranet.test/hook")
    assert error_of(response) == (400, "WEBHOOK_URL_REJECTED", "url")


@pytest.mark.parametrize(
    ("event_types", "field"),
    [
        (["foo.bar"], "event_types[0]"),
        (["call.*", "nope.*"], "event_types[1]"),
        (["call"], "event_types[0]"),
        (["*.*"], "event_types[0]"),
        ([], "event_types"),
        (["call.*", "call.*"], "event_types"),
        (["call.completed"] * 101, "event_types"),
    ],
)
async def test_event_types_are_validated(
    owner: httpx.AsyncClient, hooks: Hooks, event_types: list[str], field: str
) -> None:
    client = owner
    response = await create(client, url=hooks.url(), event_types=event_types)
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_an_org_has_at_most_20_endpoints(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    async with identity.database.tenant_session(org_id) as session:
        session.add_all(WebhookEndpoint(org_id=org_id, url=hooks.url(f"/{index}")) for index in range(20))
    response = await create(client, url=hooks.url("/21"))
    assert error_of(response) == (400, "INVALID_INPUT", None)
    assert response.json()["errors"][0]["message"] == "An org can have at most 20 webhook endpoints."


async def test_phi_allowed_needs_org_compliance(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    await signed_in(client, identity, container, "developer")
    assert error_of(await create(client, url=hooks.url(), phi_allowed=True))[:2] == (403, "FORBIDDEN")
    endpoint = await created(client, url=hooks.url(), phi_allowed=False)
    assert error_of(await patch(client, endpoint["id"], phi_allowed=True))[:2] == (403, "FORBIDDEN")
    client.cookies.clear()
    await signed_in(client, identity, container, "owner")
    assert (await created(client, url=hooks.url(), phi_allowed=True))["phi_allowed"] is True


@pytest.mark.parametrize(
    ("role", "read", "write"),
    [
        ("admin", 200, 201),
        ("developer", 200, 201),
        ("analyst", 200, 403),
        ("viewer", 200, 403),
        ("billing", 403, 403),
    ],
)
async def test_roles(  # noqa: PLR0913, PLR0917 - fixtures plus the parametrized role row
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    hooks: Hooks,
    role: MemberRole,
    read: int,
    write: int,
) -> None:
    await signed_in(client, identity, container, role)
    assert (await client.get(ENDPOINTS)).status_code == read
    assert (await client.get(DELIVERIES)).status_code == read
    assert (await create(client, url=hooks.url())).status_code == write


async def test_a_read_only_key_cannot_write(
    bare: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    member = await signed_in(bare, identity, container)
    bare.cookies.clear()
    key, _ = await store_api_key(container, member.org.id, ("webhooks:read",))
    assert (await bare.get(ENDPOINTS, headers=bearer(key))).status_code == 200
    response = await bare.post(ENDPOINTS, json={"url": hooks.url()}, headers=bearer(key))
    assert error_of(response)[:2] == (403, "FORBIDDEN")


# ---- test send ---------------------------------------------------------------------------------------------


async def test_test_send_signs_an_envelope_and_stores_nothing(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    secret = await identity.provisioner.webhook_signing_secret(await stored_org(identity, org_id))
    endpoint = await created(client, url=hooks.url("/t"), enabled=False)
    response = await send_test(client, endpoint["id"], event_type="sms.received")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    (received,) = hooks.receiver.requests
    assert signature_ok(received, secret)
    body = orjson.loads(received.body)
    assert body == {
        "id": result["event_id"],
        "type": "sms.received",
        "created_at": body["created_at"],
        "org_id": str(org_id),
        "call_id": None,
        "batch_id": None,
        "conversation_id": None,
        "data": {"test": True, "message": "Test event from VoiceAgents."},
    }
    assert received.headers["x-va-event-type"] == "sms.received"
    assert (result["succeeded"], result["status_code"], result["error"]) == (True, 200, None)
    assert result["event_id"].startswith("evt_")
    assert await deliveries_count(identity, org_id) == 0
    assert [row.action for row in await audit_rows(identity, org_id) if row.action.startswith("webhook")] == [
        "webhook_endpoint.create"
    ]


async def test_test_send_reports_failures(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    await owner_org(client, identity, container)
    endpoint = await created(client, url=hooks.url("/fail"))
    hooks.receiver.script("/fail", Reply(500))
    default = await client.post(f"{ENDPOINTS}/{endpoint['id']}/test", headers=write_headers(client))
    result = default.json()["data"]
    assert (result["event_type"], result["succeeded"], result["status_code"], result["error"]) == (
        "call.completed",
        False,
        500,
        "http_status",
    )
    assert error_of(await send_test(client, endpoint["id"], event_type="call.*")) == (
        400,
        "INVALID_INPUT",
        "event_type",
    )
    container.host_resolver.answers[HOOK_HOST] = ["169.254.169.254"]
    blocked = (await send_test(client, endpoint["id"])).json()["data"]
    assert (blocked["succeeded"], blocked["status_code"], blocked["error"]) == (
        False,
        None,
        "blocked_destination:address",
    )
    assert len(hooks.receiver.requests) == 1
    assert error_of(await send_test(client, str(new_id())))[:2] == (404, "NOT_FOUND")


# ---- deliveries --------------------------------------------------------------------------------------------


async def test_deliveries_list_newest_first_with_filters_and_cursor(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    endpoint_id = uuid.UUID((await created(client, url=hooks.url()))["id"])
    call_id = new_id()
    oldest = await seed_delivery(identity, container, org_id, 30, call_id=call_id)
    middle = await seed_delivery(
        identity,
        container,
        org_id,
        20,
        status="pending",
        event_type="sms.received",
        endpoint_id=endpoint_id,
        next_attempt_at=container.clock.now(),
    )
    newest = await seed_delivery(identity, container, org_id, 10, status="succeeded", call_id=call_id)
    listed = (await client.get(DELIVERIES)).json()
    assert [item["id"] for item in listed["data"]] == [str(newest), str(middle), str(oldest)]
    assert all("payload" not in item for item in listed["data"])
    assert listed["data"][0]["next_attempt_at"] is None
    assert listed["data"][1]["next_attempt_at"] is not None

    async def ids(**params: str) -> list[str]:
        return [item["id"] for item in (await client.get(DELIVERIES, params=params)).json()["data"]]

    assert await ids(status="dead") == [str(oldest)]
    assert await ids(event_type="sms.received") == [str(middle)]
    assert await ids(call_id=str(call_id)) == [str(newest), str(oldest)]
    assert await ids(endpoint_id=str(endpoint_id)) == [str(middle)]
    first = (await client.get(DELIVERIES, params={"limit": "2"})).json()
    assert first["pagination"]["has_more"] is True
    rest = await ids(limit="2", cursor=first["pagination"]["next_cursor"])
    assert rest == [str(oldest)]
    assert error_of(await client.get(DELIVERIES, params={"limit": "101"})) == (400, "INVALID_INPUT", "limit")
    assert error_of(await client.get(DELIVERIES, params={"status": "lost"})) == (
        400,
        "INVALID_INPUT",
        "status",
    )


async def test_replay_restarts_a_finished_delivery(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    org_id = await owner_org(client, identity, container)
    dead = await seed_delivery(identity, container, org_id, 10, delivered_at=None, last_response_ms=88)
    async with identity.database.system_session() as session:
        before = await session.get(WebhookDelivery, dead)
        assert before is not None
        event_id, url = before.event_id, before.url
    response = await client.post(f"{DELIVERIES}/{dead}/replay", headers=write_headers(client))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["id"], data["event_id"], data["url"], data["status"], data["attempts"]) == (
        str(dead),
        event_id,
        url,
        "pending",
        0,
    )
    assert (data["last_error"], data["last_status_code"], data["last_response_ms"]) == (None, None, None)
    assert data["next_attempt_at"] == container.clock.now().isoformat().replace("+00:00", "Z")
    (row,) = await audit_rows(identity, org_id, "webhook_delivery.replay")
    assert row.changes == {
        "before": {"status": "dead", "attempts": 9, "last_error": "http_status"},
        "after": {"status": "pending"},
    }
    queued = await client.post(f"{DELIVERIES}/{dead}/replay", headers=write_headers(client))
    assert error_of(queued) == (409, "CONFLICT", None)
    assert queued.json()["errors"][0]["message"] == "The delivery is already queued."


async def test_replay_refuses_cancelled_and_disabled_endpoint_deliveries(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, hooks: Hooks
) -> None:
    org_id = await owner_org(client, identity, container)
    endpoint = await created(client, url=hooks.url(), enabled=False)
    cancelled = await seed_delivery(identity, container, org_id, 5, last_error=ENDPOINT_DELETED_ERROR)
    disabled = await seed_delivery(identity, container, org_id, 4, endpoint_id=uuid.UUID(endpoint["id"]))
    for delivery_id, message in (
        (cancelled, "The delivery's endpoint was deleted."),
        (disabled, "Enable the webhook endpoint before replaying its deliveries."),
    ):
        response = await client.post(f"{DELIVERIES}/{delivery_id}/replay", headers=write_headers(client))
        assert error_of(response) == (409, "CONFLICT", None)
        assert response.json()["errors"][0]["message"] == message
    missing = await client.post(f"{DELIVERIES}/{new_id()}/replay", headers=write_headers(client))
    assert error_of(missing)[:2] == (404, "NOT_FOUND")
