"""The custom tools of a call on the media database: task mode (specs/13 §5; Decision P08-T02b1) and the
stored tools of a pathway call's graphs (specs/13 §4; Decision P08-T02c2)."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from clock_helpers import ManualClock
from graph_helpers import graph, node, start
from media_helpers import call_context
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError
from tenant_rows import open_database
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.time import Clock, FrozenClock, SystemClock, utcnow
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore, ToolRepo
from va_db.repositories.identity import NewUser
from va_db.session import Database
from va_engine.models import PathwayGraph
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_engine.templating import TemplateScope
from va_events.webhook_signing import signature_headers
from va_llm.models import ToolCall
from va_media.call_tools import SWITCH_LOAD_TIMEOUT_S, CallToolSource, load_call_tools
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

    def source(
        self, http: httpx.AsyncClient, database: Any = None, clock: Clock | None = None
    ) -> CallToolSource:
        return CallToolSource(
            database=database if database is not None else self.database,
            envelope=self.envelope,
            tools_http=http,
            allow_http=True,
            clock=clock if clock is not None else FrozenClock(utcnow()),
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


# Pathway calls: the stored tools of the graphs the call runs on (Decision P08-T02c2).


def _pathway_context(org_id: uuid.UUID) -> Any:
    return call_context(uuid.uuid4(), org_id=org_id, runner_type="pathway")


def _signed_by(request: Any, secret: str) -> bool:
    signature = request.headers["x-va-signature"]
    timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
    return bool(signature == signature_headers([secret], request.body, timestamp)["X-VA-Signature"])


async def _run(executor: Any, name: str) -> int:
    call = ToolCall(id=f"c_{name}", name=name, arguments_json="{}")
    return int(json.loads((await executor.execute(call, TemplateScope())).content)["status"])


async def test_pathway_tools_load_the_graphs_stored_tools(media_db_url: str) -> None:
    stream = json_log_stream()
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b'{"ok": true}'))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        org_id, other = await r.org(), await r.org()
        base = f"http://127.0.0.1:{port}"
        kept = await r.tool(
            org_id, "kept", url=f"{base}/kept", secret_headers={"X-Key": "k"}, speech="Checking."
        )
        shared = await r.tool(org_id, "shared", url=f"{base}/shared")
        foreign = await r.tool(other, "theirs", url=f"{base}/theirs")
        pathway = graph(
            [
                start("a", prompt="Help.", tools=[f"TL-{kept}", f"TL-{foreign}"]),
                node("b", prompt="More.", tools=[_inline("lookup", f"{base}/inline"), str(foreign)]),
            ],
            [],
            {"globalTools": [str(shared)]},
        )
        tools = await r.source(http).pathway_tools(_pathway_context(org_id), pathway, None)
        node_a = tools.executor.for_refs([f"TL-{kept}", f"TL-{foreign}", str(shared)])
        node_b = tools.executor.for_refs([_inline("lookup", f"{base}/inline"), str(foreign), str(shared)])
        assert [d.name for d in node_a.definitions()] == ["kept", "shared"]
        assert [d.name for d in node_b.definitions()] == ["lookup", "shared"]
        assert tools.executor.tool_speech == "Checking."
        assert [await _run(node_a, "kept"), await _run(node_b, "lookup"), await _run(node_b, "shared")] == [
            200,
            200,
            200,
        ]
        await tools.ensure(pathway)
    kept_request, inline_request, shared_request = receiver.requests
    assert (kept_request.path, inline_request.path, shared_request.path) == ("/kept", "/inline", "/shared")
    assert kept_request.headers["x-key"] == "k"
    assert "x-key" not in inline_request.headers
    assert all(_signed_by(request, r.secrets[org_id]) for request in receiver.requests)
    # The foreign id resolved to nothing and, like every id, was looked up once for the whole call.
    missing = [e["count"] for e in log_events(stream) if e["event"] == "media.tool_missing"]
    assert missing == [1]


class _Graphs:
    """A pathway loader over fixed graphs (``PathwayUnavailableError`` for any other id)."""

    def __init__(self, **graphs: PathwayGraph) -> None:
        self._graphs = graphs

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        del version
        found = self._graphs.get(pathway_id)
        if found is None:
            raise PathwayUnavailableError("not_found")
        return ActiveGraph(None, None, found)


@dataclass
class _Switch:
    """A pathway call on a pinned graph without tools whose loader knows ``other``, a graph holding the
    stored tool ``later`` (served by ``receiver``)."""

    rig: Rig
    org_id: uuid.UUID
    ref: str
    other: PathwayGraph
    receiver: Receiver
    tools: Any
    loader: Any

    def definitions(self) -> list[str]:
        return [d.name for d in self.tools.executor.for_refs([self.ref]).definitions()]

    async def run(self) -> int:
        return await _run(self.tools.executor.for_refs([self.ref]), "later")

    def signed(self) -> bool:
        return _signed_by(self.receiver.requests[0], self.rig.secrets[self.org_id])


@asynccontextmanager
async def _switch(media_db_url: str, wrap: Any = None, clock: Clock | None = None) -> AsyncIterator[_Switch]:
    """``wrap`` replaces the rig's database for the call's tool source; ``clock`` is the call clock the
    load at a graph switch is timed on (the wall clock unless given)."""
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b"{}"))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        org_id = await r.org()
        later = await r.tool(org_id, "later", url=f"http://127.0.0.1:{port}/later")
        other = graph([start("x", prompt="Help.", tools=[f"TL-{later}"])], [])
        source = r.source(
            http,
            database=None if wrap is None else wrap(r.database),
            clock=clock if clock is not None else SystemClock(),
        )
        pinned = graph([start("a", text="Hi.")], [])
        tools = await source.pathway_tools(_pathway_context(org_id), pinned, None)
        yield _Switch(r, org_id, f"TL-{later}", other, receiver, tools, tools.loader(_Graphs(other=other)))


async def test_a_graph_loaded_later_brings_its_tools(media_db_url: str) -> None:
    async with _switch(media_db_url) as call:
        assert call.definitions() == []

        loaded = await call.loader.load("other", "production")

        assert loaded.graph is call.other
        assert call.definitions() == ["later"]
        assert await call.run() == 200
        with pytest.raises(PathwayUnavailableError):
            await call.loader.load("gone", "production")
    assert len(call.receiver.requests) == 1
    assert call.signed()


async def test_a_failed_pathway_tool_load_keeps_the_inline_tools(media_db_url: str) -> None:
    stream = json_log_stream()
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b"{}"))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        inline = _inline("lookup", f"http://127.0.0.1:{port}/inline")
        pathway = graph([start("a", prompt="Help.", tools=[str(uuid.uuid4()), inline])], [])
        source = r.source(http, database=_BrokenDatabase())
        tools = await source.pathway_tools(_pathway_context(uuid.uuid4()), pathway, None)
        scoped = tools.executor.for_refs(pathway.nodes[0].data.tools)
        assert [d.name for d in scoped.definitions()] == ["lookup"]
        assert await _run(scoped, "lookup") == 200
    assert "x-va-signature" not in receiver.requests[0].headers
    events = {e["event"]: e for e in log_events(stream)}
    assert events["media.tools_unavailable"]["error_type"] == "OperationalError"
    assert events["media.tool_signing_unavailable"]["error_type"] == "OperationalError"


class _FirstSessions:
    """The rig's database, except that the first session of each kind in ``kinds`` (``tenant``, ``global``)
    is ``instead``."""

    def __init__(self, database: Database, kinds: set[str], instead: Any) -> None:
        self._database = database
        self._kinds = set(kinds)
        self._instead = instead

    def tenant_session(self, org_id: uuid.UUID) -> Any:
        return self._first("tenant") or self._database.tenant_session(org_id)

    def global_session(self) -> Any:
        return self._first("global") or self._database.global_session()

    def _first(self, kind: str) -> Any:
        if kind not in self._kinds:
            return None
        self._kinds.discard(kind)
        return self._instead


def _fails_once(database: Database) -> _FirstSessions:
    """A database whose first tenant session and first global session fail to open."""
    return _FirstSessions(database, {"tenant", "global"}, _BrokenDatabase())


def _unavailable(stream: Any) -> list[tuple[str, str]]:
    """The tool and signing load failures logged, in order."""
    names = ("media.tools_unavailable", "media.tool_signing_unavailable")
    return [(e["event"], e["error_type"]) for e in log_events(stream) if e["event"] in names]


async def test_a_failed_tool_load_is_tried_again_by_the_next_load_of_the_graph(media_db_url: str) -> None:
    stream = json_log_stream()
    async with _switch(media_db_url, _fails_once) as call:
        await call.loader.load("other", "production")
        assert call.definitions() == []

        await call.loader.load("other", "production")

        assert call.definitions() == ["later"]
        assert await call.run() == 200
    # Neither the tool lookup nor the signing-secret read that failed counted as done.
    assert call.signed()
    assert _unavailable(stream) == [
        ("media.tools_unavailable", "OperationalError"),
        ("media.tool_signing_unavailable", "OperationalError"),
    ]


class _CountingDatabase:
    """The rig's database, counting the global sessions opened."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.global_sessions = 0

    def tenant_session(self, org_id: uuid.UUID) -> Any:
        return self._database.tenant_session(org_id)

    def global_session(self) -> Any:
        self.global_sessions += 1
        return self._database.global_session()


async def test_the_signing_secrets_of_an_org_that_is_gone_are_read_once(media_db_url: str) -> None:
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b"{}"))
    async with rig(media_db_url) as r, served_receiver(receiver) as port, httpx.AsyncClient() as http:
        pathway = graph(
            [start("a", prompt="Help.", tools=[_inline("lookup", f"http://127.0.0.1:{port}/inline")])], []
        )
        counting = _CountingDatabase(r.database)
        source = r.source(http, database=counting)
        tools = await source.pathway_tools(_pathway_context(uuid.uuid4()), pathway, None)
        await tools.ensure(pathway)
        assert await _run(tools.executor.for_refs(pathway.nodes[0].data.tools), "lookup") == 200
    assert counting.global_sessions == 1
    assert "x-va-signature" not in receiver.requests[0].headers


class _Held:
    """A session that never opens (until cancelled); ``wrap(kind)`` puts it in place of a database's first
    ``tenant`` or ``global`` session."""

    def __init__(self) -> None:
        self.waiting = asyncio.Event()

    def wrap(self, kind: str) -> Any:
        return lambda database: _FirstSessions(database, {kind}, self)

    async def __aenter__(self) -> None:
        self.waiting.set()
        await asyncio.Event().wait()

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_a_cancelled_tool_load_is_tried_again_by_the_next_load_of_the_graph(media_db_url: str) -> None:
    held = _Held()
    async with _switch(media_db_url, held.wrap("tenant")) as call:
        cut = asyncio.create_task(call.loader.load("other", "production"))
        await held.waiting.wait()
        cut.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cut
        assert call.definitions() == []

        await call.loader.load("other", "production")

        assert call.definitions() == ["later"]
        assert await call.run() == 200
    assert call.signed()


async def _timed_out(clock: ManualClock, held: _Held, load: asyncio.Task[ActiveGraph]) -> ActiveGraph:
    """Let ``load`` (parked on ``held``'s session) run out of the time a graph switch has."""
    await held.waiting.wait()
    await clock.wait_for_sleep(SWITCH_LOAD_TIMEOUT_S)
    await clock.run_for(SWITCH_LOAD_TIMEOUT_S)
    return await load


async def test_a_tool_load_at_a_graph_switch_gives_up_after_its_deadline(media_db_url: str) -> None:
    stream = json_log_stream()
    held, clock = _Held(), ManualClock()
    async with _switch(media_db_url, held.wrap("tenant"), clock) as call:
        slow = asyncio.create_task(call.loader.load("other", "production"))
        loaded = await _timed_out(clock, held, slow)

        # The switch goes on without the graph's stored tools; the lookup did not count as done.
        assert loaded.graph is call.other
        assert call.definitions() == []
        assert _unavailable(stream) == [("media.tools_unavailable", "TimeoutError")]

        await call.loader.load("other", "production")

        assert call.definitions() == ["later"]
        assert await call.run() == 200
    assert call.signed()


async def test_a_signing_read_at_a_graph_switch_shares_the_deadline_and_is_tried_again(
    media_db_url: str,
) -> None:
    stream = json_log_stream()
    held, clock = _Held(), ManualClock()
    async with _switch(media_db_url, held.wrap("global"), clock) as call:
        slow = asyncio.create_task(call.loader.load("other", "production"))
        await _timed_out(clock, held, slow)

        assert call.definitions() == ["later"]
        assert _unavailable(stream) == [("media.tool_signing_unavailable", "TimeoutError")]
        assert await call.run() == 200
        assert "x-va-signature" not in call.receiver.requests[0].headers

        await call.loader.load("other", "production")

        assert await call.run() == 200
    assert _signed_by(call.receiver.requests[1], call.rig.secrets[call.org_id])
