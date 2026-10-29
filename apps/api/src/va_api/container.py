"""Process-wide dependencies of the api, opened in the app lifespan (no network I/O at startup)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from va_api.auth.api_key_auth import ApiKeyAuthenticator, db_lookup, db_touch
from va_api.auth.login_throttle import LoginThrottle
from va_api.auth.mfa_guard import MfaGuard
from va_api.auth.oidc_discovery import DISCOVERY_TIMEOUT_S, OidcDiscovery, discovery_for
from va_api.auth.sessions import SessionManager
from va_api.auth.sso_state import SsoStateStore
from va_api.auth.verification import ResendLimiter
from va_api.idempotency import DbIdempotencyStore, IdempotencyStore
from va_api.rate_limit import RateLimiter
from va_api.settings import ApiSettings
from va_common.auth import ApiKeyHasher, PasswordHasher
from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.crypto import Envelope, open_kms_provider
from va_common.email import EmailSender, InMemoryEmailSender, SmtpEmailSender
from va_common.health import CheckFn
from va_common.service import ReadinessSource
from va_common.settings import Environment
from va_common.time import Clock, SystemClock
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database

__all__ = ["Container", "ContainerFactory", "ReadinessSource", "build_container", "email_sender_for"]

type ContainerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessSource]]


@dataclass(slots=True)
class Container:
    settings: ApiSettings
    engine: AsyncEngine
    redis: Redis
    temporal: TemporalConnector
    dependencies: Dependencies
    database: Database
    authenticator: ApiKeyAuthenticator
    api_key_hasher: ApiKeyHasher
    rate_limiter: RateLimiter
    idempotency: IdempotencyStore
    envelope: Envelope
    provisioner: OrgProvisioner
    clock: Clock
    password_hasher: PasswordHasher
    sessions: SessionManager
    login_throttle: LoginThrottle
    mfa_guard: MfaGuard
    email_sender: EmailSender
    resend_limiter: ResendLimiter
    oidc_discovery: OidcDiscovery
    sso_states: SsoStateStore

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        """Postgres, Redis, Temporal (specs/27-observability.md §9)."""
        return self.dependencies.readiness_checks()


def email_sender_for(settings: ApiSettings, clock: Clock) -> EmailSender:
    """The in-memory sender when ``VA_ENV=test``, else SMTP through ``SMTP_URL`` (connects per message)."""
    if settings.va_env is Environment.TEST:
        return InMemoryEmailSender()
    return SmtpEmailSender(settings.smtp_config(), sender=settings.email_from, clock=clock)


@asynccontextmanager
async def build_container(settings: ApiSettings) -> AsyncIterator[Container]:
    """Create the engines, Redis, Temporal connector, authenticator (its API key hasher is also exposed for
    minting keys), rate limiter, idempotency store, the
    envelope (KMS provider plus ``org_data_keys`` store), the org provisioner, the password hasher, the
    session manager, the login throttle, the MFA guard, the email sender, the verification resend limiter, and
    the OIDC discovery client (its own ``httpx.AsyncClient``, no redirects, 5 s timeout), and the OIDC login
    state store.

    Everything opened is disposed on exit. ``RuntimeError`` when ``API_KEY_PEPPER`` or (for the local KMS
    provider) ``LOCAL_MASTER_KEY`` is not configured, before anything is opened.
    """
    hasher = settings.api_key_hasher()
    # Building the hasher computes its dummy argon2 hash; keep that CPU work off the event loop.
    password_hasher = await asyncio.to_thread(PasswordHasher)
    async with (
        open_kms_provider(settings) as kms_provider,
        open_dependencies() as deps,
        AsyncExitStack() as stack,
    ):
        engine, redis, temporal = deps.postgres(settings), deps.redis(settings), deps.temporal(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        clock: Clock = SystemClock()
        authenticator = ApiKeyAuthenticator(
            redis=redis,
            hasher=hasher,
            lookup=db_lookup(database),
            toucher=db_touch(database),
            clock=clock,
        )
        rate_limiter = RateLimiter(redis, clock)
        idempotency = DbIdempotencyStore(database, clock)
        envelope = Envelope(kms_provider, OrgDataKeyStore(database))
        oidc_http = httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_S, follow_redirects=False)
        stack.push_async_callback(oidc_http.aclose)
        yield Container(
            settings=settings,
            engine=engine,
            redis=redis,
            temporal=temporal,
            dependencies=deps,
            database=database,
            authenticator=authenticator,
            api_key_hasher=hasher,
            rate_limiter=rate_limiter,
            idempotency=idempotency,
            envelope=envelope,
            provisioner=OrgProvisioner(database, envelope),
            clock=clock,
            password_hasher=password_hasher,
            sessions=SessionManager(database=database, redis=redis, clock=clock),
            login_throttle=LoginThrottle(redis),
            mfa_guard=MfaGuard(redis),
            email_sender=email_sender_for(settings, clock),
            resend_limiter=ResendLimiter(redis),
            oidc_discovery=discovery_for(settings, oidc_http),
            sso_states=SsoStateStore(redis),
        )
