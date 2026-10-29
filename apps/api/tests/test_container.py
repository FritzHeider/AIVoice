"""The api dependency container and readiness against unreachable services."""

from __future__ import annotations

import pytest
from api_helpers import make_settings
from service_helpers import readiness_of, track_closes

from va_api.auth import ApiKeyAuthenticator
from va_api.auth.login_throttle import LoginThrottle
from va_api.auth.mfa_guard import MfaGuard
from va_api.auth.oidc_discovery import OidcDiscovery
from va_api.auth.sessions import SessionManager
from va_api.auth.verification import ResendLimiter
from va_api.container import Container, build_container, email_sender_for
from va_api.idempotency import DbIdempotencyStore
from va_api.main import create_app
from va_api.rate_limit import RateLimiter
from va_common.auth import SPEC_ARGON2_PARAMS, PasswordHasher
from va_common.crypto import Envelope
from va_common.email import InMemoryEmailSender, SmtpEmailSender
from va_common.time import SystemClock
from va_db.provisioning import OrgProvisioner

UNREACHABLE_PG = "postgresql+asyncpg://va:va@127.0.0.1:1/va"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_TEMPORAL = "127.0.0.1:1"


async def test_build_container_opens_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = track_closes(monkeypatch)
    settings = make_settings(database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, database_pool_size=3)
    async with build_container(settings) as container:
        assert isinstance(container, Container)
        assert container.engine.pool.size() == 3  # type: ignore[attr-defined]
        assert container.temporal.host == settings.temporal_host
        assert container.temporal.namespace == settings.temporal_namespace
        assert set(container.readiness_checks()) == {"postgres", "redis", "temporal"}
        assert container.database.app_engine.pool.size() == 3  # type: ignore[attr-defined]
        assert isinstance(container.authenticator, ApiKeyAuthenticator)
        assert container.authenticator._hasher is container.api_key_hasher
        minted = container.api_key_hasher.hash("va_live_" + "a" * 32)
        assert len(minted) == 64
        assert isinstance(container.rate_limiter, RateLimiter)
        assert isinstance(container.idempotency, DbIdempotencyStore)
        assert isinstance(container.envelope, Envelope)
        assert isinstance(container.provisioner, OrgProvisioner)
        assert isinstance(container.clock, SystemClock)
        assert isinstance(container.password_hasher, PasswordHasher)
        assert container.password_hasher.params == SPEC_ARGON2_PARAMS
        assert isinstance(container.sessions, SessionManager)
        assert isinstance(container.login_throttle, LoginThrottle)
        assert isinstance(container.mfa_guard, MfaGuard)
        assert isinstance(container.email_sender, SmtpEmailSender)
        assert isinstance(container.resend_limiter, ResendLimiter)
        assert isinstance(container.oidc_discovery, OidcDiscovery)
        assert container.oidc_discovery.allow_insecure  # VA_ENV=local
        assert closed == []
    # The OIDC discovery client, the va_app and va_system engines, then the dependency stack in reverse order.
    assert closed == ["http", "engine", "engine", "redis", "engine"]


def test_email_sender_is_in_memory_only_under_test() -> None:
    clock = SystemClock()
    assert isinstance(email_sender_for(make_settings(va_env="test"), clock), InMemoryEmailSender)
    assert isinstance(email_sender_for(make_settings(va_env="local"), clock), SmtpEmailSender)


async def test_build_container_requires_api_key_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = track_closes(monkeypatch)
    with pytest.raises(RuntimeError, match="API_KEY_PEPPER is not configured"):
        async with build_container(make_settings(api_key_pepper="")):
            pytest.fail("the container must not open")
    assert closed == []


async def test_build_container_requires_local_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = track_closes(monkeypatch)
    with pytest.raises(RuntimeError, match="LOCAL_MASTER_KEY is not configured"):
        async with build_container(make_settings(local_master_key="")):
            pytest.fail("the container must not open")
    assert closed == []


async def test_readyz_real_container_reports_unreachable() -> None:
    settings = make_settings(
        database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, temporal_host=UNREACHABLE_TEMPORAL
    )
    status, body = await readiness_of(create_app(settings, init_observability=False, readiness_timeout_s=1.0))
    assert status == 503
    assert body["failing"] == ["postgres", "redis", "temporal"]
    assert set(body["checks"].values()) <= {"error", "timeout"}
