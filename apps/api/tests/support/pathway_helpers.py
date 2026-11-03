"""Shared helpers of the pathways API tests (specs/12-pathways-api.md)."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
from identity_db import IdentityDb
from session_helpers import SessionContainer, make_member, store_api_key

from va_db.models import Pathway
from va_db.repositories import PathwayGraphJson, PathwayRepo

__all__ = ["BASE", "START_ONLY", "Api", "add", "seed_pathway", "star_api"]

BASE: Final = "/v1/pathway"


class Api:
    """An org's ``*`` API key on the shared client."""

    def __init__(self, client: httpx.AsyncClient, org_id: uuid.UUID, key: str) -> None:
        self.client = client
        self.org_id = org_id
        self.headers = {"Authorization": key}

    async def create(self, **body: Any) -> httpx.Response:
        return await self.client.post(
            f"{BASE}/create", json={"name": "Bella Pasta", **body}, headers=self.headers
        )

    async def created(self, **body: Any) -> str:
        response = await self.create(**body)
        assert response.status_code == 200, response.text
        assert response.json()["version_number"] == 1
        return str(response.json()["pathway_id"])

    async def get(self, pathway_id: str, **params: str) -> httpx.Response:
        return await self.client.get(f"{BASE}/{pathway_id}", params=params, headers=self.headers)

    async def list(self, **params: str) -> httpx.Response:
        return await self.client.get(BASE, params=params, headers=self.headers)

    async def update(self, pathway_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}", json=body, headers=self.headers)

    async def delete(self, pathway_id: str, **params: str) -> httpx.Response:
        return await self.client.delete(f"{BASE}/{pathway_id}", params=params, headers=self.headers)

    async def validate(self, pathway_id: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/validate", json=body, headers=self.headers)

    async def versions(self, pathway_id: str) -> httpx.Response:
        return await self.client.get(f"{BASE}/{pathway_id}/versions", headers=self.headers)

    async def version(self, pathway_id: str, number: int | str) -> httpx.Response:
        return await self.client.get(f"{BASE}/{pathway_id}/version/{number}", headers=self.headers)

    async def save(self, pathway_id: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/version", json=body, headers=self.headers)

    async def publish(self, pathway_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/publish", json=body, headers=self.headers)

    async def promote(self, pathway_id: str) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/promote", headers=self.headers)

    async def revert(self, pathway_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/revert", json=body, headers=self.headers)


async def add(identity: IdentityDb, org_id: uuid.UUID, *rows: Any) -> None:
    async with identity.database.tenant_session(org_id) as session:
        for row in rows:
            session.add(row)
            await session.flush()


async def star_api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    """A new org with a ``*`` API key on ``client``."""
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id)
    return Api(client, member.org.id, key)


START_ONLY: Final[list[Any]] = [
    {"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "text": "Hi"}}
]


async def seed_pathway(identity: IdentityDb, org_id: uuid.UUID, nodes: list[Any] | None = None) -> Pathway:
    """A pathway named ``Orders`` inserted directly (for principals that may not create one)."""
    async with identity.database.tenant_session(org_id) as session:
        pathway, _ = await PathwayRepo(session, org_id).create(
            name="Orders",
            description="",
            folder_id=None,
            graph=PathwayGraphJson(
                nodes=list(START_ONLY if nodes is None else nodes),
                edges=[],
                global_config={},
                graph_hash="0" * 64,
            ),
        )
        return pathway
