"""Destination policy of webhook deliveries (specs/19-events-and-webhooks.md §8; specs/28 §6.4).

``http`` is allowed in ``local``/``test`` or when the org setting ``allow_insecure_webhooks`` is true;
private targets only with ``WEBHOOK_ALLOW_PRIVATE`` (local/test). The pre-flight check here classifies a
refusal without connecting; the pinning transport (:mod:`va_common.safe_http`) repeats the address check on
the address it dials.
"""

from __future__ import annotations

import uuid
from typing import Final, Protocol

from va_common.net import (
    HostResolver,
    UnsafeDestinationError,
    UnsafeReason,
    check_destination_url,
    system_resolver,
)
from va_common.settings import Environment
from va_domain.org import OrgSettings

__all__ = ["DestinationPolicy", "OrgSettingsSource"]

_HTTP_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})


class OrgSettingsSource(Protocol):
    """Reads an org's settings (``va_db.org_settings.OrgSettingsReader``)."""

    async def get_lenient(self, org_id: uuid.UUID) -> OrgSettings | None: ...


class DestinationPolicy:
    """Decides whether an org's delivery URL may be contacted."""

    def __init__(
        self,
        org_settings: OrgSettingsSource,
        *,
        va_env: Environment,
        allow_private: bool,
        resolver: HostResolver = system_resolver,
    ) -> None:
        self._org_settings = org_settings
        self._va_env = va_env
        self._allow_private = allow_private
        self._resolver = resolver

    async def allow_http(self, org_id: uuid.UUID) -> bool:
        """Whether ``http`` URLs of the org may be delivered to."""
        if self._va_env in _HTTP_ENVIRONMENTS:
            return True
        settings = await self._org_settings.get_lenient(org_id)
        return settings is not None and settings.allow_insecure_webhooks

    async def check(self, org_id: uuid.UUID, url: str) -> UnsafeReason | None:
        """``None`` when ``url`` may be contacted, else the refusing rule."""
        try:
            await check_destination_url(
                url,
                allow_http=await self.allow_http(org_id),
                allow_private=self._allow_private,
                resolver=self._resolver,
            )
        except UnsafeDestinationError as exc:
            return exc.reason
        return None
