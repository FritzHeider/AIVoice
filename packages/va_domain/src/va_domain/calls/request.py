"""The `POST /v1/calls` body (specs/05-calls-api.md §3): every parameter, legacy aliases, and the pure rule
checks of validation step 4 (§4.1).

`CallParameters` holds every call parameter except the runner fields and `phone_number`; lower config
layers (org `call_defaults`, pathway `global_config.call_defaults`, agent `call_settings`, persona config)
parse with it too. `SendCallRequest` adds the runner fields and the destination.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StrictInt, StringConstraints

from va_domain.calls.errors import CallRequestError
from va_domain.calls.parts import (
    AnalysisSchema,
    CountryHint,
    DialingStrategy,
    DispositionName,
    ExternalId,
    GuardRail,
    JsonObject,
    Keyword,
    LanguageCode,
    PostCallEvals,
    PronunciationRule,
    RequestData,
    RetryRequest,
    StartTime,
    TransferList,
    TransferTarget,
    VoicemailRequest,
    WarmTransferConfig,
    WebhookEvents,
)
from va_domain.calls.tools import DynamicDataSpec, InlineTool, KbRef, ToolRef
from va_domain.org import E164, HttpUrl, Timezone
from va_domain.voices import VoiceSettingsPatch

__all__ = [
    "PARAMETER_NAMES",
    "CallParameters",
    "RunnerType",
    "SendCallRequest",
    "unknown_fields",
]

RunnerType = Literal["agent", "persona", "pathway", "task"]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
AGENT_VERSION_PATTERN: Final = (
    r"^(production|staging|dev|latest|\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?|branch:[A-Za-z0-9._/-]{1,64})$"
)
_LOCAL: Final = DialingStrategy.model_validate({"type": "local"})
# Legacy aliases folded into their canonical parameter by `explicit_fields`.
_FOLDED_ALIASES: Final = frozenset({"amd", "available_tags", "local_dialing", "geospatial_dialing"})


def _persona_setting_key(value: str) -> str:
    if value not in PARAMETER_NAMES:
        msg = f"{value!r} is not a call parameter"
        raise ValueError(msg)
    return value


class CallParameters(BaseModel):
    """Every §3.2-§3.7 parameter with its validation; defaults are the §3 defaults (`None` where the default
    comes from another layer, e.g. the org default voice)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, populate_by_name=True)

    # basic (§3.2)
    voice: str | None = Field(
        default=None, min_length=1, max_length=100, description="Voice name or voice UUID."
    )
    voice_settings: VoiceSettingsPatch | None = Field(default=None, description="Per-call TTS overrides.")
    first_sentence: str | None = Field(
        default=None, max_length=1000, description="First utterance, verbatim."
    )
    wait_for_greeting: bool = Field(default=False, description="Stay silent until the callee speaks.")
    language: LanguageCode = Field(default="babel-en", description="STT/TTS language (specs/08 §5).")
    model: str = Field(
        default="base", min_length=1, max_length=200, description="base, turbo, enhanced or an id."
    )
    temperature: float = Field(default=0.7, ge=0, le=1)
    max_tokens: int = Field(default=250, ge=32, le=1024, description="Max tokens per agent reply.")
    pronunciation_guide: list[PronunciationRule] = Field(default_factory=list, max_length=200)
    interruption_threshold: int | None = Field(
        default=None, ge=50, le=5000, description="Fixed EOT silence (ms)."
    )
    interruptibility: int = Field(default=2, ge=0, le=4, description="0 block … 4 always.")
    block_interruptions: bool = False
    noise_cancellation: bool = True
    background_track: Literal["office", "cafe", "restaurant", "none"] | None = None
    hd_audio: bool = False
    record: bool = False
    max_duration: float = Field(default=30, ge=0.5, le=240, description="Hard limit in minutes.")
    # dispatch (§3.3)
    from_: E164 | None = Field(default=None, validation_alias="from", serialization_alias="from")
    dialing_strategy: DialingStrategy | None = None
    timezone: Timezone = "America/Los_Angeles"
    start_time: StartTime | None = Field(default=None, examples=["2026-11-20 09:30:00 -08:00"])
    transfer_phone_number: TransferTarget | None = None
    transfer_list: TransferList = Field(default_factory=dict)
    warm_transfer: WarmTransferConfig | None = None
    country: CountryHint = Field(default="US", description="Region for numbers without a + prefix.")
    # knowledge and tools (§3.4)
    tools: list[ToolRef | InlineTool] = Field(default_factory=list, max_length=30)
    knowledge_base_ids: list[KbRef] = Field(default_factory=list, max_length=10)
    memory_id: UUID | None = None
    memory_enabled: bool | None = None
    # voicemail and AMD (§3.5)
    ivr_mode: bool = False
    amd: bool = Field(default=False, description="Legacy alias of ivr_mode.")
    voicemail: VoicemailRequest | None = None
    voicemail_detect_timeout_ms: int = Field(default=12_000, ge=3000, le=30_000)
    answered_by_enabled: bool = True
    # analysis and post-call (§3.6)
    summary_prompt: str | None = Field(default=None, max_length=2000)
    dispositions: list[DispositionName] | None = Field(default=None, max_length=50)
    available_tags: list[DispositionName] | None = Field(
        default=None, max_length=50, description="Legacy alias of dispositions."
    )
    analysis_schema: AnalysisSchema | None = None
    analysis_prompt: str | None = Field(default=None, max_length=2000)
    citation_schema_ids: list[UUID] = Field(default_factory=list, max_length=10)
    post_call_evals: PostCallEvals | None = None
    stream_post_transfer_transcript: bool = False
    retry: RetryRequest | None = None
    # advanced (§3.7)
    request_data: RequestData = Field(default_factory=dict, description="Variables available as {{key}}.")
    metadata: JsonObject = Field(
        default_factory=dict, description="Echoed in webhooks; never shown to the LLM."
    )
    external_id: ExternalId | None = None
    webhook: HttpUrl | None = None
    webhook_events: WebhookEvents = Field(default_factory=list)
    dynamic_data: list[DynamicDataSpec] = Field(default_factory=list, max_length=10)
    keywords: list[Keyword] = Field(default_factory=list, max_length=100)
    ignore_button_press: bool = False
    precall_dtmf_sequence: str | None = Field(default=None, pattern=r"^[0-9*#w]{1,64}$")
    test_call: bool = False
    guard_rails: list[GuardRail] = Field(default_factory=list, max_length=20)
    local_dialing: bool = Field(default=False, description="Legacy alias of dialing_strategy local.")
    geospatial_dialing: bool = Field(default=False, description="Legacy alias of local_dialing.")
    test_mode: bool = Field(default=False, description="Route the call to the simulator caller.")

    def explicit_fields(self) -> dict[str, Any]:
        """Parameter name -> value for the parameters present in the body, legacy aliases folded.

        `amd` -> `ivr_mode`, `available_tags` -> `dispositions`, `local_dialing`/`geospatial_dialing`
        -> `dialing_strategy` local (the canonical field wins when both are sent); `knowledge_base_ids`
        join the KB entries of `tools`; `voicemail` reports only its sent keys (a dict).
        """
        sent = self.model_fields_set & _PARAMETER_FIELDS
        values: dict[str, Any] = {
            ("from" if name == "from_" else name): getattr(self, name)
            for name in _PARAMETER_FIELD_ORDER
            if name in sent and name not in _FOLDED_ALIASES
        }
        values.update(self._folded_aliases(sent))
        if "knowledge_base_ids" in sent:
            del values["knowledge_base_ids"]
            tools: list[str | InlineTool] = list(self.tools)
            tools.extend(kb for kb in dict.fromkeys(self.knowledge_base_ids) if kb not in tools)
            values["tools"] = tools
        if self.voicemail is not None and "voicemail" in sent:
            values["voicemail"] = self.voicemail.sent()
        return values

    def _folded_aliases(self, sent: AbstractSet[str]) -> dict[str, Any]:
        folded: dict[str, Any] = {}
        if "amd" in sent and "ivr_mode" not in sent:
            folded["ivr_mode"] = self.amd
        if "available_tags" in sent and "dispositions" not in sent:
            folded["dispositions"] = self.available_tags
        legacy_local = {
            name for name in ("local_dialing", "geospatial_dialing") if name in sent and getattr(self, name)
        }
        if legacy_local and "dialing_strategy" not in sent:
            folded["dialing_strategy"] = _LOCAL
        return folded


_PARAMETER_FIELD_ORDER: Final = tuple(CallParameters.model_fields)
_PARAMETER_FIELDS: Final = frozenset(_PARAMETER_FIELD_ORDER)
PARAMETER_NAMES: Final = frozenset({"from" if name == "from_" else name for name in _PARAMETER_FIELDS})
PersonaSettingKey = Annotated[str, AfterValidator(_persona_setting_key)]


class SendCallRequest(CallParameters):
    """The full Send Call body: runner selection (§3.1) and the destination on top of `CallParameters`."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "phone_number": "+14155550100",
                    "task": "You are Maya from Bella Pasta. Confirm tonight's reservation for {{name}}.",
                    "first_sentence": "Hi {{name}}, this is Maya from Bella Pasta.",
                    "voice": "Karen",
                    "request_data": {"name": "Jordan"},
                    "max_duration": 5,
                    "record": True,
                    "webhook": "https://hooks.bellapasta.test/calls",
                }
            ]
        },
    )

    # runner (§3.1)
    task: str | None = Field(
        default=None, min_length=1, max_length=16_000, description="Free-form prompt runner."
    )
    pathway_id: UUID | None = None
    pathway_version: NonNegativeInt | Literal["production", "staging", "draft"] | None = Field(
        default=None, description="Integer, production (default), staging, or draft (test calls only)."
    )
    start_node_id: str | None = Field(default=None, min_length=1, max_length=128)
    persona_id: UUID | None = None
    persona_version: NonNegativeInt | Literal["production", "draft"] | None = None
    persona_settings: dict[PersonaSettingKey, JsonValue] = Field(
        default_factory=dict, description="Per-call overrides applied after the persona config."
    )
    agent_id: UUID | None = None
    agent_version: Annotated[str, StringConstraints(pattern=AGENT_VERSION_PATTERN)] | None = None
    # destination (§3.2)
    phone_number: str = Field(min_length=1, max_length=64, description="Destination; normalized to E.164.")

    @property
    def runner_type(self) -> RunnerType:
        """agent, then persona (a pathway alongside it overrides the persona's pathway), pathway, task."""
        if self.agent_id is not None:
            return "agent"
        if self.persona_id is not None:
            return "persona"
        if self.pathway_id is not None:
            return "pathway"
        return "task"

    def check_rules(self) -> None:
        """Raise the first rule failure: `RETRY_NOT_PROVIDED`, then the runner rules (`RUNNER_CONFLICT`)."""
        if self.retry is not None and self.retry.wait is None:
            msg = "Retry attempt not provided for retry call."
            raise CallRequestError("RETRY_NOT_PROVIDED", msg, "retry.wait")
        conflict = self._runner_conflict()
        if conflict is not None:
            raise CallRequestError("RUNNER_CONFLICT", *conflict)

    def _runner_conflict(self) -> tuple[str, str] | None:
        """(message, field) of the first §3.1 runner rule the request breaks."""
        if self.agent_id is None:
            if self.task is None and self.pathway_id is None and self.persona_id is None:
                return "One of task, pathway_id, persona_id or agent_id is required.", "task"
            return None
        if self.persona_id is not None or self.pathway_id is not None:
            return "agent_id cannot be combined with persona_id or pathway_id.", "agent_id"
        if self.task is not None:
            return "task must be omitted when agent_id is set.", "task"
        return None


def _known_keys() -> frozenset[str]:
    names = set(SendCallRequest.model_fields)
    return frozenset(names | {"from"})


_KNOWN_KEYS: Final = _known_keys()


def unknown_fields(raw: Mapping[str, object]) -> list[str]:
    """Top-level body keys that are not Send Call parameters (ignored; the endpoint logs them at debug)."""
    return [key for key in raw if key not in _KNOWN_KEYS]
