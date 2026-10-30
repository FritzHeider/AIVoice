"""Telnyx response shapes of specs/30 §3.2 and specs/06 §2.1, written independently of the simulator code.

Models allow extra fields (Telnyx adds fields over time) but type every field the platform reads.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Shape(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class ErrorSource(_Shape):
    pointer: str | None = None
    parameter: str | None = None


class Error(_Shape):
    code: str
    title: str
    detail: str
    source: ErrorSource | None = None


class Errors(_Shape):
    errors: list[Error]


class Balance(_Shape):
    record_type: Literal["balance"]
    balance: str
    credit_limit: str
    available_credit: str
    pending: str
    currency: Literal["USD"]


class Inbound(_Shape):
    channel_limit: int | None
    sip_subdomain_receive_settings: str


class Outbound(_Shape):
    outbound_voice_profile_id: str | None
    channel_limit: int | None


class CallControlApplication(_Shape):
    id: str
    record_type: Literal["call_control_application"]
    application_name: str
    webhook_event_url: str
    webhook_event_failover_url: str
    webhook_api_version: Literal["1", "2"]
    webhook_timeout_secs: int | None
    first_command_timeout: bool
    first_command_timeout_secs: int
    anchorsite_override: str
    dtmf_type: str
    active: bool
    inbound: Inbound
    outbound: Outbound
    created_at: str
    updated_at: str


class OutboundVoiceProfile(_Shape):
    id: str
    record_type: Literal["outbound_voice_profile"]
    name: str
    traffic_type: str
    service_plan: str
    concurrent_call_limit: int | None
    enabled: bool
    tags: list[str]
    whitelisted_destinations: list[str]
    connections_count: int
    usage_payment_method: str
    created_at: str
    updated_at: str


class MessagingProfile(_Shape):
    id: str
    record_type: Literal["messaging_profile"]
    name: str
    enabled: bool
    webhook_url: str | None
    webhook_failover_url: str | None
    webhook_api_version: str
    whitelisted_destinations: list[str]
    created_at: str
    updated_at: str


class Feature(_Shape):
    name: Literal["voice", "sms", "mms"]


class CostInformation(_Shape):
    monthly_cost: str
    upfront_cost: str
    currency: Literal["USD"]


class Region(_Shape):
    region_type: str
    region_name: str


class AvailablePhoneNumber(_Shape):
    record_type: Literal["available_phone_number"]
    phone_number: str
    features: list[Feature]
    cost_information: CostInformation
    region_information: list[Region]
    best_effort: bool
    reservable: bool


class OrderPhoneNumber(_Shape):
    id: str
    record_type: Literal["number_order_phone_number"]
    phone_number: str
    status: Literal["pending", "success", "failure"]
    regulatory_requirements: list[Any]


class NumberOrder(_Shape):
    id: str
    record_type: Literal["number_order"]
    phone_numbers: list[OrderPhoneNumber]
    phone_numbers_count: int
    connection_id: str | None
    messaging_profile_id: str | None
    customer_reference: str | None
    status: Literal["pending", "success", "failure"]
    requirements_met: bool
    created_at: str
    updated_at: str


class PhoneNumber(_Shape):
    id: str
    record_type: Literal["phone_number"]
    phone_number: str
    status: str
    connection_id: str | None
    messaging_profile_id: str | None
    customer_reference: str | None
    tags: list[str]
    created_at: str
    updated_at: str


class MessagingPhoneNumber(_Shape):
    id: str
    record_type: Literal["messaging_phone_number"]
    phone_number: str
    messaging_profile_id: str | None
    updated_at: str


class Party(_Shape):
    phone_number: str
    carrier: str
    line_type: str


class Recipient(Party):
    status: str


class Media(_Shape):
    url: str
    content_type: str | None
    sha256: str | None
    size: int | None


class MessageError(_Shape):
    code: str
    title: str
    detail: str


class Message(_Shape):
    record_type: Literal["message"]
    direction: Literal["inbound", "outbound"]
    id: str
    type: Literal["SMS", "MMS"]
    messaging_profile_id: str | None
    from_: Party = Field(alias="from")
    to: list[Recipient]
    text: str
    subject: str | None
    media: list[Media]
    webhook_url: str | None
    encoding: Literal["GSM-7", "UCS-2"]
    parts: int
    tags: list[str]
    cost: Any
    received_at: str
    sent_at: str | None
    completed_at: str | None
    valid_until: str | None
    errors: list[MessageError]


class EventData(_Shape):
    record_type: Literal["event"]
    id: str
    event_type: str
    occurred_at: str
    payload: dict[str, Any]


class EventMeta(_Shape):
    attempt: int
    delivered_to: str


class WebhookEnvelope(_Shape):
    data: EventData
    meta: EventMeta


class DialResponse(_Shape):
    record_type: Literal["call"]
    call_control_id: str = Field(pattern=r"^v3:[A-Za-z0-9_-]+$")
    call_leg_id: str
    call_session_id: str
    is_alive: bool
    client_state: str | None


class CommandResult(_Shape):
    result: Literal["ok"]


class Conference(_Shape):
    id: str
    name: str
    record_type: Literal["conference"]
    status: Literal["init", "in_progress", "completed"]
    connection_id: str
    created_at: str
    expires_at: str
    region: str


class CallEventPayload(_Shape):
    """Fields every call.* webhook payload carries (specs/30 §3.2 Webhooks)."""

    model_config = ConfigDict(extra="allow", strict=True, populate_by_name=True)

    call_control_id: str
    call_leg_id: str
    call_session_id: str
    connection_id: str
    client_state: str | None
    from_: str = Field(alias="from")
    to: str
    direction: Literal["incoming", "outgoing"]
    state: Literal["bridging", "parked", "answered", "bridged", "hangup"]
    start_time: str


class HangupPayload(CallEventPayload):
    hangup_cause: Literal[
        "normal_clearing",
        "originator_cancel",
        "timeout",
        "time_limit",
        "user_busy",
        "not_found",
        "no_answer",
        "call_rejected",
        "unspecified",
    ]
    hangup_source: Literal["caller", "callee", "unknown"]
    sip_hangup_cause: str
    end_time: str


class RecordingUrls(_Shape):
    wav: str | None
    mp3: str | None


class RecordingSavedPayload(CallEventPayload):
    recording_id: str
    channels: Literal["single", "dual"]
    format: Literal["wav", "mp3"]
    recording_started_at: str
    recording_ended_at: str
    recording_urls: RecordingUrls
    public_recording_urls: RecordingUrls


class AmdPayload(CallEventPayload):
    result: str


class ConferenceEventPayload(_Shape):
    conference_id: str
    call_control_id: str
    call_leg_id: str
    call_session_id: str
    client_state: str | None
    connection_id: str


class PageMeta(_Shape):
    page_number: int
    page_size: int
    total_pages: int
    total_results: int


def assert_shape[M: BaseModel](model: type[M], body: Any) -> M:
    """``body`` validated as ``model`` (raises ``pydantic.ValidationError`` when the shape is wrong)."""
    return model.model_validate(body)


def assert_shapes[M: BaseModel](model: type[M], items: list[Any]) -> list[M]:
    return [model.model_validate(item) for item in items]
