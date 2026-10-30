"""FakeTelnyx request models (specs/06-telephony-telnyx.md §2.1 body fields; specs/30-testing-and-evals.md
§3.2).

Every model forbids unknown fields, so a client typo fails a contract test with ``10015`` instead of being
silently ignored. Phone numbers are E.164 (defined here: the simulator does not import ``va_domain``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

__all__ = [
    "E164",
    "AccountCreate",
    "AppInbound",
    "AppOutbound",
    "CallControlAppCreate",
    "InboundMessage",
    "MessageCreate",
    "MessagingProfileCreate",
    "NumberOrderCreate",
    "OrderedNumber",
    "OutboundVoiceProfileCreate",
    "PhoneNumberMessagingUpdate",
    "PhoneNumberUpdate",
    "TelnyxConfigPatch",
    "TelnyxSimConfig",
]

type E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{6,14}$")]
type CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
type WebhookUrl = Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
type Name = Annotated[str, StringConstraints(min_length=1, max_length=255)]
type AnchorSite = Literal[
    "Latency",
    "Chicago, IL",
    "Ashburn, VA",
    "San Jose, CA",
    "Amsterdam, Netherlands",
    "London, UK",
    "Toronto, Canada",
    "Sydney, Australia",
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppInbound(_Body):
    channel_limit: PositiveInt | None = None
    sip_subdomain: str | None = None
    sip_subdomain_receive_settings: Literal["only_my_connections", "from_anyone"] = "only_my_connections"
    shaken_stir_enabled: bool = False


class AppOutbound(_Body):
    outbound_voice_profile_id: str | None = None
    channel_limit: PositiveInt | None = None


class CallControlAppCreate(_Body):
    """``POST /v2/call_control_applications``; omitted fields take the values of specs/06 §2.1."""

    application_name: Name
    webhook_event_url: WebhookUrl
    webhook_event_failover_url: WebhookUrl | Literal[""] = ""
    webhook_api_version: Literal["1", "2"] = "2"
    webhook_timeout_secs: Annotated[int, Field(ge=0, le=30)] | None = 25
    first_command_timeout: bool = True
    first_command_timeout_secs: Annotated[int, Field(ge=0, le=300)] = 30
    anchorsite_override: AnchorSite = "Latency"
    dtmf_type: Literal["RFC 2833", "Inband", "SIP INFO"] = "RFC 2833"
    active: bool = True
    inbound: AppInbound = Field(default_factory=AppInbound)
    outbound: AppOutbound = Field(default_factory=AppOutbound)


class OutboundVoiceProfileCreate(_Body):
    name: Name
    traffic_type: Literal["conversational", "short_duration"] = "conversational"
    service_plan: Literal["global", "us", "international"] = "global"
    concurrent_call_limit: PositiveInt | None = None
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    whitelisted_destinations: list[CountryCode] = Field(default_factory=lambda: ["US", "CA"])


class MessagingProfileCreate(_Body):
    name: Name
    enabled: bool = True
    webhook_url: WebhookUrl | None = None
    webhook_failover_url: WebhookUrl | None = None
    webhook_api_version: Literal["1", "2", "2010-04-01"] = "2"
    whitelisted_destinations: list[CountryCode]


class OrderedNumber(_Body):
    phone_number: E164


class NumberOrderCreate(_Body):
    phone_numbers: Annotated[list[OrderedNumber], Field(min_length=1, max_length=20)]
    connection_id: str | None = None
    messaging_profile_id: str | None = None
    customer_reference: str | None = None


class PhoneNumberUpdate(_Body):
    connection_id: str | None = None
    tags: list[str] | None = None
    customer_reference: str | None = None
    external_pin: str | None = None


class PhoneNumberMessagingUpdate(_Body):
    messaging_profile_id: str | None = None


class MessageCreate(_Body):
    """``POST /v2/messages`` (specs/21-messaging-sms.md §2 step 6); ``text`` or ``media_urls`` is required."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: E164 = Field(alias="from")
    to: E164
    text: str | None = None
    media_urls: list[WebhookUrl] = Field(default_factory=list)
    subject: str | None = None
    type: Literal["SMS", "MMS"] | None = None
    messaging_profile_id: str | None = None
    webhook_url: WebhookUrl | None = None
    webhook_failover_url: WebhookUrl | None = None
    use_profile_webhooks: bool = True
    auto_detect: bool = False


class AccountCreate(_Body):
    """``POST /sim/telnyx/accounts``: register another API key (BYO account tests)."""

    api_key: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class TelnyxSimConfig(_Body):
    """FakeTelnyx behaviour knobs (``GET``/``PATCH /sim/telnyx/config``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    force_duplicate_webhooks: bool = False
    fail_number_orders: bool = False
    failing_message_destinations: tuple[E164, ...] = ()


class TelnyxConfigPatch(_Body):
    """Partial update of :class:`TelnyxSimConfig`; omitted fields keep their value."""

    force_duplicate_webhooks: bool | None = None
    fail_number_orders: bool | None = None
    failing_message_destinations: tuple[E164, ...] | None = None


class InboundMessage(_Body):
    """``POST /sim/telnyx/messages/inbound``: a message from ``from`` to one of the account numbers."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: E164 = Field(alias="from")
    to: E164
    text: str = ""
    media_urls: list[WebhookUrl] = Field(default_factory=list)
