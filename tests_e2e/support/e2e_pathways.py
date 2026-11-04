"""Pathway API access for e2e flows: one org's ``*`` key against the api container (create, draft update,
unit tests, suite settings, publish, runs, versions). Every call answers the raw response (publish may be a
409)."""

from __future__ import annotations

from typing import Any, Final

import httpx
from e2e_harness import E2eOrg

__all__ = ["PATHWAYS", "PathwayApi", "ordering_graph"]

PATHWAYS: Final = "/v1/pathway"


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
        return await self._post("/create", {"name": name, **graph, "global_config": {}})

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
