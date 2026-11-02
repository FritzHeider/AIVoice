"""Rows for the call input loader tests (`va_db.call_inputs`, the api Send Call loader).

Every builder writes in its own `tenant_session` (platform voices through the system session) and returns the
new id. E.164 numbers are random per call so tests on the shared module database never collide on the
active-number uniqueness index.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Self

from tenant_rows import OrgFactory, RowFactory

from va_common.errors import AppError
from va_db.models import (
    Agent,
    AgentVersion,
    NumberPool,
    NumberPoolMember,
    Pathway,
    PathwayVersion,
    Persona,
    PersonaVersion,
    PhoneNumber,
    Voice,
)
from va_db.session import Database
from va_domain.call_resolution import PlatformModels
from va_domain.calls import SendCallRequest

__all__ = [
    "MODELS",
    "START_NODE",
    "Seed",
    "error_of",
    "graph",
    "random_e164",
    "request_of",
]

MODELS: Final = PlatformModels(dialogue="zai-org/GLM-5.3-Flash", router="zai-org/GLM-5.3-Flash")
START_NODE: Final = "greet"
BASE_TIME: Final = datetime(2026, 10, 1, tzinfo=UTC)


def random_e164(npa: str = "415") -> str:
    """A random valid-looking `+1` number in area code `npa` (exchange 200-999)."""
    exchange = 200 + secrets.randbelow(800)
    return f"+1{npa}{exchange}{secrets.randbelow(10_000):04d}"


def graph(*node_ids: str, start: str = START_NODE) -> list[dict[str, Any]]:
    """Pathway nodes: `start` (marked `isStart`) plus `node_ids`."""
    nodes = [{"id": start, "type": "Default", "data": {"name": start, "isStart": True}}]
    nodes.extend({"id": node_id, "type": "Default", "data": {"name": node_id}} for node_id in node_ids)
    return nodes


def request_of(body: Mapping[str, Any]) -> SendCallRequest:
    return SendCallRequest.model_validate({"phone_number": "+14155550100", "task": "Hi", **body})


def error_of(caught: BaseException) -> tuple[str, str | None, str]:
    assert isinstance(caught, AppError)
    return caught.code, caught.field, caught.message


class Seed:
    """An org of its own on the module database plus builders for every row the loader reads."""

    def __init__(self, database: Database, org_id: uuid.UUID) -> None:
        self.database = database
        self.org_id = org_id
        self._rows = RowFactory()
        self._clock = BASE_TIME

    @classmethod
    async def create(cls, database: Database) -> Self:
        return cls(database, await OrgFactory(database).create())

    async def row(self, model: Any, **values: Any) -> uuid.UUID:
        """Insert a minimal valid row of `model` in this org with `values` on top; returns its id."""
        async with self.database.tenant_session(self.org_id) as session:
            base = await self._rows.values(session, model.__table__, self.org_id)
            key = await self._rows.insert_values(session, model.__table__, {**base, **values})
        result = key.get("id")
        assert isinstance(result, uuid.UUID)
        return result

    def _tick(self) -> datetime:
        self._clock += timedelta(minutes=1)
        return self._clock

    async def pathway(
        self,
        versions: Mapping[int, Mapping[str, Any]] | None = None,
        *,
        production: int | None = 1,
        staging: int | None = None,
        draft: int = 1,
        deleted: bool = False,
    ) -> uuid.UUID:
        """A pathway whose versions map number -> `{"nodes": ..., "global_config": ...}` (default: v1)."""
        specs = versions if versions is not None else {1: {}}
        pathway_id = uuid.uuid4()
        async with self.database.tenant_session(self.org_id) as session:
            session.add(
                Pathway(
                    id=pathway_id,
                    org_id=self.org_id,
                    name=f"Pathway {secrets.token_hex(3)}",
                    production_version=production,
                    staging_version=staging,
                    draft_version=draft,
                    latest_version=max(specs),
                    deleted_at=self._tick() if deleted else None,
                )
            )
            await session.flush()
            for number, spec in specs.items():
                session.add(
                    PathwayVersion(
                        org_id=self.org_id,
                        pathway_id=pathway_id,
                        version_number=number,
                        status="published" if number == production else "draft",
                        nodes=list(spec.get("nodes", graph())),
                        edges=[],
                        global_config=dict(spec.get("global_config", {})),
                        graph_hash=secrets.token_hex(32),
                    )
                )
        return pathway_id

    async def persona(
        self,
        versions: Mapping[int, Mapping[str, Any]] | None = None,
        *,
        production: int | None = 1,
        draft: int = 1,
    ) -> uuid.UUID:
        """A persona whose versions map number -> config (default: v1 with a prompt)."""
        specs = versions if versions is not None else {1: {"runner": "prompt", "prompt": "Be kind."}}
        persona_id = uuid.uuid4()
        async with self.database.tenant_session(self.org_id) as session:
            session.add(
                Persona(
                    id=persona_id,
                    org_id=self.org_id,
                    name=f"Persona {secrets.token_hex(3)}",
                    production_version=production,
                    draft_version=draft,
                    latest_version=max(specs),
                )
            )
            await session.flush()
            for number, config in specs.items():
                session.add(
                    PersonaVersion(
                        org_id=self.org_id,
                        persona_id=persona_id,
                        version_number=number,
                        status="published" if number == production else "draft",
                        config=dict(config),
                    )
                )
        return persona_id

    async def agent(
        self, versions: Sequence[Mapping[str, Any]], *, env_pins: Mapping[str, str] | None = None
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """An agent and its versions (created in list order, one minute apart) -> (agent id, version ids)."""
        agent_id = uuid.uuid4()
        version_ids: list[uuid.UUID] = []
        async with self.database.tenant_session(self.org_id) as session:
            session.add(Agent(id=agent_id, org_id=self.org_id, name="Agent", env_pins=dict(env_pins or {})))
            await session.flush()
            for spec in versions:
                version = AgentVersion(
                    id=uuid.uuid4(),
                    org_id=self.org_id,
                    agent_id=agent_id,
                    semver=spec["semver"],
                    branch=spec.get("branch", "main"),
                    pathway_id=spec.get("pathway_id"),
                    pathway_version=spec.get("pathway_version"),
                    call_settings=dict(spec.get("call_settings", {})),
                    created_at=self._tick(),
                )
                session.add(version)
                version_ids.append(version.id)
        return agent_id, version_ids

    async def voice(self, name: str, *, platform: bool = False, **values: Any) -> uuid.UUID:
        """An org voice (or a platform voice, written through the system session)."""
        voice = Voice(
            id=uuid.uuid4(),
            org_id=None if platform else self.org_id,
            name=name,
            provider_voice_id=secrets.token_hex(10),
            **values,
        )
        sessions = self.database.system_session() if platform else self.database.tenant_session(self.org_id)
        async with sessions as session:
            session.add(voice)
        return voice.id

    async def number(self, e164: str | None = None, **values: Any) -> tuple[uuid.UUID, str]:
        """An active voice number of the org (platform account unless `telnyx_account_id` is given)."""
        chosen = e164 or random_e164()
        defaults: dict[str, Any] = {
            "e164": chosen,
            "country_code": "US",
            "status": "active",
            "capabilities": ["voice", "sms"],
            "telnyx_account_id": None,
        }
        number_id = await self.row(PhoneNumber, **{**defaults, **values})
        return number_id, chosen

    async def pool(self, members: Sequence[tuple[uuid.UUID, datetime | None]]) -> uuid.UUID:
        pool_id = await self.row(NumberPool, name=f"pool-{secrets.token_hex(3)}")
        async with self.database.tenant_session(self.org_id) as session:
            for number_id, last_used in members:
                session.add(
                    NumberPoolMember(
                        pool_id=pool_id, phone_number_id=number_id, org_id=self.org_id, last_used_at=last_used
                    )
                )
        return pool_id
