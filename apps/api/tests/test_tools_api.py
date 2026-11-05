"""Tools API: create, list, get, update, delete with references, secret headers (specs/13 §2-§3; Decision
P08-T01)."""

from __future__ import annotations

import copy
import secrets
import uuid
from typing import Any, Final

import httpx
import pytest
from call_input_rows import make_ctx
from identity_db import IdentityDb
from pathway_helpers import Api, add, star_api
from session_helpers import (
    SessionContainer,
    audit_rows,
    error_of,
    make_member,
    signed_in,
    store_api_key,
    write_headers,
)

from va_api.routers.tools_schemas import ToolCreate
from va_api.services.tools import ToolDeps, create_tool
from va_common.errors import AppError
from va_common.ids import PublicPrefix, from_public, to_public
from va_db.models import Agent, AgentVersion, Tool
from va_tools.secrets import MASK, decrypt_secret_headers

pytestmark = pytest.mark.integration

BASE: Final = "/v1/tools"
DEFINITION: Final[dict[str, Any]] = {
    "name": "check_availability",
    "description": "Checks table availability for a date, time and party size.",
    "speech": "Let me check that for you.",
    "url": "https://api.bellapasta.test/availability",
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "query": {"source": "voice"},
    "body": {"date": "{{input.date}}", "party": "{{input.party_size}}", "caller": "{{from}}"},
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["date", "party_size"],
    },
    "response": {"available": "$.available", "alternatives": "$.alternatives[*].time"},
    "timeout_ms": 8000,
    "retries": 1,
}


class ToolsApi:
    """The tools routes with an org's API key (a pathways :class:`Api` carries the client and key)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.org_id = api.org_id

    async def _send(self, method: str, path: str = "", **kwargs: Any) -> httpx.Response:
        headers = {**self.api.headers, **kwargs.pop("headers", {})}
        return await self.api.client.request(method, BASE + path, headers=headers, **kwargs)

    async def create(self, body: dict[str, Any], **headers: str) -> httpx.Response:
        return await self._send("POST", json=body, headers=headers)

    async def created(self, **changes: Any) -> dict[str, Any]:
        response = await self.create({**copy.deepcopy(DEFINITION), **changes})
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def get(self, tool_id: str) -> httpx.Response:
        return await self._send("GET", f"/{tool_id}")

    async def list(self, **params: str) -> httpx.Response:
        return await self._send("GET", params=params)

    async def update(self, tool_id: str, body: dict[str, Any]) -> httpx.Response:
        return await self._send("POST", f"/{tool_id}", json=body)

    async def delete(self, tool_id: str, **params: str) -> httpx.Response:
        return await self._send("DELETE", f"/{tool_id}", params=params)


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> ToolsApi:
    return ToolsApi(await star_api(client, identity, container))


def secret_value() -> str:
    return f"Bearer {secrets.token_hex(16)}"


async def stored(identity: IdentityDb, org_id: uuid.UUID, tool_id: str) -> Tool:
    async with identity.database.tenant_session(org_id) as session:
        row = await session.get(Tool, from_public(tool_id, PublicPrefix.TOOL))
        assert row is not None
        return row


# ---- create ------------------------------------------------------------------------------------------------


async def test_create_returns_the_tool_with_masked_secrets(api: ToolsApi, identity: IdentityDb) -> None:
    secret = secret_value()
    response = await api.create({**DEFINITION, "secret_headers": {"Authorization": secret}})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert secret[-4:] not in response.text.replace(MASK + secret[-4:], "")
    body = response.json()
    tool = body["tool"]
    assert body["status"] == "success"
    assert body["tool_id"] == tool["tool_id"]
    assert tool["tool_id"].startswith("TL-")
    assert tool["secret_headers"] == {"Authorization": MASK + secret[-4:]}
    assert {key: tool[key] for key in DEFINITION} == DEFINITION
    row = await stored(identity, api.org_id, tool["tool_id"])
    assert row.response_map == DEFINITION["response"]
    assert row.secret_headers_enc is not None
    assert secret.encode() not in row.secret_headers_enc
    assert await decrypt_secret_headers(identity.envelope, api.org_id, row.id, row.secret_headers_enc) == {
        "Authorization": secret
    }
    (audit_row,) = await audit_rows(identity, api.org_id, "tool.create")
    assert (audit_row.resource_type, audit_row.resource_id) == ("tool", str(row.id))
    assert secret not in str(audit_row.changes)
    assert audit_row.changes["after"]["name"] == "check_availability"


async def test_create_applies_defaults(api: ToolsApi) -> None:
    body = {"name": "hours", "description": "Opening hours", "url": "https://hours.test/now"}
    response = await api.create(body)
    assert response.status_code == 200, response.text
    tool = response.json()["tool"]
    assert {key: tool[key] for key in ("method", "timeout_ms", "retries", "body", "speech")} == {
        "method": "POST",
        "timeout_ms": 10_000,
        "retries": 0,
        "body": None,
        "speech": None,
    }
    assert (tool["headers"], tool["secret_headers"], tool["query"], tool["response"]) == ({}, {}, {}, {})
    assert tool["input_schema"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": "1bad"}, "name"),
        ({"name": "x" * 65}, "name"),
        ({"description": ""}, "description"),
        ({"description": "d" * 1001}, "description"),
        ({"speech": "s" * 501}, "speech"),
        ({"url": "ftp://files.test/x"}, "url"),
        ({"url": "https://a.test/" + "p" * 2048}, "url"),
        ({"method": "TRACE"}, "method"),
        ({"headers": {"Bad Header": "v"}}, "headers"),
        ({"query": {"q": 1}}, "query.q"),
        ({"body": {"blob": "x" * 70_000}}, "body"),
        ({"input_schema": {"type": "array"}}, "input_schema"),
        ({"input_schema": {"type": "object", "properties": {"a": {"type": "nope"}}}}, "input_schema"),
        ({"response": {"available": "$.[[["}}, "response.available"),
        ({"response": {"bad-key": "$.a"}}, "response"),
        ({"timeout_ms": 99}, "timeout_ms"),
        ({"timeout_ms": 60_001}, "timeout_ms"),
        ({"retries": 4}, "retries"),
        ({"name": "end_call"}, "name"),
        ({"name": "lookup_knowledge"}, "name"),
        ({"unknown": 1}, "unknown"),
        ({"secret_headers": {"content-type": "v"}}, "secret_headers.content-type"),
        ({"secret_headers": {"Authorization": "a\nb"}}, "secret_headers.Authorization"),
        ({"secret_headers": {"Bad Name": "v"}}, "secret_headers"),
    ],
)
async def test_create_rejects_invalid_fields(api: ToolsApi, changes: dict[str, Any], field: str) -> None:
    response = await api.create({**DEFINITION, **changes})
    assert error_of(response) == (400, "INVALID_INPUT", field), response.text


async def test_missing_required_fields_are_named(api: ToolsApi) -> None:
    response = await api.create({"name": "only_name"})
    assert response.status_code == 400
    fields = {item.get("field") for item in response.json()["errors"]}
    assert {"description", "url"} <= fields


async def test_http_urls_are_refused_outside_local_and_test(
    identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    deps = ToolDeps(database=identity.database, envelope=identity.envelope, allow_http=False)
    body = ToolCreate.model_validate({**DEFINITION, "url": "http://api.bellapasta.test/availability"})
    with pytest.raises(AppError) as caught:
        await create_tool(deps, make_ctx(member.org.id), body)
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "url")
    https = ToolCreate.model_validate(DEFINITION)
    view = await create_tool(deps, make_ctx(member.org.id), https)
    assert view.url == DEFINITION["url"]


async def test_http_urls_pass_in_test(api: ToolsApi) -> None:
    await api.created(url="http://api.bellapasta.test/availability")


async def test_duplicate_live_name_conflicts_and_a_deleted_name_is_reusable(api: ToolsApi) -> None:
    first = (await api.created())["tool_id"]
    response = await api.create(DEFINITION)
    assert error_of(response) == (409, "CONFLICT", "name")
    assert (await api.delete(first)).status_code == 200
    second = (await api.created())["tool_id"]
    assert second != first


async def test_create_replays_with_the_same_idempotency_key(api: ToolsApi, identity: IdentityDb) -> None:
    key = secrets.token_hex(8)
    first = await api.create(DEFINITION, **{"Idempotency-Key": key})
    again = await api.create(DEFINITION, **{"Idempotency-Key": key})
    assert first.status_code == again.status_code == 200
    assert first.json()["tool_id"] == again.json()["tool_id"]
    assert len(await audit_rows(identity, api.org_id, "tool.create")) == 1


# ---- read --------------------------------------------------------------------------------------------------


async def test_get_accepts_both_id_forms(api: ToolsApi) -> None:
    secret = secret_value()
    tool_id = (await api.created(secret_headers={"X-Api-Key": secret}))["tool_id"]
    bare = str(from_public(tool_id, PublicPrefix.TOOL))
    for value in (tool_id, bare, bare.upper()):
        response = await api.get(value)
        assert response.status_code == 200, response.text
        assert response.json()["tool"]["tool_id"] == tool_id
        assert response.json()["tool"]["secret_headers"] == {"X-Api-Key": MASK + secret[-4:]}
        assert secret not in response.text


@pytest.mark.parametrize("value", ["KB-0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71", "nope", "TL-123"])
async def test_malformed_ids_are_invalid_input(api: ToolsApi, value: str) -> None:
    assert error_of(await api.get(value)) == (400, "INVALID_INPUT", "tool_id")


async def test_unknown_and_foreign_tools_are_not_found(
    api: ToolsApi, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    tool_id = (await api.created())["tool_id"]
    foreign = ToolsApi(await star_api(client, identity, container))
    for response in (
        await foreign.get(tool_id),
        await foreign.update(tool_id, {"retries": 2}),
        await foreign.delete(tool_id),
        await api.get(to_public(PublicPrefix.TOOL, uuid.uuid4())),
    ):
        assert error_of(response) == (404, "TOOL_NOT_FOUND", None)
    assert (await api.get(tool_id)).json()["tool"]["retries"] == 1


async def test_list_search_paging_and_deleted_tools(api: ToolsApi) -> None:
    ids = [
        (await api.created(name=name, description=f"{name} lookup"))["tool_id"] for name in ("a1", "b2", "c3")
    ]
    gone = (await api.created(name="d4"))["tool_id"]
    assert (await api.delete(gone)).status_code == 200
    first = await api.list(limit="2")
    assert first.status_code == 200, first.text
    page = first.json()
    assert [tool["tool_id"] for tool in page["tools"]] == [ids[2], ids[1]]
    second = (await api.list(limit="2", cursor=page["next_cursor"])).json()
    assert [tool["tool_id"] for tool in second["tools"]] == [ids[0]]
    assert second["next_cursor"] is None
    found = (await api.list(search="B2 LOOK")).json()["tools"]
    assert [tool["name"] for tool in found] == ["b2"]
    assert error_of(await api.list(limit="101")) == (400, "INVALID_INPUT", "limit")


# ---- update ------------------------------------------------------------------------------------------------


async def test_partial_update_keeps_other_fields_and_secrets(api: ToolsApi, identity: IdentityDb) -> None:
    secret = secret_value()
    created = await api.created(secret_headers={"Authorization": secret})
    tool_id = created["tool_id"]
    response = await api.update(tool_id, {"description": "New text", "timeout_ms": 5000})
    assert response.status_code == 200, response.text
    tool = response.json()["tool"]
    assert (tool["description"], tool["timeout_ms"]) == ("New text", 5000)
    assert tool["secret_headers"] == {"Authorization": MASK + secret[-4:]}
    assert {key: tool[key] for key in ("name", "url", "body", "response")} == {
        key: DEFINITION[key] for key in ("name", "url", "body", "response")
    }
    (row,) = await audit_rows(identity, api.org_id, "tool.update")
    assert row.changes == {
        "before": {"description": DEFINITION["description"], "timeout_ms": 8000},
        "after": {"description": "New text", "timeout_ms": 5000},
    }


async def test_null_clears_speech_body_and_secrets(api: ToolsApi, identity: IdentityDb) -> None:
    secret = secret_value()
    tool_id = (await api.created(secret_headers={"Authorization": secret}))["tool_id"]
    response = await api.update(tool_id, {"speech": None, "body": None, "secret_headers": None})
    assert response.status_code == 200, response.text
    tool = response.json()["tool"]
    assert (tool["speech"], tool["body"], tool["secret_headers"]) == (None, None, {})
    row = await stored(identity, api.org_id, tool_id)
    assert row.secret_headers_enc is None
    (audit_row,) = await audit_rows(identity, api.org_id, "tool.update")
    assert audit_row.changes["after"]["speech"] is None
    assert secret not in str(audit_row.changes)


@pytest.mark.parametrize(
    "field", ["name", "description", "url", "method", "headers", "input_schema", "retries"]
)
async def test_null_is_refused_for_other_fields(api: ToolsApi, field: str) -> None:
    tool_id = (await api.created())["tool_id"]
    response = await api.update(tool_id, {field: None})
    assert error_of(response) == (400, "INVALID_INPUT", field), response.text


async def test_secret_headers_replace_the_whole_set(api: ToolsApi, identity: IdentityDb) -> None:
    tool_id = (await api.created(secret_headers={"Authorization": secret_value(), "X-Old": "short"}))[
        "tool_id"
    ]
    new_secret = secret_value()
    response = await api.update(tool_id, {"secret_headers": {"X-Api-Key": new_secret}})
    assert response.status_code == 200, response.text
    assert response.json()["tool"]["secret_headers"] == {"X-Api-Key": MASK + new_secret[-4:]}
    row = await stored(identity, api.org_id, tool_id)
    assert await decrypt_secret_headers(identity.envelope, api.org_id, row.id, row.secret_headers_enc) == {
        "X-Api-Key": new_secret
    }
    (audit_row,) = await audit_rows(identity, api.org_id, "tool.update")
    assert audit_row.changes == {
        "before": {"secret_headers": "[REDACTED]"},
        "after": {"secret_headers": "[REDACTED]"},
    }


async def test_a_short_secret_change_is_still_audited(api: ToolsApi, identity: IdentityDb) -> None:
    tool_id = (await api.created(secret_headers={"X-Pin": "1234"}))["tool_id"]
    response = await api.update(tool_id, {"secret_headers": {"X-Pin": "9876"}})
    assert response.status_code == 200, response.text
    assert response.json()["tool"]["secret_headers"] == {"X-Pin": MASK}
    (audit_row,) = await audit_rows(identity, api.org_id, "tool.update")
    assert audit_row.changes["after"] == {"secret_headers": "[REDACTED]"}


async def test_update_validates_the_merged_definition(api: ToolsApi) -> None:
    tool_id = (await api.created())["tool_id"]
    cases: list[tuple[dict[str, Any], str]] = [
        ({"name": "end_call"}, "name"),
        ({"response": {"x": "$.[["}}, "response.x"),
        ({"input_schema": {"type": "object", "required": 3}}, "input_schema"),
        ({"secret_headers": {"Content-Type": "v"}}, "secret_headers.Content-Type"),
        ({"timeout_ms": 50}, "timeout_ms"),
        ({"bogus": True}, "bogus"),
    ]
    for body, field in cases:
        assert error_of(await api.update(tool_id, body)) == (400, "INVALID_INPUT", field), body
    # A plain header that collides with a stored secret header is refused too.
    with_secret = (await api.created(name="other", secret_headers={"X-Api-Key": secret_value()}))["tool_id"]
    response = await api.update(with_secret, {"headers": {"x-api-key": "plain"}})
    assert error_of(response) == (400, "INVALID_INPUT", "secret_headers.X-Api-Key")


async def test_rename_conflicts_with_a_live_tool(api: ToolsApi) -> None:
    await api.created(name="taken")
    tool_id = (await api.created(name="mine"))["tool_id"]
    assert error_of(await api.update(tool_id, {"name": "taken"})) == (409, "CONFLICT", "name")
    renamed = await api.update(tool_id, {"name": "renamed"})
    assert renamed.json()["tool"]["name"] == "renamed"


async def test_a_no_op_update_writes_nothing(api: ToolsApi, identity: IdentityDb) -> None:
    secret = secret_value()
    created = (await api.created(secret_headers={"Authorization": secret}))["tool"]
    body = {key: DEFINITION[key] for key in ("name", "retries", "body")} | {
        "secret_headers": {"Authorization": secret}
    }
    response = await api.update(created["tool_id"], body)
    assert response.status_code == 200, response.text
    assert response.json()["tool"]["updated_at"] == created["updated_at"]
    assert await audit_rows(identity, api.org_id, "tool.update") == []


# ---- delete ------------------------------------------------------------------------------------------------


async def test_delete_unreferenced_tool(api: ToolsApi, identity: IdentityDb) -> None:
    tool_id = (await api.created())["tool_id"]
    response = await api.delete(tool_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "message": "Tool deleted."}
    assert error_of(await api.get(tool_id)) == (404, "TOOL_NOT_FOUND", None)
    assert error_of(await api.delete(tool_id)) == (404, "TOOL_NOT_FOUND", None)
    (row,) = await audit_rows(identity, api.org_id, "tool.delete")
    assert row.changes["after"] == {"name": DEFINITION["name"], "force": False, "references": 0}


async def test_referenced_tool_needs_force(api: ToolsApi, identity: IdentityDb) -> None:
    tool_id = (await api.created())["tool_id"]
    agent = Agent(org_id=api.org_id, name="Front desk", env_pins={})
    await add(identity, api.org_id, agent)
    version = AgentVersion(
        org_id=api.org_id, agent_id=agent.id, semver="1.0.0", call_settings={"tools": [tool_id]}
    )
    await add(identity, api.org_id, version)
    response = await api.delete(tool_id)
    assert error_of(response) == (409, "CONFLICT", None)
    item = response.json()["errors"][0]
    assert item["details"]["references"] == [{"type": "agent", "id": str(agent.id), "version": "1.0.0"}]
    assert (await api.get(tool_id)).status_code == 200
    forced = await api.delete(tool_id, force="true")
    assert forced.status_code == 200, forced.text
    (row,) = await audit_rows(identity, api.org_id, "tool.delete")
    assert row.changes["after"]["force"] is True


# ---- scopes and roles --------------------------------------------------------------------------------------


async def test_read_scope_cannot_write(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, api: ToolsApi
) -> None:
    tool_id = (await api.created())["tool_id"]
    key, _ = await store_api_key(container, api.org_id, ("tools:read",))
    reader = ToolsApi(Api(client, api.org_id, key))
    assert (await reader.get(tool_id)).status_code == 200
    assert (await reader.list()).status_code == 200
    assert error_of(await reader.create({**DEFINITION, "name": "x"}))[:2] == (403, "FORBIDDEN")
    assert error_of(await reader.update(tool_id, {"retries": 0}))[:2] == (403, "FORBIDDEN")
    assert error_of(await reader.delete(tool_id))[:2] == (403, "FORBIDDEN")


@pytest.mark.parametrize(("role", "writes"), [("developer", True), ("analyst", False), ("viewer", False)])
async def test_session_roles_follow_the_rbac_matrix(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: str, writes: bool
) -> None:
    member = await signed_in(client, identity, container, role)  # type: ignore[arg-type]
    listing = await client.get(BASE)
    assert listing.status_code == 200, listing.text
    response = await client.post(BASE, json=DEFINITION, headers=write_headers(client))
    assert (response.status_code == 200) is writes, response.text
    assert member.org.id is not None
