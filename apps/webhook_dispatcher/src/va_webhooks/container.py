"""Webhook dispatcher dependencies (specs/19-events-and-webhooks.md §8; readiness: Postgres only)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from va_common.checks import Dependencies, open_dependencies
from va_common.crypto import Envelope, open_kms_provider
from va_common.email import EmailSender, InMemoryEmailSender, SmtpEmailSender
from va_common.health import CheckFn
from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.net import HostResolver, system_resolver
from va_common.redis import redis_client
from va_common.safe_http import safe_client
from va_common.settings import Environment
from va_common.time import Clock, SystemClock
from va_db.org_settings import OrgSettingsReader
from va_db.repositories.data_keys import OrgDataKeyStore
from va_db.session import Database
from va_events import EventPublisher
from va_webhooks import __version__
from va_webhooks.delivery import Deliverer
from va_webhooks.destinations import DestinationPolicy
from va_webhooks.dispatcher import Dispatcher, DispatchLimits
from va_webhooks.notify import EndpointDisabledNotifier
from va_webhooks.schedule import DEFAULT_TIMEOUT_S
from va_webhooks.settings import DispatcherSettings
from va_webhooks.signing_secrets import SigningSecrets

__all__ = ["DispatcherContainer", "build_container", "email_sender_for"]

_LIMITS: Final = DispatchLimits()


@dataclass(frozen=True, slots=True)
class DispatcherContainer:
    """The dependency set and the configured :class:`Dispatcher`."""

    dependencies: Dependencies
    database: Database
    dispatcher: Dispatcher

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        """Postgres only (specs/27-observability.md §9)."""
        return self.dependencies.readiness_checks()


def email_sender_for(settings: DispatcherSettings, clock: Clock) -> EmailSender:
    """The in-memory sender when ``VA_ENV=test``, else SMTP through ``SMTP_URL`` (same rule as the api)."""
    if settings.va_env is Environment.TEST:
        return InMemoryEmailSender()
    return SmtpEmailSender(settings.smtp_config(), sender=settings.email_from, clock=clock)


@asynccontextmanager
async def build_container(
    settings: DispatcherSettings,
    *,
    metrics: ServiceMetrics | None = None,
    resolver: HostResolver = system_resolver,
    limits: DispatchLimits = _LIMITS,
) -> AsyncIterator[DispatcherContainer]:
    """Engines, Redis (event sequencing and org settings cache; not a readiness check), the envelope, the
    SSRF-safe HTTP client, and the dispatcher; ``metrics`` is the registry the probes serve.

    ``RuntimeError`` before anything is opened when (local KMS provider) ``LOCAL_MASTER_KEY`` is missing.
    """
    service_metrics = metrics if metrics is not None else build_metrics(Service.WEBHOOK_DISPATCHER)
    clock: Clock = SystemClock()
    async with (
        open_kms_provider(settings) as kms_provider,
        open_dependencies() as deps,
        AsyncExitStack() as stack,
    ):
        deps.postgres(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        redis = redis_client(settings)
        stack.push_async_callback(redis.aclose)
        http = safe_client(
            settings,
            timeout=DEFAULT_TIMEOUT_S,
            allow_private=settings.webhook_allow_private,
            resolver=resolver,
            max_connections=limits.concurrency,
            version=__version__,
        )
        stack.push_async_callback(http.aclose)
        envelope = Envelope(kms_provider, OrgDataKeyStore(database))
        dispatcher = Dispatcher(
            database=database,
            deliverer=Deliverer(http, clock=clock),
            secrets=SigningSecrets(database, envelope, clock=clock),
            destinations=DestinationPolicy(
                OrgSettingsReader(database, redis),
                va_env=settings.va_env,
                allow_private=settings.webhook_allow_private,
                resolver=resolver,
            ),
            publisher=EventPublisher(redis, call_events=None, router=None),
            notifier=EndpointDisabledNotifier(
                database, email_sender_for(settings, clock), dashboard_base_url=settings.dashboard_base_url
            ),
            clock=clock,
            metrics=service_metrics,
            limits=limits,
        )
        yield DispatcherContainer(dependencies=deps, database=database, dispatcher=dispatcher)
