"""Org providers from ``org_llm_providers`` over a migrated Postgres (specs/10 §1; Decision P03-T13a
rule 3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from docker_probe import docker_available
from identity_db import IdentityDb, open_identity_db
from migration_helpers import fresh_database, upgrade
from pg_container import running_postgres
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.models import OrgLlmProvider
from va_llm import OrgProviderError, OrgProviderStore, encrypt_org_api_key
from va_llm.org_providers import api_key_aad

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

MODELS = {"dialogue": "org/dlg", "router": "org/rtr", "analysis": "org/ana", "fallback": "org/fb"}


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    if not docker_available():
        pytest.skip("docker unavailable")
    with running_postgres() as server_url, fresh_database(server_url) as url:
        upgrade(url)
        yield url


@pytest.fixture
async def identity(migrated_url: str) -> AsyncIterator[IdentityDb]:
    async with open_identity_db(migrated_url) as db:
        yield db


class CountingSessions:
    """Counts tenant sessions opened through the store (a cache hit opens none)."""

    def __init__(self, identity: IdentityDb) -> None:
        self._database = identity.database
        self.opened = 0

    def tenant_session(self, org_id: uuid.UUID) -> AbstractAsyncContextManager[AsyncSession]:
        self.opened += 1
        return self._database.tenant_session(org_id)


async def _org(identity: IdentityDb) -> uuid.UUID:
    return (await identity.provisioner.provision(name=f"org-{uuid.uuid4().hex[:6]}", owner=None)).org.id


async def _store_row(identity: IdentityDb, org_id: uuid.UUID, api_key: str, **values: Any) -> uuid.UUID:
    row_id = new_id()
    sealed = await encrypt_org_api_key(identity.envelope, org_id, row_id, api_key)
    async with identity.database.tenant_session(org_id) as session:
        session.add(
            OrgLlmProvider(
                id=row_id,
                org_id=org_id,
                base_url=values.get("base_url", "https://llm.example.org/v1/"),
                api_key_enc=sealed,
                models=values.get("models", MODELS),
                supports_tool_choice=values.get("supports_tool_choice", False),
            )
        )
    return row_id


def _store(identity: IdentityDb, clock: FrozenClock) -> tuple[OrgProviderStore, CountingSessions]:
    sessions = CountingSessions(identity)
    return OrgProviderStore(sessions, identity.envelope, clock=clock, ttl_s=60), sessions


async def test_a_stored_row_becomes_the_org_provider(identity: IdentityDb) -> None:
    org_id = await _org(identity)
    await _store_row(identity, org_id, "org-secret")
    store, _ = _store(identity, FrozenClock())
    provider = await store.get(org_id)
    assert provider is not None
    assert (provider.id, provider.base_url, provider.models) == (
        f"org:{org_id}",
        "https://llm.example.org/v1",
        MODELS,
    )
    assert provider.api_key.get_secret_value() == "org-secret"
    assert provider.supports_tool_choice is False
    assert "org-secret" not in repr(provider)


async def test_orgs_without_a_row_get_none(identity: IdentityDb) -> None:
    store, _ = _store(identity, FrozenClock())
    assert await store.get(await _org(identity)) is None


async def test_hits_and_misses_are_cached_for_the_ttl(identity: IdentityDb) -> None:
    org_id, empty_org = await _org(identity), await _org(identity)
    await _store_row(identity, org_id, "first")
    clock = FrozenClock()
    store, sessions = _store(identity, clock)
    assert await store.get(org_id) is not None
    assert await store.get(empty_org) is None
    clock.advance(59)
    await store.get(org_id)
    await store.get(empty_org)
    assert sessions.opened == 2
    async with identity.database.tenant_session(org_id) as session:
        await session.execute(
            update(OrgLlmProvider)
            .where(OrgLlmProvider.org_id == org_id)
            .values(base_url="https://new.example.org/v1")
        )
    clock.advance(1)
    refreshed = await store.get(org_id)
    assert refreshed is not None
    assert refreshed.base_url == "https://new.example.org/v1"
    assert sessions.opened == 3


async def test_invalidate_drops_the_cached_entry(identity: IdentityDb) -> None:
    org_id = await _org(identity)
    store, sessions = _store(identity, FrozenClock())
    assert await store.get(org_id) is None
    await _store_row(identity, org_id, "added-later")
    assert await store.get(org_id) is None
    store.invalidate(org_id)
    store.invalidate(uuid.uuid4())
    provider = await store.get(org_id)
    assert provider is not None
    assert provider.api_key.get_secret_value() == "added-later"
    assert sessions.opened == 2


async def test_a_key_sealed_for_another_row_does_not_decrypt(identity: IdentityDb) -> None:
    org_id = await _org(identity)
    other_row = new_id()
    sealed = await identity.envelope.encrypt_str(org_id, "moved", api_key_aad(other_row))
    async with identity.database.tenant_session(org_id) as session:
        session.add(
            OrgLlmProvider(
                org_id=org_id, base_url="https://x.example.org/v1", api_key_enc=sealed, models=MODELS
            )
        )
    store, sessions = _store(identity, FrozenClock())
    with pytest.raises(OrgProviderError) as raised:
        await store.get(org_id)
    assert (raised.value.provider_id, raised.value.cause) == (f"org:{org_id}", "DecryptionError")
    assert "moved" not in str(raised.value)
    with pytest.raises(OrgProviderError):  # failures are not cached
        await store.get(org_id)
    assert sessions.opened == 2


async def test_a_stored_models_map_missing_a_role_is_a_lookup_error(identity: IdentityDb) -> None:
    org_id = await _org(identity)
    partial = {role: model for role, model in MODELS.items() if role != "fallback"}
    await _store_row(identity, org_id, "org-secret", models=partial)
    store, _ = _store(identity, FrozenClock())
    with pytest.raises(OrgProviderError) as raised:
        await store.get(org_id)
    assert raised.value.cause == "ValidationError"
    assert "org-secret" not in str(raised.value)


async def test_an_unknown_model_role_is_a_lookup_error(identity: IdentityDb) -> None:
    org_id = await _org(identity)
    await _store_row(identity, org_id, "org-secret", models={**MODELS, "summarizer": "org/sum"})
    store, _ = _store(identity, FrozenClock())
    with pytest.raises(OrgProviderError) as raised:
        await store.get(org_id)
    assert raised.value.cause == "ValidationError"


async def test_rls_keeps_each_org_to_its_own_row(identity: IdentityDb) -> None:
    org_a, org_b = await _org(identity), await _org(identity)
    await _store_row(identity, org_a, "a-key")
    store, _ = _store(identity, FrozenClock())
    assert await store.get(org_b) is None
    async with identity.database.tenant_session(org_b) as session:
        rows = (await session.execute(OrgLlmProvider.__table__.select())).all()
    assert rows == []
    provider = await store.get(org_a)
    assert provider is not None
    assert provider.api_key.get_secret_value() == "a-key"
