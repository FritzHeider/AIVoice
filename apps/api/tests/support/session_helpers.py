"""Session-auth test support: a migrated Postgres, a container with real sessions, members, a probe router.

Argon2 runs with cheap parameters; the app is served over ``https://`` because the cookies are ``Secure``
outside ``VA_ENV=local`` and httpx never sends ``Secure`` cookies over http.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
import pytest
from api_helpers import make_settings
from auth_helpers import AuthContainer
from docker_probe import docker_available
from fastapi import Depends, FastAPI, Request
from identity_db import IdentityDb, unique_email
from migration_helpers import fresh_database, upgrade
from pg_container import running_postgres
from service_helpers import make_client
from sqlalchemy import update

from va_api.auth import ApiKeyAuthenticator, RequestContext, require_scope
from va_api.auth.api_key_auth import db_lookup
from va_api.auth.cookies import CSRF_COOKIE
from va_api.auth.login_throttle import LoginThrottle
from va_api.auth.mfa_guard import MfaGuard
from va_api.auth.oidc_discovery import OidcDiscovery, discovery_for
from va_api.auth.sessions import SessionManager
from va_api.auth.sso_state import SsoStateStore
from va_api.auth.verification import ResendLimiter
from va_api.envelopes import envelope_router
from va_api.settings import ApiSettings
from va_common.auth import Argon2Params, PasswordHasher, api_key_prefix, totp_code, totp_counter
from va_common.crypto import Envelope
from va_common.email import EmailSender, InMemoryEmailSender
from va_common.ids import new_id
from va_common.money import dollars_to_micros
from va_db.models import (
    ApiKey,
    CreditHold,
    CreditHoldStatus,
    CreditLedger,
    MemberRole,
    Membership,
    Org,
    OrgStatus,
    User,
)
from va_db.provisioning import OrgProvisioner
from va_db.repositories import ApiKeyRecord, IdentityRepo, MembershipRepo, NewUser
from va_db.session import Database

__all__ = [
    "ACCEPT_INVITATION",
    "API_BASE_URL",
    "CHEAP_ARGON2",
    "DASHBOARD_URL",
    "INVITATIONS",
    "MFA_DISABLE",
    "MFA_ENABLE",
    "MFA_SETUP",
    "MFA_VERIFY",
    "PASSWORD",
    "SESSION_PROBE_PATH",
    "Member",
    "SessionContainer",
    "add_member",
    "enrol_mfa",
    "error_of",
    "fund",
    "hold",
    "invite",
    "link_token",
    "login",
    "make_member",
    "mfa_code",
    "migrated_postgres",
    "session_app",
    "session_client",
    "set_org",
    "signed_in",
    "signup",
    "store_api_key",
    "stored_org",
    "write_headers",
]

CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
DASHBOARD_URL = "https://app.voiceagents.test"
API_BASE_URL = "https://api.voiceagents.test"
PASSWORD = "plum-orchard-sailboat-42"  # noqa: S105 - test users' password
SESSION_PROBE_PATH = "/v1/knowledge/_session/whoami"
INVITATIONS = "/v1/org/members/invitations"
ACCEPT_INVITATION = "/internal/auth/invitations/accept"
MFA_SETUP = "/internal/auth/mfa/setup"
MFA_ENABLE = "/internal/auth/mfa/enable"
MFA_VERIFY = "/internal/auth/mfa/verify"
MFA_DISABLE = "/internal/auth/mfa/disable"
VERIFIED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@contextmanager
def migrated_postgres() -> Iterator[str]:
    """A fresh database migrated to head on a test Postgres container (skips without Docker)."""
    if not docker_available():
        pytest.skip("docker unavailable")
    with running_postgres() as server_url, fresh_database(server_url) as url:
        upgrade(url)
        yield url


@dataclass
class SessionContainer(AuthContainer):
    """The fake api container plus a real database, envelope, and provisioner, cheap password hasher,
    sessions, login throttle, MFA guard, an in-memory email sender, the verification resend limiter, the OIDC
    login state store, and the OIDC discovery client built from the app settings when the app opens it
    (intercept it with respx)."""

    identity: IdentityDb = field(kw_only=True)
    password_hasher: PasswordHasher = field(default_factory=lambda: PasswordHasher(CHEAP_ARGON2))
    email_sender: EmailSender = field(default_factory=InMemoryEmailSender)
    database: Database = field(init=False)
    envelope: Envelope = field(init=False)
    provisioner: OrgProvisioner = field(init=False)
    sessions: SessionManager = field(init=False)
    login_throttle: LoginThrottle = field(init=False)
    mfa_guard: MfaGuard = field(init=False)
    resend_limiter: ResendLimiter = field(init=False)
    oidc_discovery: OidcDiscovery = field(init=False)
    sso_states: SsoStateStore = field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.database = self.identity.database
        self.envelope = self.identity.envelope
        self.provisioner = self.identity.provisioner
        self.sessions = SessionManager(database=self.database, redis=self.redis, clock=self.clock)
        self.login_throttle = LoginThrottle(self.redis)
        self.mfa_guard = MfaGuard(self.redis)
        self.resend_limiter = ResendLimiter(self.redis)
        self.sso_states = SsoStateStore(self.redis)

    @asynccontextmanager
    async def __call__(self, settings: object) -> AsyncIterator[Self]:
        assert isinstance(settings, ApiSettings)
        async with super().__call__(settings) as opened, httpx.AsyncClient() as http:
            self.oidc_discovery = discovery_for(settings, http)
            yield opened

    @property
    def authenticator(self) -> ApiKeyAuthenticator:
        """Keys of the in-memory store first, then keys minted into ``api_keys`` (system-session lookup)."""
        from_db = db_lookup(self.database)

        async def lookup(prefix: str) -> ApiKeyRecord | None:
            return await self.store.lookup(prefix) or await from_db(prefix)

        return ApiKeyAuthenticator(
            redis=self.redis,
            hasher=self.store.hasher,
            lookup=lookup,
            toucher=self.store.touch,
            clock=self.clock,
        )

    @property
    def outbox(self) -> InMemoryEmailSender:
        """The in-memory sender (``TypeError`` when a test replaced it with another sender)."""
        if not isinstance(self.email_sender, InMemoryEmailSender):
            msg = "email_sender is not an InMemoryEmailSender"
            raise TypeError(msg)
        return self.email_sender


_probe = envelope_router(prefix="/v1/knowledge/_session", tags=["Knowledge"])


def _echo(ctx: RequestContext) -> dict[str, Any]:
    return {
        "org_id": str(ctx.org_id),
        "principal_type": ctx.principal_type,
        "principal_id": str(ctx.principal_id),
        "role": ctx.role,
        "scopes": sorted(ctx.scopes),
        "environment": ctx.environment,
        "plan_id": ctx.plan_id,
    }


@_probe.get(
    "/whoami", operation_id="session_probe_read", dependencies=[Depends(require_scope("knowledge:read"))]
)
async def probe_read(request: Request) -> dict[str, Any]:
    return _echo(request.state.ctx)


@_probe.post(
    "/whoami", operation_id="session_probe_write", dependencies=[Depends(require_scope("knowledge:write"))]
)
async def probe_write(request: Request) -> dict[str, Any]:
    return _echo(request.state.ctx)


def session_app(container: SessionContainer, **settings_overrides: Any) -> FastAPI:
    """The production app plus the probe router; ``VA_ENV=test`` (Secure cookies) unless overridden."""
    from va_api.main import ROUTERS, create_app  # noqa: PLC0415 - mirrors auth_helpers.auth_app

    overrides = {"va_env": "test", "dashboard_base_url": DASHBOARD_URL, **settings_overrides}
    return create_app(
        make_settings(**overrides),
        container_factory=container,
        init_observability=False,
        routers=(*ROUTERS, _probe),
    )


@asynccontextmanager
async def session_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(app, base_url=API_BASE_URL) as client:
        yield client


@dataclass(frozen=True, slots=True)
class Member:
    user: User
    org: Org


async def make_member(  # noqa: PLR0913 - keyword-only member attributes
    identity: IdentityDb,
    hasher: PasswordHasher,
    *,
    role: MemberRole = "owner",
    org_settings: dict[str, Any] | None = None,
    org_status: OrgStatus = "active",
    password: str = PASSWORD,
) -> Member:
    """A verified user with ``password`` and a membership with ``role`` in a freshly provisioned org."""
    provisioned = await identity.provisioner.provision(
        name="Acme Dental",
        owner=NewUser(email=unique_email("jane"), name="Jane Park", password_hash=hasher.hash(password)),
    )
    assert provisioned.owner is not None
    org = provisioned.org
    async with identity.database.system_session() as session:
        await session.execute(
            update(Org).where(Org.id == org.id).values(settings=org_settings or {}, status=org_status)
        )
        await session.execute(
            update(Membership)
            .where(Membership.org_id == org.id, Membership.user_id == provisioned.owner.id)
            .values(role=role)
        )
    return Member(user=provisioned.owner, org=org)


async def add_member(
    identity: IdentityDb,
    hasher: PasswordHasher,
    org_id: uuid.UUID,
    role: MemberRole,
    *,
    name: str = "Omar Haddad",
) -> User:
    """A new verified user with :data:`PASSWORD` and a membership with ``role`` in ``org_id``."""
    new = NewUser(
        email=unique_email("member"),
        name=name,
        password_hash=hasher.hash(PASSWORD),
        email_verified_at=VERIFIED_AT,
    )
    async with identity.database.tenant_session(org_id) as session:
        user = await IdentityRepo(session).create_user(new)
        await MembershipRepo(session, org_id).add(user.id, role)
    return user


async def store_api_key(
    container: SessionContainer, org_id: uuid.UUID, scopes: tuple[str, ...] = ("*",)
) -> tuple[str, ApiKey]:
    """A key the in-memory store authenticates for ``org_id``, also stored as an ``api_keys`` row."""
    plaintext, record = container.store.issue(org_id=org_id, scopes=scopes)
    row = ApiKey(
        id=record.id,
        org_id=org_id,
        name="server",
        prefix=api_key_prefix(plaintext),
        secret_hash=record.secret_hash,
        scopes=list(scopes),
        environment=record.environment,
    )
    async with container.database.system_session() as session:
        session.add(row)
    return plaintext, row


async def fund(container: SessionContainer, org_id: uuid.UUID, dollars: str) -> None:
    """Grant ``dollars`` of credit to ``org_id``: a ``grant`` ledger row and the matching balance."""
    micros = dollars_to_micros(dollars)
    async with container.database.system_session() as session:
        org = await session.get(Org, org_id, with_for_update=True)
        assert org is not None
        org.credits_balance_micros += micros
        session.add(
            CreditLedger(
                id=new_id(),
                org_id=org_id,
                entry_type="grant",
                amount_micros=micros,
                balance_after_micros=org.credits_balance_micros,
                reference_type="manual",
                description="test grant",
                idempotency_key=f"grant:{new_id()}",
            )
        )


async def hold(
    container: SessionContainer, org_id: uuid.UUID, dollars: str, status: CreditHoldStatus = "held"
) -> None:
    """A ``credit_holds`` row of ``dollars`` with ``status`` for ``org_id``."""
    async with container.database.system_session() as session:
        session.add(
            CreditHold(
                id=new_id(),
                org_id=org_id,
                reference_type="call",
                reference_id=str(new_id()),
                amount_micros=dollars_to_micros(dollars),
                status=status,
                expires_at=container.clock.now() + timedelta(hours=1),
            )
        )


async def set_org(identity: IdentityDb, org_id: uuid.UUID, **values: Any) -> None:
    """Update columns of the ``orgs`` row ``org_id`` directly (test setup)."""
    async with identity.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == org_id).values(**values))


async def stored_org(identity: IdentityDb, org_id: uuid.UUID) -> Org:
    """The ``orgs`` row ``org_id`` as stored."""
    async with identity.database.system_session() as session:
        org = await session.get(Org, org_id)
    assert org is not None
    return org


def error_of(response: httpx.Response) -> tuple[int, str, str | None]:
    """The status, error code, and field of the response's first envelope error."""
    item = response.json()["errors"][0]
    return response.status_code, item["error"], item.get("field")


def write_headers(client: httpx.AsyncClient, *, origin: str | None = DASHBOARD_URL) -> dict[str, str]:
    """``X-CSRF-Token`` from the client's ``va_csrf`` cookie plus the dashboard ``Origin``."""
    headers = {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE) or ""}
    if origin is not None:
        headers["Origin"] = origin
    return headers


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    """``POST /internal/auth/login`` from the dashboard origin; asserts success."""
    response = await client.post(
        "/internal/auth/login", json={"email": email, "password": password}, headers={"Origin": DASHBOARD_URL}
    )
    assert response.status_code == 200, response.text
    return response


async def signed_in(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole = "owner"
) -> Member:
    """A :func:`make_member` with ``role``, signed in on ``client``."""
    member = await make_member(identity, container.password_hasher, role=role)
    await login(client, member.user.email)
    return member


async def signup(  # noqa: PLR0913 - keyword-only signup fields
    client: httpx.AsyncClient,
    *,
    email: str,
    password: str = PASSWORD,
    name: str = "Jane Park",
    org_name: str = "Acme Dental",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """``POST /internal/auth/signup`` from the dashboard origin (unless ``headers`` replace it)."""
    body = {"email": email, "password": password, "name": name, "org_name": org_name}
    return await client.post(
        "/internal/auth/signup",
        json=body,
        headers=headers if headers is not None else {"Origin": DASHBOARD_URL},
    )


_PAGES = {"verify_email": "verify-email", "password_reset": "reset-password", "invitation": "invite"}


def link_token(sender: InMemoryEmailSender, email: str, template: str = "verify_email") -> str:
    """The token of the newest ``template`` link (verification by default) sent to ``email``."""
    messages = [message for message in sender.to(email) if message.template == template]
    assert messages, f"no {template} email to {email}"
    link = re.compile(re.escape(f"{DASHBOARD_URL}/{_PAGES[template]}/") + r"([A-Za-z0-9_-]+)")
    match = link.search(messages[-1].text)
    assert match is not None, messages[-1].text
    return match.group(1)


async def invite(
    client: httpx.AsyncClient, container: SessionContainer, email: str, role: MemberRole = "developer"
) -> str:
    """The signed-in client invites ``email`` with ``role``; the token of the emailed link."""
    response = await client.post(
        INVITATIONS, json={"email": email, "role": role}, headers=write_headers(client)
    )
    assert response.status_code == 201, response.text
    return link_token(container.outbox, email, "invitation")


def mfa_code(container: SessionContainer, secret: str) -> str:
    """The TOTP code of ``secret`` at the container clock's current time step."""
    return totp_code(secret, totp_counter(container.clock.now()))


async def enrol_mfa(client: httpx.AsyncClient, container: SessionContainer) -> tuple[str, list[str]]:
    """Set up and enable MFA for the client's signed-in user; the secret and the recovery codes.

    The clock then moves to the next time step, so the enabling code cannot collide with the caller's next.
    """
    setup = await client.post(MFA_SETUP, headers=write_headers(client))
    assert setup.status_code == 200, setup.text
    secret: str = setup.json()["data"]["secret_base32"]
    enable = await client.post(
        MFA_ENABLE, json={"code": mfa_code(container, secret)}, headers=write_headers(client)
    )
    assert enable.status_code == 200, enable.text
    container.clock.advance(30)
    return secret, enable.json()["data"]["recovery_codes"]
