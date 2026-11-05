"""Resource factories of the cross-tenant route suite (specs/04 §14 decision, P02-T15).

Each factory creates one resource in the factory context's tenant and returns the id that fills its path
parameter. A later phase whose routes take a new path parameter registers its factory here (or in a module
imported from here); the suite reports any path parameter without one.
"""

from __future__ import annotations

import uuid
from typing import Any

import orjson
from call_rows import seed_call
from cross_tenant import REGISTRY, FactoryContext
from identity_db import unique_email
from session_helpers import add_member, store_api_key, sub_account

from va_api.services.pathway_chat import DEFAULT_SIMULATE_FROM, DEFAULT_SIMULATE_TO, ChatSession, ChatStore
from va_common.ids import PublicPrefix, new_id, to_public
from va_db.models import PathwayTestRun, WebhookDelivery, WebhookEndpoint
from va_db.repositories import (
    InvitationRepo,
    PathwayFolderRepo,
    PathwayGraphJson,
    PathwayRepo,
    PathwayTestRepo,
    ToolRepo,
)
from va_engine.models import EngineState, dump_engine_state, graph_hash

__all__ = ["REGISTRY"]


async def _member(context: FactoryContext) -> str:
    """A developer member of the tenant's org."""
    user = await add_member(
        context.identity, context.container.password_hasher, context.tenant.org_id, "developer"
    )
    return str(user.id)


async def _invitation(context: FactoryContext) -> str:
    """An open developer invitation sent by the tenant's owner."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        row, _ = await InvitationRepo(session, org_id).create(
            email=unique_email("invitee"),
            role="developer",
            invited_by=context.tenant.owner.user.id,
            now=context.container.clock.now(),
        )
    return str(row.id)


async def _api_key(context: FactoryContext) -> str:
    """A second ``*`` key of the tenant's org."""
    _, row = await store_api_key(context.container, context.tenant.org_id)
    return str(row.id)


async def _sub_account(context: FactoryContext) -> str:
    """A sub-account created through ``POST /v1/orgs/create`` with the tenant's key."""
    child_id, _ = await sub_account(context.client, Authorization=context.tenant.api_key)
    return str(child_id)


async def _call(context: FactoryContext) -> str:
    """An ``in_progress`` call of the tenant's org (stopping it publishes a control message)."""
    call = await seed_call(context.identity.database, status="in_progress", org_id=context.tenant.org_id)
    return str(call.call_id)


# A closed port on an IP literal: no DNS, never leaves the host, and the owner's test send fails fast.
_CLOSED_HOOK = "http://127.0.0.1:9/hook"


async def _webhook_endpoint(context: FactoryContext) -> str:
    """An enabled webhook endpoint of the tenant's org."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        endpoint = WebhookEndpoint(org_id=org_id, url=_CLOSED_HOOK)
        session.add(endpoint)
        await session.flush()
        return str(endpoint.id)


async def _webhook_delivery(context: FactoryContext) -> str:
    """A ``dead`` delivery of the tenant's org (no endpoint), so the owner's replay succeeds."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        delivery = WebhookDelivery(
            org_id=org_id,
            event_id=f"evt_{new_id().hex}",
            event_type="call.completed",
            url=_CLOSED_HOOK,
            payload={"status": "completed"},
            status="dead",
            attempts=9,
            last_error="http_status",
        )
        session.add(delivery)
        await session.flush()
        return str(delivery.id)


async def _pathway(context: FactoryContext) -> str:
    """A pathway of the tenant's org with a one-node draft (unreferenced, so the owner may delete it)."""
    org_id = context.tenant.org_id
    nodes: list[Any] = [
        {"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "text": "Hi"}}
    ]
    graph: dict[str, Any] = {"nodes": nodes, "edges": [], "global_config": {}}
    async with context.identity.database.tenant_session(org_id) as session:
        pathway, _ = await PathwayRepo(session, org_id).create(
            name="Cross-tenant pathway",
            description="",
            folder_id=None,
            graph=PathwayGraphJson(nodes=nodes, edges=[], global_config={}, graph_hash=graph_hash(graph)),
        )
        return str(pathway.id)


async def _tool(context: FactoryContext) -> str:
    """A tool of the tenant's org that nothing references (``TL-`` form, as the api returns it)."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        tool = await ToolRepo(session, org_id).create(
            {
                "name": f"cross_tenant_{new_id().hex}",
                "description": "Cross-tenant tool",
                "url": "https://hooks.test/tool",
            }
        )
        return to_public(PublicPrefix.TOOL, tool.id)


async def _folder(context: FactoryContext) -> str:
    """A root pathway folder of the tenant's org."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        folder = await PathwayFolderRepo(session, org_id).create(name="Cross-tenant folder", parent_id=None)
        return str(folder.id)


async def _chat(context: FactoryContext) -> str:
    """An ended pathway test chat of the tenant's org (written straight to Redis: no model needed)."""
    chat_id, pathway_id = new_id(), new_id()
    state = EngineState(
        runner="pathway",
        pathway_id=pathway_id,
        pathway_version=1,
        current_node_id="start",
        visit_counts={"start": 1},
        ended=True,
    )
    document = orjson.loads(dump_engine_state(state))
    session = ChatSession(
        org_id=context.tenant.org_id,
        pathway_id=pathway_id,
        version=1,
        graph={"nodes": [], "edges": [], "global_config": {}},
        request_data={},
        simulate_from=DEFAULT_SIMULATE_FROM,
        simulate_to=DEFAULT_SIMULATE_TO,
        created_at=context.container.clock.now(),
        state=document,
        snapshots={"0": document},
    )
    await ChatStore(context.container.redis).save(chat_id, session)
    return str(chat_id)


async def _unit_test(context: FactoryContext) -> str:
    """A unit test of the pathway created for ``{pathway_id}`` to its left."""
    org_id = context.tenant.org_id
    pathway_id = uuid.UUID(context.params["pathway_id"])
    async with context.identity.database.tenant_session(org_id) as session:
        test = await PathwayTestRepo(session, org_id).add(
            pathway_id,
            {
                "name": "Cross-tenant test",
                "node_id": "start",
                "history": [],
                "user_message": "Hi",
                "expected": {},
            },
        )
        return str(test.id)


async def _test_run(context: FactoryContext) -> str:
    """A finished unit run of the pathway created for ``{pathway_id}`` to its left."""
    org_id = context.tenant.org_id
    run = PathwayTestRun(
        org_id=org_id,
        pathway_id=uuid.UUID(context.params["pathway_id"]),
        version_number=1,
        kind="unit",
        status="passed",
        results=[],
    )
    async with context.identity.database.tenant_session(org_id) as session:
        session.add(run)
        await session.flush()
        return str(run.id)


async def _first_version(context: FactoryContext) -> str:
    """Version 1, which every pathway has."""
    del context
    return "1"


REGISTRY.register_resource_factory("user_id", _member)
REGISTRY.register_resource_factory("invitation_id", _invitation)
REGISTRY.register_resource_factory("key_id", _api_key)
REGISTRY.register_resource_factory("org_id", _sub_account)
REGISTRY.register_resource_factory("call_id", _call)
REGISTRY.register_resource_factory("endpoint_id", _webhook_endpoint)
REGISTRY.register_resource_factory("delivery_id", _webhook_delivery)
REGISTRY.register_resource_factory("pathway_id", _pathway)
REGISTRY.register_resource_factory("version_number", _first_version, path_prefix="/v1/pathway/", marker=False)
# The factory pathway has no staging version, so the owner's promote control is ``409 CONFLICT`` while org B
# gets ``NOT_FOUND`` (Decision P07-T09b).
REGISTRY.register_owner_status("pathways_promote", 409)
REGISTRY.register_resource_factory("folder_id", _folder)
REGISTRY.register_resource_factory("tool_id", _tool)
REGISTRY.register_resource_factory("chat_id", _chat)
REGISTRY.register_resource_factory("test_id", _unit_test)
REGISTRY.register_resource_factory("run_id", _test_run)
# The from_call example names a call that exists nowhere: the owner's control is ``404 CALL_NOT_FOUND``, while
# org B is refused at the pathway (``404 NOT_FOUND``) before any call is read.
REGISTRY.register_owner_status("pathways_tests_from_call", 404)
# The factory pathway has no unit tests: the owner's run control is ``400 INVALID_INPUT`` ("There are no unit
# tests to run."), while org B is refused at the pathway (``404 NOT_FOUND``); no workflow starts for either.
REGISTRY.register_owner_status("pathways_tests_run", 400)
# The factory chat has ended, so the owner's send control is ``409 CONFLICT`` (no model call) while org B gets
# ``NOT_FOUND``.
REGISTRY.register_request("pathways_chat_send", json={"message": "Hi"})
REGISTRY.register_owner_status("pathways_chat_send", 409)
# The move example names a pathway id that exists nowhere; an empty move is the owner's no-op control.
REGISTRY.register_request("pathways_folders_move", json={"pathway_ids": []})
# The duplicate example copies the production pin, which the factory pathway does not have.
REGISTRY.register_request("pathways_duplicate", json={"name": "Copy"})
# The revert example names version 2; the factory pathway only has version 1.
REGISTRY.register_request("pathways_revert", json={"version_number": 1})
# ``GET /v1/calls`` takes its filters as one query model, which the route walker sees as a required parameter;
# the unfiltered listing must hold none of org A's calls.
REGISTRY.register_request("calls_list", params={"limit": "1000"})
# The call artifact routes take one query model each (seen as required); the owner's seeded call has no
# recording, so its recording control is ``404 NOT_FOUND`` while org B gets ``CALL_NOT_FOUND``.
REGISTRY.register_request("calls_recording", params={"format": "json"})
REGISTRY.register_request("calls_transcript", params={"format": "json"})
REGISTRY.register_request("calls_events", params={"limit": "1000"})
REGISTRY.register_owner_status("calls_recording", 404)
# The call event stream tails an in-progress call until it completes, which an in-process client would wait
# for: the owner control sends an invalid ``Last-Event-ID`` (checked after ownership), so it answers 400 at
# once while org B still gets ``CALL_NOT_FOUND``.
REGISTRY.register_request("calls_events_stream", headers={"Last-Event-ID": "not-a-seq"})
REGISTRY.register_owner_status("calls_events_stream", 400)
# An unbounded stream never ends through ASGITransport (it buffers the body): the owner control sends a
# malformed filter, which is checked after authentication and the scope.
REGISTRY.register_request("events_stream", params={"types": "BAD!"})
REGISTRY.register_owner_status("events_stream", 400)
