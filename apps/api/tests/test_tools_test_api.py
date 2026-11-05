"""``POST /v1/tools/{tool_id}/test`` against a real-socket receiver (specs/13 §2, §5; Decision P08-T02a)."""

from __future__ import annotations

import hmac
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import star_api
from session_helpers import SessionContainer, audit_rows, error_of, store_api_key
from webhook_receiver import Receiver, Reply, hex_hmac, served_receiver

from va_db.repositories import OrgRepo
from va_db.webhook_secrets import active_signing_secrets
from va_tools.secrets import MASK

pytestmark = pytest.mark.integration

BASE: Final = "/v1/tools"
HOOK_HOST: Final = "hooks.test"
INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "required": ["date", "party_size"],
}
JSON_HEADERS: Final = {"content-type": "application/json"}


@dataclass(frozen=True, slots=True)
class Hooks:
    receiver: Receiver
    port: int

    def url(self, path: str = "/availability") -> str:
        return f"http://{HOOK_HOST}:{self.port}{path}"


@dataclass(frozen=True, slots=True)
class Org:
    client: httpx.AsyncClient
    org_id: uuid.UUID
    headers: dict[str, str]

    async def tool(self, hooks: Hooks, **changes: Any) -> str:
        definition = {
            "name": f"check_{secrets.token_hex(4)}",
            "description": "Checks availability.",
            "url": hooks.url(),
            "method": "POST",
            "headers": {"X-Org": "{{org_label}}"},
            "query": {"source": "voice"},
            "body": {"date": "{{input.date}}", "party": "{{input.party_size}}", "who": "{{customer}}"},
            "input_schema": INPUT_SCHEMA,
            "response": {"available": "$.available", "times": "$.alternatives[*].time", "nope": "$.nope"},
            "timeout_ms": 5000,
            **changes,
        }
        response = await self.client.post(BASE, json=definition, headers=self.headers)
        assert response.status_code == 200, response.text
        tool_id: str = response.json()["tool_id"]
        return tool_id

    async def run(self, tool_id: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post(f"{BASE}/{tool_id}/test", json=body or {}, headers=self.headers)


@pytest.fixture
async def hooks(container: SessionContainer) -> AsyncIterator[Hooks]:
    receiver = Receiver()
    container.host_resolver.answers[HOOK_HOST] = ["127.0.0.1"]
    async with served_receiver(receiver) as port:
        yield Hooks(receiver, port)


@pytest.fixture
async def org(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Org:
    api = await star_api(client, identity, container)
    return Org(client, api.org_id, dict(api.headers))


async def _org_secret(identity: IdentityDb, container: SessionContainer, org_id: uuid.UUID) -> str:
    async with identity.database.global_session() as session:
        stored = await OrgRepo(session).get(org_id)
        assert stored is not None
        active = await active_signing_secrets(identity.envelope, stored, now=container.clock.now())
    return active.secrets[0]


async def test_success_sends_signed_request_with_secrets_and_masks_them(
    org: Org, hooks: Hooks, identity: IdentityDb, container: SessionContainer
) -> None:
    secret = f"Bearer {secrets.token_hex(16)}"
    tool_id = await org.tool(hooks, secret_headers={"Authorization": secret})
    answer = {"available": False, "alternatives": [{"time": "19:00"}, {"time": "20:30"}]}
    hooks.receiver.script("/availability", Reply(200, JSON_HEADERS, body=json.dumps(answer).encode()))

    response = await org.run(
        tool_id,
        {
            "input": {"date": "2026-11-06", "party_size": 4},
            "variables": {"customer": "Ada", "org_label": "acme"},
        },
    )

    assert response.status_code == 200, response.text
    assert secret not in response.text
    (received,) = hooks.receiver.requests
    assert received.headers["authorization"] == secret
    assert received.headers["x-org"] == "acme"
    assert json.loads(received.body) == {"date": "2026-11-06", "party": 4, "who": "Ada"}
    org_secret = await _org_secret(identity, container, org.org_id)
    parts = dict(item.split("=", 1) for item in received.headers["x-va-signature"].split(","))
    signed = f"{parts['t']}.".encode() + received.body
    assert hmac.compare_digest(parts["v1"], hex_hmac(org_secret, signed))
    assert hmac.compare_digest(received.headers["x-webhook-signature"], hex_hmac(org_secret, received.body))
    body = response.json()
    assert body == {
        "status": "success",
        "request": {
            "url": f"{hooks.url()}?source=voice",
            "method": "POST",
            "headers": {"X-Org": "acme", "Authorization": MASK},
            "body": {"date": "2026-11-06", "party": 4, "who": "Ada"},
        },
        "status_code": 200,
        "response": answer,
        "extracted": {"available": False, "times": ["19:00", "20:30"], "nope": None},
        "duration_ms": body["duration_ms"],
        "error": None,
        "details": [],
        "truncated": False,
    }
    assert isinstance(body["duration_ms"], int)
    assert await audit_rows(identity, org.org_id, "tool.test") == []


async def test_invalid_input_answers_200_with_details_and_sends_nothing(org: Org, hooks: Hooks) -> None:
    tool_id = await org.tool(hooks)
    response = await org.run(tool_id, {"input": {"party_size": 50}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["error"], body["request"], body["status_code"]) == ("invalid_arguments", None, None)
    assert {detail["path"] for detail in body["details"]} == {"", "party_size"}
    assert hooks.receiver.requests == []


async def test_server_error_is_reported_as_http_error(org: Org, hooks: Hooks) -> None:
    tool_id = await org.tool(hooks, retries=1)
    hooks.receiver.script("/availability", Reply(500, body=b"boom"), Reply(500, body=b"boom"))
    response = await org.run(tool_id, {"input": {"date": "d", "party_size": 2}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status_code"], body["error"], body["response"]) == (500, "http_error", "boom")
    assert body["extracted"] == {"available": None, "times": None, "nope": None}
    assert len(hooks.receiver.requests) == 2


async def test_slow_tool_times_out(org: Org, hooks: Hooks) -> None:
    tool_id = await org.tool(hooks, timeout_ms=200)
    hooks.receiver.script("/availability", Reply(hold=True))
    response = await org.run(tool_id, {"input": {"date": "d", "party_size": 2}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["error"], body["status_code"], body["response"]) == ("timeout", None, None)
    assert len(hooks.receiver.requests) == 1


async def test_unknown_tool_and_other_orgs_tool_are_not_found(
    org: Org, hooks: Hooks, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    missing = await org.run(f"TL-{uuid.uuid4()}")
    assert error_of(missing)[:2] == (404, "TOOL_NOT_FOUND")
    tool_id = await org.tool(hooks)
    other = await star_api(client, identity, container)
    response = await client.post(f"{BASE}/{tool_id}/test", json={}, headers=dict(other.headers))
    assert error_of(response)[:2] == (404, "TOOL_NOT_FOUND")
    assert hooks.receiver.requests == []


async def test_read_scope_is_refused(org: Org, hooks: Hooks, container: SessionContainer) -> None:
    tool_id = await org.tool(hooks)
    key, _ = await store_api_key(container, org.org_id, ("tools:read",))
    response = await org.client.post(f"{BASE}/{tool_id}/test", json={}, headers={"Authorization": key})
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    assert hooks.receiver.requests == []


async def test_unknown_body_fields_are_rejected(org: Org, hooks: Hooks) -> None:
    tool_id = await org.tool(hooks)
    response = await org.run(tool_id, {"inputs": {}})
    assert error_of(response) == (400, "INVALID_INPUT", "inputs")
