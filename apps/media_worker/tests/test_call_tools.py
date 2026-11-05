"""The custom tools of a task-mode call on the media database (specs/13 §5; Decision P08-T02b1)."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from media_helpers import call_context
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError
from tenant_rows import open_database
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.time import FrozenClock, utcnow
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore, ToolRepo
from va_db.repositories.identity import NewUser
from va_db.session import Database
from va_engine.templating import TemplateScope
from va_events.webhook_signing import signature_headers
from va_llm.models import ToolCall
from va_media.call_tools import CallToolSource, load_call_tools
from va_tools.secrets import encrypt_secret_headers

pytestmark = pytest.mark.integration

INLINE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"order": {"type": "string"}}}


@dataclass
class Rig:
    database: Database
    envelope: Envelope
    provisioner: OrgProvisioner
    secrets: dict[uuid.UUID, str] = field(default_factory=dict)

    async def org(self) -> uuid.UUID:
        provisioned = await self.provisioner.provision(
            name="Tools Inc", owner=NewUser(email=f"owner-{secrets.token_hex(4)}@tools.test")
        )
        self.secrets[provisioned.org.id] = provisioned.webhook_signing_secret
        return provisioned.org.id

    async def tool(
        self, org_id: uuid.UUID, name: str, *, url: str = "https://api.tools.test/x", **values: Any
    ) -> uuid.UUID:
        tool_id = uuid.uuid4()
        hidden = values.pop("secret_headers", {})
        blob = await encrypt_secret_headers(self.envelope, org_id, tool_id, hidden)
        async with self.database.tenant_session(org_id) as session:
            await ToolRepo(session, org_id).create(
                {
                    "name": name,
                    "description": f"Tool {name}",
                    "url": url,
                    "secret_headers_enc": blob,
                    **values,
                },
                tool_id=tool_id,
            )
        return tool_id

    def source(self, http: httpx.AsyncClient, database: Any = None) -> CallToolSource:
        return CallToolSource(
            database=database if database is not None else self.database,
            envelope=self.envelope,
            tools_http=http,
            allow_http=True,
            clock=FrozenClock(utcnow()),
        )


@asynccontextmanager
async def rig(media_db_url: str) -> AsyncIterator[Rig]:
    async with open_database(media_db_url) as database:
        envelope = Envelope(LocalKmsProvider.from_hex(secrets.token_hex(32)), OrgDataKeyStore(database))
        yield Rig(database, envelope, OrgProvisioner(database, envelope))


class _BrokenDatabase:
    """A database whose sessions fail to open."""

    def tenant_session(self, _org_id: uuid.UUID) -> _BrokenDatabase:
        return self

    def global_session(self) -> _BrokenDatabase:
        return self

    async def __aenter__(self) -> None:
        raise OperationalError("SELECT", None, ConnectionError("down"))

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _inline(name: str, url: str) -> dict[str, Any]:
    return {"name": name, "description": f"Inline {name}", "url": url, "input_schema": INLINE_SCHEMA}


async def test_stored_tools_load_in_order_with_decrypted_secret_headers(media_db_url: str) -> None:
    stream = json_log_stream()
    async with rig(media_db_url) as r:
        org_id, other = await r.org(), await r.org()
        first = await r.tool(org_id, "first", secret_headers={"Authorization": "Bearer s3cret"})
        second = await r.tool(org_id, "second", headers={"X-Plain": "yes"}, speech="One sec.")
        gone = await r.tool(org_id, "gone")
        foreign = await r.tool(other, "theirs")
        async with r.database.tenant_session(org_id) as session:
            repo = ToolRepo(session, org_id)
            await repo.soft_delete(await repo.get(gone))
        specs = await load_call_tools(r.database, r.envelope, org_id, [second, gone, foreign, first])
    assert [(s.name, s.tool_id) for s in specs] == [("second", f"TL-{second}"), ("first", f"TL-{first}")]
    assert dict(specs[1].secret_headers) == {"Authorization": "Bearer s3cret"}
    assert (dict(specs[0].headers), specs[0].speech) == ({"X-Plain": "yes"}, "One sec.")
    assert "s3cret" not in repr(specs)
    [missing] = [e for e in log_events(stream) if e["event"] == "media.tool_missing"]
    assert missing["count"] == 2


async def test_an_inline_tool_replaces_a_stored_namesake_and_requests_are_signed(media_db_url: str) -> None:
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b'{"ok": true}'))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        org_id = await r.org()
        stored = await r.tool(org_id, "lookup", secret_headers={"X-Key": "stored-secret"})
        kept = await r.tool(
            org_id, "kept", url=f"http://127.0.0.1:{port}/kept", secret_headers={"X-Key": "k"}
        )
        context = call_context(
            uuid.uuid4(),
            org_id=org_id,
            tool_ids=[str(stored), str(kept)],
            inline_tools=[_inline("lookup", f"http://127.0.0.1:{port}/inline")],
        )
        executor = await r.source(http).executor(context, None)
        assert executor is not None
        assert [d.name for d in executor.definitions()] == ["kept", "lookup"]
        for name in ("lookup", "kept"):
            call = ToolCall(id=f"c_{name}", name=name, arguments_json='{"order": "A7"}')
            answer = await executor.execute(call, TemplateScope())
            assert json.loads(answer.content)["status"] == 200
    inline, kept_request = receiver.requests
    assert inline.path == "/inline"
    assert "x-key" not in inline.headers
    assert kept_request.headers["x-key"] == "k"
    assert kept_request.headers["x-va-call-id"] == str(context.call_id)
    signature = kept_request.headers["x-va-signature"]
    timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
    expected = signature_headers([r.secrets[org_id]], kept_request.body, timestamp)
    assert signature == expected["X-VA-Signature"]


async def test_a_call_without_tools_gets_no_executor(media_db_url: str) -> None:
    async with rig(media_db_url) as r, httpx.AsyncClient() as http:
        org_id = await r.org()
        assert await r.source(http).executor(call_context(uuid.uuid4(), org_id=org_id), None) is None
        context = call_context(uuid.uuid4(), org_id=org_id, tool_ids=[str(uuid.uuid4())])
        assert await r.source(http).executor(context, None) is None


async def test_a_broken_database_leaves_the_inline_tools_unsigned(media_db_url: str) -> None:
    stream = json_log_stream()
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b"{}"))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        context = call_context(
            uuid.uuid4(),
            org_id=uuid.uuid4(),
            tool_ids=[str(uuid.uuid4())],
            inline_tools=[_inline("lookup", f"http://127.0.0.1:{port}/inline")],
        )
        executor = await r.source(http, database=_BrokenDatabase()).executor(context, None)
        assert executor is not None
        assert [d.name for d in executor.definitions()] == ["lookup"]
        call = ToolCall(id="c1", name="lookup", arguments_json="{}")
        await executor.execute(call, TemplateScope())
    assert "x-va-signature" not in receiver.requests[0].headers
    events = {e["event"]: e for e in log_events(stream)}
    assert events["media.tools_unavailable"]["error_type"] == "OperationalError"
    assert events["media.tool_signing_unavailable"]["error_type"] == "OperationalError"


async def test_signing_is_skipped_when_the_org_is_gone(media_db_url: str) -> None:
    stream = json_log_stream()
    async with rig(media_db_url) as r, httpx.AsyncClient() as http:
        context = call_context(
            uuid.uuid4(), org_id=uuid.uuid4(), inline_tools=[_inline("lookup", "http://127.0.0.1:9/x")]
        )
        assert await r.source(http).executor(context, None) is not None
    [event] = [e for e in log_events(stream) if e["event"] == "media.tool_signing_unavailable"]
    assert event["error_type"] == "OrgMissing"
