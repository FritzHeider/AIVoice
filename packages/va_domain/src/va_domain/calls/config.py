"""`SendCallConfig`: the resolved Send Call configuration persisted in `calls.config` (specs/05-calls-api.md
§4.4). Field names, types and defaults match the spec block exactly (`tests/test_call_config.py`)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_domain.calls.parts import (
    DialingStrategy,
    GuardRail,
    PostCallEvals,
    PronunciationRule,
    VoicemailSms,
    WarmTransferConfig,
)
from va_domain.calls.tools import DynamicDataSpec, InlineTool
from va_domain.voices import VoiceSettings

__all__ = ["DEFAULT_ERROR_PHRASE", "RetryConfig", "SendCallConfig", "VoicemailConfig"]

DEFAULT_ERROR_PHRASE = "I'm sorry, I'm having trouble on my end. Let me get someone to help you."


# jscpd:ignore-start  (the field blocks mirror specs/05 §4.4 on purpose)
class VoicemailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["hangup", "leave_message", "leave_message_and_sms", "ignore"] = "hangup"
    message: str | None = None
    sms: VoicemailSms | None = None
    sensitive: bool = False
    detect_timeout_ms: int = 12000


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait: int
    voicemail_action: Literal["hangup", "leave_message", "ignore"] = "hangup"
    voicemail_message: str | None = None
    max_attempts: int = 1
    on_no_answer: bool = False


class SendCallConfig(BaseModel):
    """The configuration a call runs with, after every layer of §4.3 was applied."""

    model_config = ConfigDict(extra="forbid")

    # runner
    runner_type: Literal["task", "pathway", "persona", "agent"]
    task: str | None = None
    pathway_id: UUID | None = None
    pathway_version: int | None = None
    start_node_id: str | None = None
    persona_id: UUID | None = None
    persona_version: int | None = None
    agent_id: UUID | None = None
    agent_version_id: UUID | None = None
    experiment_id: UUID | None = None
    experiment_arm_id: UUID | None = None
    # basic
    phone_number: str
    country: str = "US"
    voice_id: UUID
    provider_voice_id: str
    voice_settings: VoiceSettings
    first_sentence: str | None = None
    wait_for_greeting: bool = False
    greeting_timeout_ms: int = 6000
    language: str = "babel-en"
    dialogue_model: str
    router_model: str
    temperature: float = 0.7
    max_tokens: int = 250
    pronunciation_guide: list[PronunciationRule] = Field(default_factory=list)
    interruption_threshold_ms: int | None = None
    interruptibility: int = 2
    block_interruptions: bool = False
    noise_cancellation: bool = True
    background_track: Literal["office", "cafe", "restaurant", "none"] | None = None
    hd_audio: bool = False
    record: bool = False
    max_duration_min: float = 30
    # dispatch
    from_number: str
    phone_number_id: UUID | None
    telnyx_account_id: UUID | None = None
    dialing_strategy: DialingStrategy = Field(default_factory=DialingStrategy)
    timezone: str = "America/Los_Angeles"
    start_time: datetime | None = None
    transfer_phone_number: str | None = None
    transfer_list: dict[str, str] = Field(default_factory=dict)
    warm_transfer: WarmTransferConfig = Field(default_factory=WarmTransferConfig)
    # knowledge
    tool_ids: list[UUID] = Field(default_factory=list)
    inline_tools: list[InlineTool] = Field(default_factory=list)
    knowledge_base_ids: list[UUID] = Field(default_factory=list)
    memory_id: UUID | None = None
    memory_enabled: bool = False
    # voicemail
    ivr_mode: bool = False
    answered_by_enabled: bool = True
    voicemail: VoicemailConfig = Field(default_factory=VoicemailConfig)
    # analysis
    summary_prompt: str | None = None
    dispositions: list[str]
    analysis_schema: dict[str, str] | None = None
    analysis_prompt: str | None = None
    citation_schema_ids: list[UUID] = Field(default_factory=list)
    post_call_evals: PostCallEvals | None = None
    stream_post_transfer_transcript: bool = False
    retry: RetryConfig | None = None
    # advanced
    request_data: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    external_id: str | None = None
    webhook: str | None = None
    webhook_events: list[str] = Field(default_factory=list)
    dynamic_data: list[DynamicDataSpec] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    ignore_button_press: bool = False
    precall_dtmf_sequence: str | None = None
    guard_rails: list[GuardRail] = Field(default_factory=list)
    test_mode: bool = False
    # derived
    max_reprompts: int = 2
    no_input_timeout_ms: int = 8000
    error_phrase: str = DEFAULT_ERROR_PHRASE


# jscpd:ignore-end  (test_call_config.py checks the blocks against the spec)
