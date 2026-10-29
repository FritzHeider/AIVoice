"""Development seed: one dev org with an owner, starting credit, and a live and a test API key (specs/02
layout ``scripts/seed_dev.py``; the ``> Decision (2026-10-29)`` block "Dev seed (P02-T14)" there).

Re-running is safe: the org, owner, and credit grant are created once; the two ``dev-seed`` keys are rolled
on every run (the old ones revoked, new ones minted) because raw keys are never stored, so rolling is the only
way a re-run can print keys that work. ``scripts/seed_dev.py`` is the command-line wrapper; the logic lives
here so it is typed and tested with the api package.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.api_keys import mint_api_key
from va_api.auth.scopes import ALL_SCOPES
from va_api.routers.api_keys_schemas import api_key_view
from va_common.auth import ApiKeyHasher, KeyEnvironment, PasswordHasher
from va_common.errors import AppError
from va_common.money import micros_to_dollars
from va_common.settings import Environment
from va_common.time import Clock
from va_db.audit import AuditActor, record
from va_db.models import Org
from va_db.provisioning import OrgProvisioner, ProvisionedOrg
from va_db.repositories import ApiKeyRepo, IdentityRepo, MembershipLookupRepo, NewUser, grant_credits
from va_db.session import Database

__all__ = [
    "CREDITS_IDEMPOTENCY_KEY",
    "DEV_CREDITS_MICROS",
    "DEV_EMAIL",
    "DEV_ORG_NAME",
    "DEV_OWNER_NAME",
    "DEV_PLAN_ID",
    "KEY_NAMES",
    "SEED_ENVIRONMENTS",
    "SEED_REFUSED_MESSAGE",
    "DevSeeder",
    "SeedRefusedError",
    "SeedResult",
    "SeededKey",
    "check_seed_environment",
    "render_text",
]

DEV_EMAIL: Final = "dev@voiceagents.test"
DEV_ORG_NAME: Final = "VoiceAgents Dev"
DEV_OWNER_NAME: Final = "Dev Owner"
DEV_PLAN_ID: Final = "build"
DEV_CREDITS_MICROS: Final = 50_000_000
CREDITS_IDEMPOTENCY_KEY: Final = "seed:dev-credits"
CREDITS_DESCRIPTION: Final = "Dev seed credits"
KEY_NAMES: Final[Mapping[KeyEnvironment, str]] = {"live": "dev-seed live", "test": "dev-seed test"}
SEED_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})
SEED_REFUSED_MESSAGE: Final = "seed_dev.py only runs with VA_ENV=local or test."
PASSWORD_BYTES: Final = 18
API_BASE_URL: Final = "localhost:8000"


class SeedRefusedError(Exception):
    """The seed was asked to run outside ``VA_ENV=local``/``test`` (it prints secrets)."""


def check_seed_environment(environment: Environment) -> None:
    """:class:`SeedRefusedError` unless ``environment`` is ``local`` or ``test``."""
    if environment not in SEED_ENVIRONMENTS:
        raise SeedRefusedError(SEED_REFUSED_MESSAGE)


@dataclass(frozen=True, slots=True)
class SeededKey:
    """A key minted by this run; the plaintext never appears in ``repr``."""

    id: uuid.UUID
    prefix: str
    key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What the seed created or found; ``password`` is set only when this run created or reset it."""

    created: bool
    org_id: uuid.UUID
    org_name: str
    slug: str
    plan_id: str
    credits_balance_micros: int
    owner_id: uuid.UUID
    owner_email: str
    password: str | None = field(repr=False)
    keys: Mapping[KeyEnvironment, SeededKey]
    revoked_prefixes: list[str]

    def to_json(self) -> dict[str, Any]:
        """The ``--json`` output object."""
        return {
            "created": self.created,
            "org": {
                "id": str(self.org_id),
                "name": self.org_name,
                "slug": self.slug,
                "plan_id": self.plan_id,
                "credits_balance_micros": self.credits_balance_micros,
            },
            "owner": {"id": str(self.owner_id), "email": self.owner_email, "password": self.password},
            "api_keys": {
                environment: {"id": str(key.id), "prefix": key.prefix, "key": key.key}
                for environment, key in self.keys.items()
            },
        }


def render_text(result: SeedResult) -> str:
    """The human-readable output: org, owner login, both keys, and a ``curl`` line to try the test key."""
    password = result.password if result.password is not None else "unchanged (use --reset-password)"
    lines = [
        f"Dev org {'created' if result.created else 'already seeded'}: {result.org_name}",
        f"  id:       {result.org_id}",
        f"  slug:     {result.slug}",
        f"  plan:     {result.plan_id}",
        f"  credits:  ${micros_to_dollars(result.credits_balance_micros):.2f}",
        "Dashboard login:",
        f"  email:    {result.owner_email}",
        f"  password: {password}",
        "API keys (previous dev-seed keys were revoked):",
        *(f"  {environment}: {key.key}" for environment, key in result.keys.items()),
        "Try it:",
        f'  curl -H "Authorization: {result.keys["test"].key}" {API_BASE_URL}/v1/me',
    ]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Owner:
    user_id: uuid.UUID
    email: str
    org_id: uuid.UUID
    created: bool
    password: str | None


class DevSeeder:
    """Creates or refreshes the dev org; never nests database sessions (one connection per role)."""

    def __init__(
        self,
        *,
        database: Database,
        provisioner: OrgProvisioner,
        api_key_hasher: ApiKeyHasher,
        password_hasher: PasswordHasher,
        clock: Clock,
    ) -> None:
        self._database = database
        self._provisioner = provisioner
        self._api_key_hasher = api_key_hasher
        self._password_hasher = password_hasher
        self._clock = clock

    async def seed(self, *, email: str = DEV_EMAIL, reset_password: bool = False) -> SeedResult:
        """Seed the dev org of ``email``; ``INVALID_INPUT`` (``field: "email"``) for a malformed address."""
        owner = await self._owner_and_org(_normalized(email), reset_password=reset_password)
        actor = AuditActor(owner.org_id, "system", None)
        async with self._database.tenant_session(owner.org_id) as session:
            await grant_credits(
                session,
                owner.org_id,
                DEV_CREDITS_MICROS,
                idempotency_key=CREDITS_IDEMPOTENCY_KEY,
                description=CREDITS_DESCRIPTION,
            )
            revoked = await self._revoke_seed_keys(session, actor)
            keys = {
                environment: await self._mint(session, actor, environment, owner.user_id)
                for environment in KEY_NAMES
            }
            org = await session.get(Org, owner.org_id)
            if org is None:
                raise AppError("NOT_FOUND", "Organization not found.")
            return SeedResult(
                created=owner.created,
                org_id=org.id,
                org_name=org.name,
                slug=org.slug,
                plan_id=org.plan_id,
                credits_balance_micros=org.credits_balance_micros,
                owner_id=owner.user_id,
                owner_email=owner.email,
                password=owner.password,
                keys=keys,
                revoked_prefixes=revoked,
            )

    async def _owner_and_org(self, email: str, *, reset_password: bool) -> _Owner:
        """Find or create the owner and the dev org; the password is set only on creation or reset."""
        async with self._database.global_session() as session:
            user = await IdentityRepo(session).get_user_by_email(email)
        if user is None:
            password = _new_password()
            new_user = NewUser(
                email=email,
                name=DEV_OWNER_NAME,
                password_hash=await self._hash(password),
                email_verified_at=self._clock.now(),
            )
            provisioned = await self._provision(new_user)
            if provisioned.owner is None:
                raise AppError("NOT_FOUND", "Owner not found.")
            return _Owner(provisioned.owner.id, provisioned.owner.email, provisioned.org.id, True, password)
        reset = await self._reset_password(user.id) if reset_password else None
        org_id = await self._dev_org_of(user.id)
        created = org_id is None
        if org_id is None:
            org_id = (await self._provision(user.id)).org.id
        return _Owner(user.id, user.email, org_id, created, reset)

    async def _provision(self, owner: uuid.UUID | NewUser) -> ProvisionedOrg:
        return await self._provisioner.provision(
            name=DEV_ORG_NAME, owner=owner, plan_id=DEV_PLAN_ID, kyc_level=0
        )

    async def _dev_org_of(self, user_id: uuid.UUID) -> uuid.UUID | None:
        """The org named :data:`DEV_ORG_NAME` the user owns (the oldest one), or ``None``."""
        async with self._database.system_session() as session:
            memberships = await MembershipLookupRepo(session).for_user(user_id)
        for membership in memberships:
            if membership.role == "owner" and membership.org_name == DEV_ORG_NAME:
                return membership.org_id
        return None

    async def _reset_password(self, user_id: uuid.UUID) -> str:
        password = _new_password()
        password_hash = await self._hash(password)
        async with self._database.global_session() as session:
            await IdentityRepo(session).set_password_hash(user_id, password_hash)
        return password

    async def _hash(self, password: str) -> str:
        # argon2id is CPU-bound; keep it off the event loop.
        return await asyncio.to_thread(self._password_hasher.hash, password)

    async def _revoke_seed_keys(self, session: AsyncSession, actor: AuditActor) -> list[str]:
        """Revoke the org's active ``dev-seed`` keys (one ``api_key.revoke`` row each); their prefixes."""
        repo = ApiKeyRepo(session, actor.org_id)
        now = self._clock.now()
        names = set(KEY_NAMES.values())
        revoked: list[str] = []
        for key in await repo.list_keys():
            if key.name not in names or key.revoked_at is not None:
                continue
            row = await repo.revoke(key.id, now)
            if row is None:
                continue
            await record(
                session,
                actor,
                "api_key.revoke",
                "api_key",
                key.id,
                before={"revoked_at": None},
                after={"revoked_at": row.revoked_at},
            )
            revoked.append(row.prefix)
        return revoked

    async def _mint(
        self, session: AsyncSession, actor: AuditActor, environment: KeyEnvironment, owner_id: uuid.UUID
    ) -> SeededKey:
        """A new all-scope ``dev-seed`` key for ``environment`` with its ``api_key.create`` audit row."""
        minted = mint_api_key(self._api_key_hasher, environment)
        row = await ApiKeyRepo(session, actor.org_id).add(
            name=KEY_NAMES[environment],
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=[ALL_SCOPES],
            environment=environment,
            created_by=owner_id,
        )
        await record(
            session, actor, "api_key.create", "api_key", row.id, after=api_key_view(row).model_dump()
        )
        return SeededKey(id=row.id, prefix=row.prefix, key=minted.plaintext)


def _normalized(email: str) -> str:
    try:
        return validate_email(email, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError as exc:
        raise AppError("INVALID_INPUT", str(exc), field="email") from exc


def _new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)
