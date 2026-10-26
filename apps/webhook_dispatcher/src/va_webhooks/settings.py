"""Webhook dispatcher settings (specs/02-repo-layout-and-conventions.md §6, scope ``dispatcher``)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import model_validator

from va_common.settings import DatabaseSettings, Environment, RedisSettings

__all__ = ["DispatcherSettings"]

_PRIVATE_FORBIDDEN = frozenset({Environment.STAGING, Environment.PRODUCTION})


class DispatcherSettings(RedisSettings, DatabaseSettings):
    """Settings of the outbox delivery service."""

    service_name: ClassVar[str] = "webhook-dispatcher"

    # specs/02 §6: allowed in local/test, forced false in staging and production.
    webhook_allow_private: bool = True

    @model_validator(mode="after")
    def _forbid_private_outside_dev(self) -> DispatcherSettings:
        if self.va_env in _PRIVATE_FORBIDDEN and self.webhook_allow_private:
            object.__setattr__(self, "webhook_allow_private", False)
        return self
