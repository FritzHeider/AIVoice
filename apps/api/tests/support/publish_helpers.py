"""Publishing with the regression gate in api tests: :class:`Gate` wraps one org's pathway (unit tests, suite
settings, publish, runs, the production pin, run rows, and publish audits)."""

from __future__ import annotations

from typing import Any, Final

import httpx
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, add_unit_test, run_of, star_api
from session_helpers import SessionContainer, audit_rows
from sqlalchemy import select

from va_db.models import PathwayTestRun

__all__ = ["SCENARIO", "TEST", "Gate", "gate_of"]

TEST: Final[dict[str, Any]] = {"node_id": "start", "user_message": "Hello"}
SCENARIO: Final[dict[str, Any]] = {"name": "Caller", "persona": "In a hurry.", "goal": "Order placed"}


class Gate:
    def __init__(self, api: Api, container: SessionContainer, identity: IdentityDb, pathway_id: str) -> None:
        self.api = api
        self.container = container
        self.identity = identity
        self.pathway_id = pathway_id
        self.base = f"{BASE}/{pathway_id}"

    async def add_test(self, name: str) -> str:
        return await add_unit_test(self.api, self.pathway_id, {**TEST, "name": name})

    async def settings(self, **body: Any) -> None:
        response = await self.api.client.put(f"{self.base}/regression", json=body, headers=self.api.headers)
        assert response.status_code == 200, response.text

    async def publish(self, **body: Any) -> httpx.Response:
        return await self.api.publish(self.pathway_id, **body)

    async def run(self, run_id: str) -> dict[str, Any]:
        return run_of(await self.api.client.get(f"{self.base}/runs/{run_id}", headers=self.api.headers))

    async def production(self) -> int | None:
        detail = (await self.api.get(self.pathway_id)).json()
        versions = (await self.api.versions(self.pathway_id)).json()["versions"]
        assert detail["pathway_id"] == self.pathway_id
        pinned = [item["version_number"] for item in versions if item["is_production"]]
        return pinned[0] if pinned else None

    async def run_rows(self) -> list[PathwayTestRun]:
        async with self.identity.database.tenant_session(self.api.org_id) as session:
            return list(await session.scalars(select(PathwayTestRun)))

    async def publish_audits(self) -> int:
        return len(await audit_rows(self.identity, self.api.org_id, "pathway.publish"))

    @property
    def started(self) -> list[Any]:
        return self.container.pathway_test_starter.started


async def gate_of(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, **pathway: Any
) -> Gate:
    """A new org with a ``*`` key and one pathway (``pathway``: the create body, default one node)."""
    api = await star_api(client, identity, container)
    return Gate(api, container, identity, await api.created(**pathway))
