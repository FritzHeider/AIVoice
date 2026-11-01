"""Webhook dispatcher settings (specs/02-repo-layout-and-conventions.md §6, scope ``dispatcher``)."""

from __future__ import annotations

from typing import ClassVar

from va_common.settings import DatabaseSettings, RedisSettings, WebhookSafetySettings

__all__ = ["DispatcherSettings"]


class DispatcherSettings(WebhookSafetySettings, RedisSettings, DatabaseSettings):
    """Settings of the outbox delivery service (``WEBHOOK_ALLOW_PRIVATE`` comes from the shared mixin)."""

    service_name: ClassVar[str] = "webhook-dispatcher"
