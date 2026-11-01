"""Telnyx provisioning response models (specs/06-telephony-telnyx.md §2.1 provisioning rows, §12).

Request bodies are built by :mod:`va_telnyx.provisioning` (fixed field lists of the §2.1 rows); the models
here read only the fields the platform uses and ignore the rest.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from va_telnyx.models import ResponseModel

__all__ = [
    "AppOutboundSettings",
    "Balance",
    "CallControlApp",
    "MessagingProfile",
    "OutboundVoiceProfile",
]


class AppOutboundSettings(ResponseModel):
    """``outbound`` of a Call Control Application."""

    outbound_voice_profile_id: str | None = None


class CallControlApp(ResponseModel):
    """A Call Control Application (its ``id`` is the ``connection_id`` of calls and numbers)."""

    id: str
    application_name: str
    webhook_event_url: str
    outbound: AppOutboundSettings = Field(default_factory=AppOutboundSettings)

    @property
    def outbound_voice_profile_id(self) -> str | None:
        return self.outbound.outbound_voice_profile_id


class OutboundVoiceProfile(ResponseModel):
    id: str
    name: str
    whitelisted_destinations: tuple[str, ...] = ()


class MessagingProfile(ResponseModel):
    id: str
    name: str
    webhook_url: str | None = None
    whitelisted_destinations: tuple[str, ...] = ()


class Balance(ResponseModel):
    """``GET /balance`` (decimal strings in ``currency``)."""

    balance: Decimal
    credit_limit: Decimal
    available_credit: Decimal
    currency: str
