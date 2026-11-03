"""Webhook dispatcher settings (specs/02-repo-layout-and-conventions.md §6, scope ``dispatcher``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.email import EmailSettings
from va_common.settings import CryptoSettings, DatabaseSettings, RedisSettings, WebhookSafetySettings

__all__ = ["DispatcherSettings"]


class DispatcherSettings(
    WebhookSafetySettings, CryptoSettings, EmailSettings, RedisSettings, DatabaseSettings
):
    """Settings of the outbox delivery service.

    ``WEBHOOK_ALLOW_PRIVATE`` comes from the shared mixin; the KMS settings decrypt org signing secrets; the
    email settings and ``DASHBOARD_BASE_URL`` serve the endpoint-disabled notice (specs/19 §8).
    """

    service_name: ClassVar[str] = "webhook-dispatcher"

    dashboard_base_url: str = "http://localhost:3000"
