"""``va_api.dev_seed`` against a real database: the dev org, owner, credit, and rolled keys (specs/02
``scripts/seed_dev.py`` Decision "Dev seed (P02-T14)")."""

from __future__ import annotations

import uuid

import fakeredis
import httpx
import pytest
from api_helpers import make_settings
from identity_db import IdentityDb, row_counts, unique_email
from session_helpers import SessionContainer, add_member
from sqlalchemy import delete, select

import seed_dev
from va_api.auth.api_key_auth import cache_key
from va_api.auth.api_keys import drop_cached_keys
from va_api.dev_seed import (
    CREDITS_IDEMPOTENCY_KEY,
    DEV_CREDITS_MICROS,
    DEV_EMAIL,
    DEV_ORG_NAME,
    DEV_PLAN_ID,
    KEY_NAMES,
    SEED_REFUSED_MESSAGE,
    DevSeeder,
    SeededKey,
    SeedRefusedError,
    SeedResult,
    check_seed_environment,
    render_text,
)
from va_common.errors import AppError
from va_common.settings import Environment
from va_db.models import ApiKey, AuditLog, CreditLedger, Membership, Org, User

pytestmark = pytest.mark.integration


def seeder(container: SessionContainer) -> DevSeeder:
    return DevSeeder(
        database=container.database,
        provisioner=container.provisioner,
        api_key_hasher=container.api_key_hasher,
        password_hasher=container.password_hasher,
        clock=container.clock,
    )


async def authenticates(client: httpx.AsyncClient, key: str) -> dict[str, object] | None:
    """The ``/v1/me`` data for ``key``, or ``None`` when it is rejected."""
    response = await client.get("/v1/me", headers={"Authorization": key})
    assert response.status_code in {200, 401}, response.text
    return response.json()["data"] if response.status_code == 200 else None


async def org_keys(identity: IdentityDb, org_id: uuid.UUID) -> list[ApiKey]:
    async with identity.database.system_session() as session:
        statement = select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at, ApiKey.id)
        return list((await session.scalars(statement)).all())


async def audit_actions(identity: IdentityDb, org_id: uuid.UUID) -> list[tuple[str, str]]:
    async with identity.database.system_session() as session:
        rows = (await session.scalars(select(AuditLog).where(AuditLog.org_id == org_id))).all()
    return sorted((row.action, row.actor_type) for row in rows)


async def ledger(identity: IdentityDb, org_id: uuid.UUID) -> list[CreditLedger]:
    async with identity.database.system_session() as session:
        return list((await session.scalars(select(CreditLedger).where(CreditLedger.org_id == org_id))).all())


async def stored_user(identity: IdentityDb, user_id: uuid.UUID) -> User:
    async with identity.database.system_session() as session:
        user = await session.get(User, user_id)
    assert user is not None
    return user


async def test_first_run_creates_org_owner_credit_and_working_keys(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    result = await seeder(container).seed(email=unique_email("seed"))
    assert result.created is True
    assert (result.org_name, result.plan_id, result.credits_balance_micros) == (
        DEV_ORG_NAME,
        DEV_PLAN_ID,
        DEV_CREDITS_MICROS,
    )
    assert result.revoked_prefixes == []
    async with identity.database.system_session() as session:
        org = await session.get(Org, result.org_id)
        membership = await session.scalar(select(Membership).where(Membership.org_id == result.org_id))
    assert org is not None
    assert (org.slug, org.kyc_level, org.settings) == (result.slug, 0, {})
    assert membership is not None
    assert (membership.user_id, membership.role) == (result.owner_id, "owner")
    user = await stored_user(identity, result.owner_id)
    assert user.email == result.owner_email
    assert user.email_verified_at == container.clock.now()
    assert result.password is not None
    assert container.password_hasher.verify(user.password_hash, result.password)
    [grant] = await ledger(identity, result.org_id)
    assert (grant.entry_type, grant.amount_micros, grant.reference_type, grant.idempotency_key) == (
        "grant",
        DEV_CREDITS_MICROS,
        "manual",
        CREDITS_IDEMPOTENCY_KEY,
    )
    keys = await org_keys(identity, result.org_id)
    assert sorted((key.environment, key.name, key.scopes, key.created_by) for key in keys) == [
        ("live", KEY_NAMES["live"], ["*"], result.owner_id),
        ("test", KEY_NAMES["test"], ["*"], result.owner_id),
    ]
    assert result.keys["live"].key.startswith("va_live_")
    assert result.keys["test"].key.startswith("va_test_")
    for seeded in result.keys.values():
        me = await authenticates(client, seeded.key)
        assert me is not None
        assert me["org"]["id"] == str(result.org_id)  # type: ignore[index]
        assert me["api_key"]["id"] == str(seeded.id)  # type: ignore[index]
    assert await audit_actions(identity, result.org_id) == [("api_key.create", "system")] * 2


async def test_second_run_keeps_org_and_credit_and_rolls_keys(
    identity: IdentityDb, container: SessionContainer, client: httpx.AsyncClient
) -> None:
    email = unique_email("seed")
    first = await seeder(container).seed(email=email)
    for seeded in first.keys.values():
        assert await authenticates(client, seeded.key) is not None
    counts = await row_counts(identity.database)
    second = await seeder(container).seed(email=email.upper())
    assert (second.created, second.password, second.org_id, second.owner_id) == (
        False,
        None,
        first.org_id,
        first.owner_id,
    )
    assert second.credits_balance_micros == DEV_CREDITS_MICROS
    assert await row_counts(identity.database) == counts
    assert len(await ledger(identity, first.org_id)) == 1
    assert sorted(second.revoked_prefixes) == sorted(key.prefix for key in first.keys.values())
    keys = await org_keys(identity, first.org_id)
    active = sorted((key.environment, key.prefix) for key in keys if key.revoked_at is None)
    assert active == sorted((env, key.prefix) for env, key in second.keys.items())
    assert {key.prefix for key in keys if key.revoked_at is not None} == set(second.revoked_prefixes)
    assert await drop_cached_keys(container.redis, second.revoked_prefixes) == []
    for old, new in zip(first.keys.values(), second.keys.values(), strict=True):
        assert await authenticates(client, old.key) is None
        assert await authenticates(client, new.key) is not None
    assert await audit_actions(identity, first.org_id) == [
        *[("api_key.create", "system")] * 4,
        *[("api_key.revoke", "system")] * 2,
    ]


async def test_reset_password_replaces_the_password(
    identity: IdentityDb, container: SessionContainer
) -> None:
    email = unique_email("seed")
    first = await seeder(container).seed(email=email)
    second = await seeder(container).seed(email=email, reset_password=True)
    assert second.created is False
    assert second.password is not None
    assert first.password is not None
    user = await stored_user(identity, first.owner_id)
    assert container.password_hasher.verify(user.password_hash, second.password)
    assert not container.password_hasher.verify(user.password_hash, first.password)


async def test_existing_user_without_dev_org_becomes_its_owner(
    identity: IdentityDb, container: SessionContainer
) -> None:
    other = await identity.provisioner.provision(name="Other org", owner=None)
    member = await add_member(identity, container.password_hasher, other.org.id, "owner")
    result = await seeder(container).seed(email=member.email)
    assert (result.created, result.password, result.owner_id) == (True, None, member.id)
    assert result.org_id != other.org.id
    user = await stored_user(identity, member.id)
    assert user.password_hash == member.password_hash


async def test_dev_org_lost_its_owner_gets_a_new_one(
    identity: IdentityDb, container: SessionContainer
) -> None:
    email = unique_email("seed")
    first = await seeder(container).seed(email=email)
    async with identity.database.system_session() as session:
        await session.execute(delete(Membership).where(Membership.org_id == first.org_id))
    second = await seeder(container).seed(email=email)
    assert second.created is True
    assert second.org_id != first.org_id


async def test_default_owner_email_seeds(identity: IdentityDb, container: SessionContainer) -> None:
    result = await seeder(container).seed()
    assert (result.created, result.owner_email) == (True, DEV_EMAIL)
    user = await stored_user(identity, result.owner_id)
    assert user.email == DEV_EMAIL


async def test_malformed_email_is_invalid_input(container: SessionContainer) -> None:
    with pytest.raises(AppError) as caught:
        await seeder(container).seed(email="not an email")
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "email")


async def test_script_run_seed_end_to_end(migrated_url: str, identity: IdentityDb) -> None:
    """``seed_dev.run_seed`` on its own engines and KMS key; an unreachable Redis is reported, not fatal."""
    settings = make_settings(
        database_url=migrated_url, database_pool_size=1, redis_url="redis://127.0.0.1:1/0", va_env="test"
    )
    email = unique_email("script")
    first, first_failed = await seed_dev.run_seed(settings, email=email, reset_password=False)
    second, second_failed = await seed_dev.run_seed(settings, email=email, reset_password=False)
    assert (first.created, first_failed, second.created, second.org_id) == (True, [], False, first.org_id)
    assert sorted(second_failed) == sorted(second.revoked_prefixes)
    assert len(second_failed) == len(KEY_NAMES)
    hasher = settings.api_key_hasher()
    active = [key for key in await org_keys(identity, first.org_id) if key.revoked_at is None]
    plaintexts = {key.prefix: key.key for key in second.keys.values()}
    assert all(hasher.verify(plaintexts[key.prefix], key.secret_hash, key.pepper_version) for key in active)
    assert len(active) == len(KEY_NAMES)


async def test_drop_cached_keys_deletes_entries_and_reports_redis_failures() -> None:
    redis = fakeredis.FakeAsyncRedis()
    await redis.set(cache_key("va_test_abcd1234"), b"cached")
    assert await drop_cached_keys(redis, ["va_test_abcd1234"]) == []
    assert await redis.exists(cache_key("va_test_abcd1234")) == 0
    server = fakeredis.FakeServer()
    server.connected = False
    broken = fakeredis.FakeAsyncRedis(server=server)
    assert await drop_cached_keys(broken, ["va_test_abcd1234", "va_live_efgh5678"]) == [
        "va_test_abcd1234",
        "va_live_efgh5678",
    ]


def sample_result(password: str | None) -> SeedResult:
    return SeedResult(
        created=password is not None,
        org_id=uuid.UUID(int=1),
        org_name=DEV_ORG_NAME,
        slug="voiceagents-dev-ab12",
        plan_id=DEV_PLAN_ID,
        credits_balance_micros=DEV_CREDITS_MICROS,
        owner_id=uuid.UUID(int=2),
        owner_email=DEV_EMAIL,
        password=password,
        keys={
            "live": SeededKey(id=uuid.UUID(int=3), prefix="va_live_aaaa1111", key="va_live_secretlive"),
            "test": SeededKey(id=uuid.UUID(int=4), prefix="va_test_bbbb2222", key="va_test_secrettest"),
        },
        revoked_prefixes=[],
    )


def test_to_json_shape() -> None:
    assert sample_result("pw-one").to_json() == {
        "created": True,
        "org": {
            "id": str(uuid.UUID(int=1)),
            "name": DEV_ORG_NAME,
            "slug": "voiceagents-dev-ab12",
            "plan_id": DEV_PLAN_ID,
            "credits_balance_micros": DEV_CREDITS_MICROS,
        },
        "owner": {"id": str(uuid.UUID(int=2)), "email": DEV_EMAIL, "password": "pw-one"},
        "api_keys": {
            "live": {"id": str(uuid.UUID(int=3)), "prefix": "va_live_aaaa1111", "key": "va_live_secretlive"},
            "test": {"id": str(uuid.UUID(int=4)), "prefix": "va_test_bbbb2222", "key": "va_test_secrettest"},
        },
    }


def test_render_text_and_repr() -> None:
    created = render_text(sample_result("pw-one"))
    assert "Dev org created: VoiceAgents Dev" in created
    assert "credits:  $50.00" in created
    assert "password: pw-one" in created
    assert "live: va_live_secretlive" in created
    assert 'curl -H "Authorization: va_test_secrettest" localhost:8000/v1/me' in created
    again = render_text(sample_result(None))
    assert "Dev org already seeded" in again
    assert "password: unchanged (use --reset-password)" in again
    shown = repr(sample_result("pw-one"))
    assert "pw-one" not in shown
    assert "secretlive" not in shown
    assert "va_live_aaaa1111" in shown


@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
def test_seed_runs_locally(environment: Environment) -> None:
    check_seed_environment(environment)


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_seed_refuses_shared_environments(environment: Environment) -> None:
    with pytest.raises(SeedRefusedError, match=SEED_REFUSED_MESSAGE):
        check_seed_environment(environment)
