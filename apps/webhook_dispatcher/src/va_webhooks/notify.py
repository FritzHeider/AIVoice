"""Email the org's owners and admins when a ``410 Gone`` disabled an endpoint (specs/19 §8)."""

from __future__ import annotations

import uuid
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError

from va_common.email import EmailDeliveryError, EmailSender, webhook_endpoint_disabled
from va_common.logging import get_logger
from va_db.repositories import MembershipLookupRepo
from va_db.session import Database

__all__ = ["SETTINGS_PATH", "EndpointDisabledNotifier"]

SETTINGS_PATH: Final = "/settings/webhooks"

_log = get_logger(__name__)


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


class EndpointDisabledNotifier:
    """Sends ``webhook_endpoint_disabled`` to every active owner/admin; failures are logged, never raised."""

    def __init__(self, database: Database, sender: EmailSender, *, dashboard_base_url: str) -> None:
        self._database = database
        self._sender = sender
        self._link = dashboard_base_url.rstrip("/") + SETTINGS_PATH

    async def notify(self, org_id: uuid.UUID, url: str) -> int:
        """Email the admins of ``org_id`` about ``url``; returns how many messages were accepted."""
        try:
            async with self._database.system_session() as session:
                recipients = await MembershipLookupRepo(session).admin_emails(org_id)
        except (SQLAlchemyError, OSError) as exc:
            _log.warning(
                "webhook.endpoint_disable_notify_failed", org_id=str(org_id), error=type(exc).__name__
            )
            return 0
        sent = 0
        for recipient in recipients:
            try:
                await self._sender.send(webhook_endpoint_disabled(url=url, link=self._link, to=recipient))
            except (EmailDeliveryError, OSError) as exc:
                _log.warning(
                    "webhook.endpoint_disable_notify_failed", org_id=str(org_id), error=type(exc).__name__
                )
                continue
            sent += 1
        _log.info("webhook.endpoint_disabled", org_id=str(org_id), host=_host(url), notified=sent)
        return sent
