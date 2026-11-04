"""Pathway API access for e2e flows: one org's ``*`` key against the api container (create, draft update,
unit tests, suite settings, publish, runs, versions). Every call answers the raw response (publish may be a
409), except :meth:`PathwayApi.published`.

The bundled templates live in ``tests_e2e/pathways/`` (Bland camelCase graph JSON, specs/30 §5 Decision
P07-T12c); their Webhook nodes are pointed at the run's receiver with :func:`with_webhook_url`. Pathway calls
are sent with :func:`pathway_call`; :func:`route_targets` and :func:`events_of` read the call's event log.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx
from e2e_harness import E2eOrg, FinishedCall, Harness, RegisteredScenario

__all__ = [
    "PATHWAYS",
    "PATHWAYS_DIR",
    "PathwayApi",
    "events_of",
    "load_template",
    "ok",
    "ordering_graph",
    "pathway_call",
    "route_targets",
    "run_pathway_call",
    "webhook_graph",
    "webhook_node_data",
    "with_webhook_url",
]

PATHWAYS: Final = "/v1/pathway"
PATHWAYS_DIR: Final = Path(__file__).resolve().parents[1] / "pathways"
OK: Final = 200
ROUTE_EVENT: Final = "call.route"


def ok(response: httpx.Response) -> dict[str, Any]:
    """The JSON body of a 200 response (AssertionError with the body otherwise)."""
    assert response.status_code == OK, f"{response.status_code}: {response.text}"
    body: dict[str, Any] = response.json()
    return body


def load_template(name: str) -> dict[str, Any]:
    """``tests_e2e/pathways/<name>.json``: ``nodes``, ``edges`` and ``global_config``."""
    graph: dict[str, Any] = json.loads((PATHWAYS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return graph


def with_webhook_url(graph: dict[str, Any], url: str) -> dict[str, Any]:
    """A deep copy of ``graph`` whose Webhook nodes post to ``url`` (nothing else changes)."""
    rewritten = copy.deepcopy(graph)
    for node in rewritten["nodes"]:
        if node["type"] == "Webhook":
            node["data"]["webhook"]["url"] = url
    return rewritten


def webhook_graph(url: str) -> dict[str, Any]:
    """Offer → Webhook node (speaks the extracted ``status``/``ref``, then waits) → goodbye (webhook_node)."""
    nodes = [
        {
            "id": "start",
            "type": "Default",
            "data": {
                "name": "Offer",
                "isStart": True,
                "text": "Hi, this is Ava from Bella Pasta. Shall I confirm your booking?",
            },
        },
        {
            "id": "confirm",
            "type": "Webhook",
            "data": {
                "name": "Confirm Booking",
                "text": "Your booking is {{status}}, reference {{ref}}.",
                "webhook": {
                    "url": url,
                    "method": "POST",
                    "body": {"call": "{{call_id}}"},
                    "timeoutMs": 5000,
                    "responseData": [
                        {"name": "status", "data": "$.status"},
                        {"name": "ref", "data": "$.ref"},
                    ],
                },
            },
        },
        {"id": "bye", "type": "End Call", "data": {"name": "Goodbye", "endCall": {"speech": "Goodbye!"}}},
    ]
    edges = [
        {
            "id": "e_confirm",
            "source": "start",
            "target": "confirm",
            "data": {"label": "Caller wants the booking confirmed"},
        },
        {"id": "e_bye", "source": "confirm", "target": "bye", "data": {"label": "Caller says goodbye"}},
    ]
    return {"nodes": nodes, "edges": edges}


def pathway_call(org: E2eOrg, scenario: RegisteredScenario, pathway_id: str) -> dict[str, Any]:
    """A Send Call body running ``pathway_id`` from the org's number to the scenario's number; the opener
    plays at once (``wait_for_greeting`` false), nothing is recorded."""
    return {
        "phone_number": scenario.number,
        "from": org.number,
        "pathway_id": pathway_id,
        "voice": org.voice,
        "wait_for_greeting": False,
        "record": False,
    }


def events_of(events: Sequence[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    """The event log entries of ``event_type``, in log order."""
    return [event for event in events if event["type"] == event_type]


def route_targets(events: Sequence[dict[str, Any]]) -> list[str]:
    """The ``to_node`` of every ``call.route`` event, in log order."""
    return [str(event["data"]["to_node"]) for event in events_of(events, ROUTE_EVENT)]


def ordering_graph(order_target: str) -> dict[str, list[dict[str, Any]]]:
    """Greeting → ordering or goodbye (Bland camelCase nodes); the "order food" edge leads to
    ``order_target``. FakeLLM routes by the words of the edge labels, so no rules are needed."""
    nodes = [
        {
            "id": "greet",
            "type": "Default",
            "data": {"name": "Greeting", "isStart": True, "text": "Hi, thanks for calling Bella Pasta."},
        },
        {"id": "order", "type": "Default", "data": {"name": "Take Order", "text": "What would you like?"}},
        {"id": "bye", "type": "End Call", "data": {"name": "Goodbye", "endCall": {"speech": "Goodbye!"}}},
    ]
    edges = [
        {
            "id": "e_order",
            "source": "greet",
            "target": order_target,
            "data": {"label": "Caller wants to order food"},
        },
        {"id": "e_bye", "source": "greet", "target": "bye", "data": {"label": "Caller says goodbye"}},
    ]
    return {"nodes": nodes, "edges": edges}


class PathwayApi:
    """The pathway routes of one org."""

    def __init__(self, http: httpx.AsyncClient, api_base: str, org: E2eOrg) -> None:
        self._http = http
        self._base = f"{api_base}{PATHWAYS}"
        self._headers = org.headers

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return await self._http.post(f"{self._base}{path}", json=body, headers=self._headers)

    async def _get(self, path: str) -> httpx.Response:
        return await self._http.get(f"{self._base}{path}", headers=self._headers)

    async def create(self, name: str, graph: dict[str, Any]) -> httpx.Response:
        """``POST /create`` with the graph's ``global_config`` (``{}`` when it has none)."""
        return await self._post("/create", {"global_config": {}, **graph, "name": name})

    async def published(self, name: str, graph: dict[str, Any]) -> str:
        """Create the pathway and publish its draft to production; the pathway id."""
        pathway_id = str(ok(await self.create(name, graph))["pathway_id"])
        ok(await self.publish(pathway_id))
        return pathway_id

    async def update_graph(self, pathway_id: str, graph: dict[str, Any]) -> httpx.Response:
        """Replace the draft's nodes and edges (its ``global_config`` keeps the suite settings)."""
        return await self._post(f"/{pathway_id}", graph)

    async def add_unit_test(self, pathway_id: str, body: dict[str, Any]) -> httpx.Response:
        return await self._post(f"/{pathway_id}/tests", body)

    async def put_regression(self, pathway_id: str, body: dict[str, Any]) -> httpx.Response:
        return await self._http.put(f"{self._base}/{pathway_id}/regression", json=body, headers=self._headers)

    async def publish(self, pathway_id: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self._post(f"/{pathway_id}/publish", body or {})

    async def run(self, pathway_id: str, run_id: str) -> httpx.Response:
        return await self._get(f"/{pathway_id}/runs/{run_id}")

    async def versions(self, pathway_id: str) -> httpx.Response:
        return await self._get(f"/{pathway_id}/versions")


def webhook_node_data(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The data of the call's single ``call.webhook_node`` event (AssertionError for none or several)."""
    found = events_of(events, "call.webhook_node")
    assert len(found) == 1, found
    data: dict[str, Any] = found[0]["data"]
    return data


async def run_pathway_call(
    harness: Harness, org: E2eOrg, scenario: RegisteredScenario, *, name: str, graph: dict[str, Any]
) -> FinishedCall:
    """Publish ``graph`` as pathway ``name``, call the scenario's number with it, and wait for the end."""
    pathways = PathwayApi(harness.http, harness.urls.api, org)
    pathway_id = await pathways.published(name, graph)
    call_id = await harness.send_call(org, pathway_call(org, scenario, pathway_id))
    return await harness.finished(org, call_id, number=scenario.number)
