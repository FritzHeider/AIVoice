"""va_pathways test helpers: a text-run rig over FakeLLM and the unit-test graph (specs/12 §8 Decision
P07-T11a).

:func:`text_rig` serves FakeLLM on a real socket behind a real :class:`~va_llm.LlmGateway` (manual clock: its
deadlines never fire) and gives :class:`~va_pathways.conversation.TextDeps` on the wall clock (the turn cap)
with a plain tool HTTP client. :func:`bella_pasta` is the tested graph: FakeLLM routes by edge label words.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from graph_helpers import edge, node, start
from llm_client_helpers import served_gateway, served_requests
from llm_helpers import add_rules, realtime_llm_state
from navigation_helpers import MemoryLoader

from va_common.time import SystemClock
from va_db.repositories import PathwayRepo, PathwayTestRepo
from va_db.repositories.pathways import PathwayGraphJson
from va_db.session import Database
from va_domain.org import OrgSettings
from va_engine.models import dump_graph, graph_hash, load_graph
from va_engine.navigation import ActiveGraph
from va_llm.tokens import default_counter
from va_pathways.conversation import TextDeps
from va_pathways.unit_tests import UnitTestResult, UnitTestSpec, run_unit_test

__all__ = [
    "GREETING",
    "ORG",
    "PATHWAY",
    "LlmControl",
    "TextRig",
    "bella_pasta",
    "spec",
    "store_bella_pasta",
    "store_unit_test",
    "text_rig",
]

ORG: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b02")
PATHWAY: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b03")
GREETING: Final = "Hi, Bella Pasta."
NO_SPECULATION: Final = OrgSettings.model_validate({"engine": {"speculative": False}})


def bella_pasta(hook_port: int = 9) -> ActiveGraph:
    """Greeting → order (extracts ``pizza_size``), the delivery-status webhook ``hook`` (``eta``), goodbye."""
    hook = {
        "url": f"http://127.0.0.1:{hook_port}/status",
        "method": "POST",
        "body": {"caller": "{{from}}"},
        "speech": "Let me check that for you.",
        "responseData": [{"name": "eta", "data": "$.eta"}],
    }
    nodes = [
        start(
            "greet",
            name="Greeting",
            text=GREETING,
            extractVars=[{"name": "pizza_size", "type": "string", "description": "Pizza size"}],
        ),
        node("order", name="Take Order", text="Great, what toppings would you like?"),
        node(
            "hook",
            name="Delivery Status",
            node_type="Webhook",
            webhook=hook,
            text="It arrives in {{eta}} minutes.",
        ),
        node("bye", name="Goodbye", node_type="End Call", endCall={"speech": "Goodbye!"}),
    ]
    edges = [
        edge("e_order", "greet", "order", "Caller wants to order food"),
        edge("e_hook", "greet", "hook", "Caller asks about delivery status"),
        edge("e_bye", "greet", "bye", "Caller says goodbye"),
    ]
    return ActiveGraph(PATHWAY, 1, load_graph(nodes, edges, {}))


def spec(user_message: str = "I want to order some food", **fields: Any) -> UnitTestSpec:
    """A test at ``greet`` after the greeting."""
    body: dict[str, Any] = {
        "name": "Routes to order",
        "node_id": "greet",
        "history": [{"role": "assistant", "content": GREETING}],
        "user_message": user_message,
        **fields,
    }
    return UnitTestSpec.model_validate(body)


async def store_bella_pasta(database: Database, org_id: uuid.UUID) -> uuid.UUID:
    """A pathway of ``org_id`` whose version 1 (the draft) is :func:`bella_pasta`."""
    graph = dump_graph(bella_pasta().graph)
    stored = PathwayGraphJson(
        nodes=list(graph["nodes"]),
        edges=list(graph["edges"]),
        global_config=dict(graph["global_config"]),
        graph_hash=graph_hash(graph),
    )
    async with database.tenant_session(org_id) as session:
        pathway, _ = await PathwayRepo(session, org_id).create(
            name="Bella Pasta", description="", folder_id=None, graph=stored
        )
        return pathway.id


async def store_unit_test(
    database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, **values: Any
) -> uuid.UUID:
    """A stored unit test at ``greet`` that expects the ``order`` route (``values`` override columns)."""
    row = {
        "name": "Routes to order",
        "node_id": "greet",
        "history": [{"role": "assistant", "content": GREETING}],
        "user_message": "I want to order some food",
        "expected": {"route_to_node_id": "order"},
        "simulated_variations": 0,
        **values,
    }
    async with database.tenant_session(org_id) as session:
        return (await PathwayTestRepo(session, org_id).add(pathway_id, row)).id


class LlmControl:
    """FakeLLM's control API on ``control`` (rules and the request log); shared by the api chat stack."""

    control: httpx.AsyncClient

    async def rules(self, yaml_text: str) -> None:
        await add_rules(self.control, yaml_text)

    async def requests(self) -> list[dict[str, Any]]:
        """FakeLLM's request log."""
        return await served_requests(self.control)


@dataclass(slots=True)
class TextRig(LlmControl):
    deps: TextDeps
    control: httpx.AsyncClient
    loader: MemoryLoader = field(default_factory=MemoryLoader)

    async def run(
        self,
        test: UnitTestSpec,
        active: ActiveGraph | None = None,
        *,
        org: OrgSettings = NO_SPECULATION,
        test_id: str = "PT-test",
    ) -> UnitTestResult:
        return await run_unit_test(
            self.deps,
            test,
            test_id=test_id,
            org_id=ORG,
            active=active if active is not None else bella_pasta(),
            org=org,
            loader=self.loader,
            counter=default_counter(),
        )


@asynccontextmanager
async def text_rig() -> AsyncIterator[TextRig]:
    state = realtime_llm_state()
    try:
        async with served_gateway(state) as (rig, control), httpx.AsyncClient() as tools_http:
            deps = TextDeps(gateway=rig.gateway, tools_http=tools_http, clock=SystemClock(), allow_http=True)
            yield TextRig(deps, control)
    finally:
        await state.aclose()
