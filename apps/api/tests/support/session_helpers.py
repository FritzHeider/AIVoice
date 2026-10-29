"""Session-auth test support: a migrated Postgres, a container with real sessions, members, a probe router.

Argon2 runs with cheap parameters; the app is served over ``https://`` because the cookies are ``Secure``
outside ``VA_ENV=local`` and httpx never sends ``Secure`` cookies over http.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

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

from va_api.auth import RequestContext, require_scope
from va_api.auth.cookies import CSRF_COOKIE
from va_api.auth.login_throttle import LoginThrottle
from va_api.auth.mfa_guard import MfaGuard
from va_api.auth.sessions import SessionManager
from va_api.auth.verification import ResendLimiter
from va_api.envelopes import envelope_router
from va_common.auth import Argon2Params, PasswordHasher, totp_code, totp_counter
from va_common.crypto import Envelope
from va_common.email import EmailSender, InMemoryEmailSender
from va_db.models import MemberRole, Membership, Org, OrgStatus, User
from va_db.provisioning import OrgProvisioner
from va_db.repositories import NewUser
from va_db.session import Database

__all__ = [
    "API_BASE_URL",
    "CHEAP_ARGON2",
    "DASHBOARD_URL",
    "MFA_DISABLE",
    "MFA_ENABLE",
    "MFA_SETUP",
    "MFA_VERIFY",
    "PASSWORD",
    "SESSION_PROBE_PATH",
    "Member",
    "SessionContainer",
    "enrol_mfa",
    "error_of",
    "link_token",
    "login",
    "make_member",
    "mfa_code",
    "migrated_postgres",
    "session_app",
    "session_client",
    "signup",
    "write_headers",
]

CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
DASHBOARD_URL = "https://app.voiceagents.test"
API_BASE_URL = "https://api.voiceagents.test"
PASSWORD = "plum-orchard-sailboat-42"  # noqa: S105 - test users' password
SESSION_PROBE_PATH = "/v1/knowledge/_session/whoami"
MFA_SETUP = "/internal/auth/mfa/setup"
MFA_ENABLE = "/internal/auth/mfa/enable"
MFA_VERIFY = "/internal/auth/mfa/verify"
MFA_DISABLE = "/internal/auth/mfa/disable"


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
    sessions, login throttle, MFA guard, an in-memory email sender, and the verification resend limiter."""

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

    def __post_init__(self) -> None:
        super().__post_init__()
        self.database = self.identity.database
        self.envelope = self.identity.envelope
        self.provisioner = self.identity.provisioner
        self.sessions = SessionManager(database=self.database, redis=self.redis, clock=self.clock)
        self.login_throttle = LoginThrottle(self.redis)
        self.mfa_guard = MfaGuard(self.redis)
        self.resend_limiter = ResendLimiter(self.redis)

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


_PAGES = {"verify_email": "verify-email", "password_reset": "reset-password"}


def link_token(sender: InMemoryEmailSender, email: str, template: str = "verify_email") -> str:
    """The token of the newest ``template`` link (verification by default) sent to ``email``."""
    messages = [message for message in sender.to(email) if message.template == template]
    assert messages, f"no {template} email to {email}"
    link = re.compile(re.escape(f"{DASHBOARD_URL}/{_PAGES[template]}/") + r"([A-Za-z0-9_-]+)")
    match = link.search(messages[-1].text)
    assert match is not None, messages[-1].text
    return match.group(1)


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
