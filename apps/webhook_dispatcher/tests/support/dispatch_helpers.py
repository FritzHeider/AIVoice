"""Dispatcher rig over a migrated Postgres: real orgs (local KMS), outbox rows, a receiver, fake DNS."""

from __future__ import annotations

import random
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dns_fakes import MappedResolver
from fakeredis import FakeAsyncRedis
from service_helpers import settings_of
from sqlalchemy import delete, select
from tenant_rows import open_database
from webhook_receiver import Receiver, served_receiver

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.email import InMemoryEmailSender
from va_common.ids import new_id
from va_common.metrics import Service, ServiceMetrics, build_metrics, new_registry
from va_common.safe_http import safe_client
from va_common.settings import Environment
from va_common.time import SystemClock
from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.provisioning import OrgProvisioner
from va_db.repositories import MembershipRepo, OrgDataKeyStore
from va_db.repositories.identity import IdentityRepo, NewUser
from va_db.session import Database
from va_domain.org import OrgSettings
from va_events import EventPublisher
from va_webhooks.delivery import Deliverer
from va_webhooks.destinations import DestinationPolicy
from va_webhooks.dispatcher import Dispatcher, DispatchLimits
from va_webhooks.notify import EndpointDisabledNotifier
from va_webhooks.schedule import Timeouts
from va_webhooks.settings import DispatcherSettings
from va_webhooks.signing_secrets import SigningSecrets

__all__ = ["DASHBOARD", "Rig", "StaticOrgSettings", "dispatch_rig", "short_timeouts"]

DASHBOARD = "https://app.voiceagents.test"
LOOPBACK = "127.0.0.1"


def short_timeouts(seconds: float = 0.5) -> Timeouts:
    return Timeouts(batch_s=seconds, default_s=seconds)


@dataclass
class StaticOrgSettings:
    """Org settings source returning one ``OrgSettings`` for every org."""

    settings: OrgSettings = field(default_factory=OrgSettings)

    async def get_lenient(self, org_id: uuid.UUID) -> OrgSettings | None:
        del org_id
        return self.settings


@dataclass
class Org:
    id: uuid.UUID
    secret: str = field(repr=False)


@dataclass
class Rig:
    database: Database
    provisioner: OrgProvisioner
    receiver: Receiver
    port: int
    dns: MappedResolver
    dispatcher: Dispatcher
    metrics: ServiceMetrics
    sender: InMemoryEmailSender
    redis: FakeAsyncRedis
    envelope: Envelope

    def url(self, path: str, host: str = "hooks.test", scheme: str = "http") -> str:
        return f"{scheme}://{host}:{self.port}{path}"

    async def org(self, *, admins: int = 0) -> Org:
        """A provisioned org with an owner plus ``admins`` admins (all with verified-free fresh emails)."""
        provisioned = await self.provisioner.provision(
            name="Hooks Inc", owner=NewUser(email=f"owner-{secrets.token_hex(4)}@hooks.test")
        )
        org_id = provisioned.org.id
        for _ in range(admins):
            async with self.database.tenant_session(org_id) as session:
                user = await IdentityRepo(session).create_user(
                    NewUser(email=f"admin-{secrets.token_hex(4)}@hooks.test")
                )
                await MembershipRepo(session, org_id).add(user.id, "admin")
        return Org(org_id, provisioned.webhook_signing_secret)

    async def endpoint(self, org: Org, url: str, *, enabled: bool = True) -> uuid.UUID:
        async with self.database.system_session() as session:
            endpoint = WebhookEndpoint(org_id=org.id, url=url, enabled=enabled)
            session.add(endpoint)
            await session.flush()
            return endpoint.id

    async def delivery(self, org: Org, url: str, **values: Any) -> uuid.UUID:
        """Insert a due ``pending`` delivery (``values`` override columns)."""
        columns: dict[str, Any] = {
            "event_id": f"evt_{new_id().hex}",
            "event_type": "call.completed",
            "payload": {"call_id": str(new_id()), "status": "completed", "note": "héllo"},
            **values,
        }
        row = WebhookDelivery(org_id=org.id, url=url, **columns)
        async with self.database.system_session() as session:
            session.add(row)
            await session.flush()
            return row.id

    async def row(self, delivery_id: uuid.UUID) -> WebhookDelivery:
        async with self.database.system_session() as session:
            found = await session.get(WebhookDelivery, delivery_id)
            assert found is not None
            return found

    async def endpoint_enabled(self, endpoint_id: uuid.UUID) -> bool:
        async with self.database.system_session() as session:
            return bool(
                await session.scalar(select(WebhookEndpoint.enabled).where(WebhookEndpoint.id == endpoint_id))
            )

    def deliveries(self, result: str, fmt: str = "bland") -> float:
        value = self.metrics.registry.get_sample_value(
            "va_webhook_deliveries_total", {"result": result, "format": fmt}
        )
        return value or 0.0


@asynccontextmanager
async def dispatch_rig(  # noqa: PLR0913 - keyword-only rig knobs
    database_url: str,
    *,
    va_env: Environment = Environment.TEST,
    allow_private: bool = True,
    answers: Mapping[str, list[str]] | None = None,
    limits: DispatchLimits | None = None,
    timeouts: Timeouts | None = None,
    org_settings: StaticOrgSettings | None = None,
    receiver: Receiver | None = None,
    master_key: str | None = None,
) -> AsyncIterator[Rig]:
    """A dispatcher wired like ``build_container`` but over test doubles; the outbox is emptied first.

    Rigs that share orgs (two pods) must share ``master_key``.
    """
    dns = MappedResolver(
        dict(answers)
        if answers is not None
        else {name: [LOOPBACK] for name in ("hooks.test", "a.hooks.test", "b.hooks.test")}
    )
    target = receiver if receiver is not None else Receiver()
    clock = SystemClock()
    async with AsyncExitStack() as stack:
        database = await stack.enter_async_context(open_database(database_url))
        async with database.system_session() as session:
            await session.execute(delete(WebhookDelivery))
        port = await stack.enter_async_context(served_receiver(target))
        envelope = Envelope(
            LocalKmsProvider.from_hex(master_key or secrets.token_hex(32)), OrgDataKeyStore(database)
        )
        redis = FakeAsyncRedis()
        stack.push_async_callback(redis.aclose)
        http = safe_client(
            settings_of(DispatcherSettings), timeout=15.0, allow_private=allow_private, resolver=dns
        )
        stack.push_async_callback(http.aclose)
        metrics = build_metrics(Service.WEBHOOK_DISPATCHER, new_registry(process_collectors=False))
        sender = InMemoryEmailSender()
        dispatcher = Dispatcher(
            database=database,
            deliverer=Deliverer(
                http, clock=clock, timeouts=timeouts if timeouts is not None else short_timeouts(5.0)
            ),
            secrets=SigningSecrets(database, envelope, clock=clock),
            destinations=DestinationPolicy(
                org_settings if org_settings is not None else StaticOrgSettings(),
                va_env=va_env,
                allow_private=allow_private,
                resolver=dns,
            ),
            publisher=EventPublisher(redis, call_events=None),
            notifier=EndpointDisabledNotifier(database, sender, dashboard_base_url=DASHBOARD),
            clock=clock,
            metrics=metrics,
            rng=random.Random(7),  # noqa: S311 - deterministic jitter for tests
            limits=limits,
        )
        yield Rig(
            database=database,
            provisioner=OrgProvisioner(database, envelope),
            receiver=target,
            port=port,
            dns=dns,
            dispatcher=dispatcher,
            metrics=metrics,
            sender=sender,
            redis=redis,
            envelope=envelope,
        )
