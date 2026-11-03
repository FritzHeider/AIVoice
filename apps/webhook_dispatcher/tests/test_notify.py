"""Endpoint-disabled notices: failures are logged, never raised (specs/19-events-and-webhooks.md §8)."""

from __future__ import annotations

import uuid

import pytest
from dispatch_helpers import DASHBOARD, dispatch_rig
from service_helpers import UNREACHABLE, json_log_stream, log_events
from tenant_rows import open_database

from va_common.email import EmailDeliveryError, EmailMessage, InMemoryEmailSender
from va_webhooks.notify import EndpointDisabledNotifier

pytestmark = pytest.mark.integration


class RefusingSender(InMemoryEmailSender):
    """Accepts the first message, refuses the rest."""

    async def send(self, message: EmailMessage) -> None:
        if self.sent:
            msg = "relay refused"
            raise EmailDeliveryError(msg)
        await super().send(message)


async def test_a_refused_message_does_not_stop_the_others(dispatcher_db_url: str) -> None:
    stream = json_log_stream()
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org(admins=2)
        sender = RefusingSender()
        notifier = EndpointDisabledNotifier(rig.database, sender, dashboard_base_url=DASHBOARD + "/")
        sent = await notifier.notify(org.id, "https://hooks.example.com/va?token=abc")
    assert sent == 1
    assert len(sender.sent) == 1
    events = log_events(stream)
    failures = [event for event in events if event["event"] == "webhook.endpoint_disable_notify_failed"]
    assert len(failures) == 2
    (summary,) = [event for event in events if event["event"] == "webhook.endpoint_disabled"]
    assert (summary["host"], summary["notified"]) == ("hooks.example.com", 1)
    assert "token=abc" not in stream.getvalue()


async def test_an_unreachable_database_sends_nothing() -> None:
    stream = json_log_stream()
    async with open_database(UNREACHABLE["database_url"]) as database:
        sender = InMemoryEmailSender()
        notifier = EndpointDisabledNotifier(database, sender, dashboard_base_url=DASHBOARD)
        assert await notifier.notify(uuid.uuid4(), "not a url") == 0
    assert sender.sent == []
    assert any(event["event"] == "webhook.endpoint_disable_notify_failed" for event in log_events(stream))
