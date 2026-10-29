"""Org settings (specs/04-auth-tenancy-rbac.md §7): the model behind `orgs.settings` and its merge helpers.

`orgs.settings` is stored sparse: it holds only the keys a client set explicitly, and `load_settings`
fills every other field from the model defaults at read time, so a later default change reaches every
org that never overrode it. PATCH bodies are parsed with the same model and read back with
`exclude_unset=True`, which is exactly what the client sent. Groups (sub-models) merge key by key;
every other field, lists and maps included, is replaced whole.
"""

from __future__ import annotations

import copy
import ipaddress
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_validator import EmailNotValidError, validate_email
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
)

from va_domain.scheduling import ALL_DAYS, CallingWindow, unique_items, windows_overlap
from va_domain.voices import VoiceSettings

__all__ = [
    "COMPLIANCE_PATHS",
    "SETTINGS_GROUPS",
    "AnalysisSettings",
    "ComplianceSettings",
    "EngineSettings",
    "ExperimentsSettings",
    "GuardRailConfig",
    "LlmSettings",
    "MemorySettings",
    "OrgSettings",
    "PrivacySettings",
    "RedactionSettings",
    "SmsSettings",
    "ToolsSettings",
    "VoicesSettings",
    "effective_values",
    "field_path",
    "load_settings",
    "merge_settings",
    "needs_compliance",
    "sent_paths",
    "sparse_settings",
]

_MAX_URL_LENGTH = 2048
_MAX_CALLING_WINDOWS = 14


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        msg = f"unknown IANA timezone {value!r}"
        raise ValueError(msg) from exc
    return value


def _url(value: str, schemes: frozenset[str]) -> str:
    parts = urlsplit(value)
    if parts.scheme not in schemes or not parts.hostname:
        allowed = " or ".join(sorted(schemes))
        msg = f"must be an absolute {allowed} URL"
        raise ValueError(msg)
    return value


def _http_url(value: str) -> str:
    return _url(value, frozenset({"http", "https"}))


def _https_url(value: str) -> str:
    return _url(value, frozenset({"https"}))


def _cidr(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as exc:
        msg = f"{value!r} is not an IPv4 or IPv6 network"
        raise ValueError(msg) from exc


def _email(value: str) -> str:
    # Syntax only (no DNS); `.test` domains are accepted so seeded development orgs validate.
    try:
        return validate_email(value, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError as exc:
        msg = f"not a valid email address: {exc}"
        raise ValueError(msg) from exc


def _calling_hours(windows: list[CallingWindow]) -> list[CallingWindow]:
    if windows_overlap(windows):
        msg = "calling windows on the same day must not overlap"
        raise ValueError(msg)
    return windows


Timezone = Annotated[str, StringConstraints(min_length=1, max_length=64), AfterValidator(_timezone)]
E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{1,14}$")]
E164Prefix = Annotated[str, StringConstraints(pattern=r"^\+[0-9]{1,15}$")]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
HttpUrl = Annotated[str, StringConstraints(max_length=_MAX_URL_LENGTH), AfterValidator(_http_url)]
HttpsUrl = Annotated[str, StringConstraints(max_length=_MAX_URL_LENGTH), AfterValidator(_https_url)]
Cidr = Annotated[str, AfterValidator(_cidr)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=320), AfterValidator(_email)]
Message = Annotated[str, StringConstraints(min_length=1, max_length=500)]
ModelName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
EventName = Annotated[str, StringConstraints(min_length=1, max_length=64)]
EntityName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
Disposition = Annotated[str, StringConstraints(min_length=1, max_length=100)]


def _default_calling_hours() -> list[CallingWindow]:
    return [CallingWindow(days=list(ALL_DAYS), start="08:00", end="21:00")]


class ComplianceSettings(BaseModel):
    """Calling hours, DNC inheritance, and disclosure policies (specs/23-guardrails-and-compliance.md §2)."""

    model_config = ConfigDict(extra="forbid")

    enforce_calling_hours: bool = Field(
        default=False, description="Apply `calling_hours` to non-batch API calls as well as batches."
    )
    calling_hours: Annotated[list[CallingWindow], AfterValidator(_calling_hours)] = Field(
        default_factory=_default_calling_hours,
        min_length=1,
        max_length=_MAX_CALLING_WINDOWS,
        description="Windows in the callee's local timezone; windows of one day must not overlap.",
    )
    dnc_inherit_from_parent: bool = Field(
        default=True, description="A sub-account also honors its parent's do-not-call list."
    )
    state_quiet_hours: bool = Field(
        default=True, description="Apply stricter state-specific calling hours on top of `calling_hours`."
    )
    max_attempts_per_number_per_day: int = Field(
        default=0, ge=0, le=100, description="Outbound attempts per number per day; 0 means unlimited."
    )
    recording_disclosure_policy: Literal["always", "auto", "never"] = Field(
        default="auto", description="When the recording-disclosure guard rail is added to recorded calls."
    )
    ai_disclosure_policy: Literal["always", "auto", "never"] = Field(
        default="always", description="When the AI-disclosure guard rail is added to calls."
    )
    require_consent_attestation: bool = Field(
        default=True, description="Batches must carry `global_props.consent_attestation=true`."
    )


class LlmSettings(BaseModel):
    """Per-org LLM override and dialogue limits (specs/10-llm-gateway.md)."""

    model_config = ConfigDict(extra="forbid")

    base_url: HttpsUrl | None = Field(
        default=None, description="OpenAI-compatible base URL replacing the platform's."
    )
    api_key_ref: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,64}$")] | None = Field(
        default=None, description="Name of the org secret (`PUT /v1/org/secrets/{name}`) holding the API key."
    )
    dialogue_model: ModelName | None = Field(default=None, description="Model for dialogue turns.")
    router_model: ModelName | None = Field(default=None, description="Model for pathway routing.")
    analysis_model: ModelName | None = Field(default=None, description="Model for post-call analysis.")
    fallback_model: ModelName | None = Field(default=None, description="Model used when the primary fails.")
    temperature_default: float | None = Field(
        default=None, ge=0, le=2, description="Default sampling temperature."
    )
    allow_model_override: bool = Field(
        default=False, description="Allow per-call `model` values outside the platform model allowlist."
    )
    context_budget_tokens: int = Field(
        default=6000, ge=1000, le=32000, description="Max prompt tokens for dialogue context assembly."
    )


class PrivacySettings(BaseModel):
    """Transcript redaction shorthand and vendor zero retention."""

    model_config = ConfigDict(extra="forbid")

    redact_transcripts: bool = Field(
        default=False, description="Forces `pii_redaction` to at least `transcripts`."
    )
    zero_retention: bool = Field(
        default=False, description="Vendor calls use zero-retention options; requires the enterprise plan."
    )


class RedactionSettings(BaseModel):
    """Real-time PII/PCI redaction (specs/23-guardrails-and-compliance.md §6)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False, description="Redact entity spans in stored transcripts in real time."
    )
    redact_llm_context: bool = Field(
        default=False, description="Also replace redacted spans in the live LLM context."
    )


class AnalysisSettings(BaseModel):
    """Post-call analysis switches (specs/18-post-call-analysis.md)."""

    model_config = ConfigDict(extra="forbid")

    retranscribe: bool | None = Field(
        default=None,
        description="Diarized re-transcription after the call; null means on whenever the call is recorded.",
    )
    sentiment: bool = Field(default=True, description="Run sentiment analysis on connected calls.")


class MemorySettings(BaseModel):
    """Cross-channel memory defaults."""

    model_config = ConfigDict(extra="forbid")

    default_enabled: bool = Field(
        default=False, description="Memory on for calls and personas that do not specify it."
    )


class ToolsSettings(BaseModel):
    """Built-in tools allowed in calls."""

    model_config = ConfigDict(extra="forbid")

    allow_send_sms: bool = Field(default=True, description="Built-in SMS tool and node allowed.")
    allow_schedule_callback: bool = Field(
        default=False, description="Built-in schedule-callback tool allowed."
    )


class EngineSettings(BaseModel):
    """Dialogue engine behaviour."""

    model_config = ConfigDict(extra="forbid")

    speculative: bool = Field(
        default=True, description="Generate the dialogue reply in parallel with pathway routing."
    )
    tool_fillers: bool = Field(default=True, description="Speak tool filler phrases while tools run.")
    allow_callbacks: bool = Field(default=False, description="The agent may schedule callback calls.")


class VoicesSettings(BaseModel):
    """Voice resolution rules."""

    model_config = ConfigDict(extra="forbid")

    allow_raw_provider_ids: bool = Field(
        default=False, description="`voice` may be a raw ElevenLabs voice_id that is not in the catalog."
    )


class ExperimentsSettings(BaseModel):
    """Experiment arm assignment."""

    model_config = ConfigDict(extra="forbid")

    sticky_by_contact: bool = Field(
        default=False, description="Hash arm assignment by contact instead of by call id."
    )


class SmsSettings(BaseModel):
    """Outbound SMS encoding (specs/21-messaging-sms.md)."""

    model_config = ConfigDict(extra="forbid")

    smart_encoding: bool = Field(
        default=True, description="Replace curly quotes, dashes, and ellipses so messages stay GSM-7."
    )


class GuardRailConfig(BaseModel):
    """A default guard rail (specs/23-guardrails-and-compliance.md §1.1); other keys are kept as sent."""

    model_config = ConfigDict(extra="allow")

    type: Literal[
        "tcpa:ai_disclosure", "tcpa:self_introduction", "tcpa:recording_disclosure", "tcpa:opt_out", "custom"
    ] = Field(description="Guard rail type.")


class OrgSettings(BaseModel):
    """Every org setting with its default; `orgs.settings` stores only the keys that were set explicitly."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "timezone": "America/New_York",
                    "default_record": True,
                    "compliance": {"state_quiet_hours": True},
                }
            ]
        },
    )

    timezone: Timezone = Field(
        default="America/Los_Angeles", description="Default timezone for calls and calling windows."
    )
    default_language: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,16}$")] = Field(
        default="en", description="Default call `language`."
    )
    default_voice: Annotated[str, StringConstraints(min_length=1, max_length=100)] = Field(
        default="Karen", description="Default `voice` (voice name or id)."
    )
    default_from_number: E164 | None = Field(
        default=None, description="Default caller ID when `from` is absent."
    )
    default_max_duration_min: int = Field(
        default=30, ge=1, le=240, description="Default `max_duration` (minutes)."
    )
    default_record: bool = Field(default=False, description="Default `record`.")
    default_webhook: HttpUrl | None = Field(default=None, description="Applied when a call omits `webhook`.")
    default_webhook_events: Annotated[list[EventName], AfterValidator(unique_items)] = Field(
        default_factory=list, max_length=50, description="Default `webhook_events`."
    )
    default_voice_id: uuid.UUID | None = Field(
        default=None, description="Default voice by id; takes precedence over `default_voice`."
    )
    default_country: CountryCode = Field(default="US", description="Region used to parse non-E.164 numbers.")
    call_defaults: dict[str, JsonValue] = Field(
        default_factory=dict, description="Org-wide Send Call defaults below agent and persona defaults."
    )
    dial_timeout_s: int = Field(default=45, ge=5, le=120, description="Seconds to ring before giving up.")
    transfer_caller_id_mode: Literal["agent_number", "original_caller"] = Field(
        default="agent_number", description="Caller ID presented to transfer targets."
    )
    transfer_failed_message: Message = Field(
        default="I'm sorry, I wasn't able to connect you. Is there anything else I can help with?",
        description="Spoken when a transfer leg fails.",
    )
    max_duration_message: Message = Field(
        default="I'm sorry, we've reached the time limit for this call. Goodbye.",
        description="Spoken before the `max_duration` hangup.",
    )
    no_input_goodbye: Message = Field(
        default="I haven't heard anything, so I'll end the call now. Goodbye.",
        description="Spoken after repeated no-input timeouts.",
    )
    pathway_version_fallback: bool = Field(
        default=True, description="A missing numeric pathway version falls back to production."
    )
    voice_overrides: dict[uuid.UUID, VoiceSettings] = Field(
        default_factory=dict, max_length=200, description="Org-level voice settings per voice id."
    )
    default_guard_rails: list[GuardRailConfig] = Field(
        default_factory=list,
        max_length=10,
        description="Guard rails applied to every call unless overridden.",
    )
    ai_disclosure_required: bool = Field(
        default=False, description="Forces the `tcpa:ai_disclosure` guard rail whatever the policy."
    )
    recording_disclosure_required: bool = Field(
        default=True, description="Forces `tcpa:recording_disclosure` on recorded calls."
    )
    honor_opt_out: bool = Field(default=True, description="Opt-out phrases write do-not-call entries.")
    allow_insecure_webhooks: bool = Field(
        default=False, description="Permit `http://` webhook URLs in production."
    )
    webhook_format: Literal["bland", "envelope"] = Field(
        default="bland", description="Default format for per-call webhooks."
    )
    hipaa_mode: bool = Field(default=False, description="HIPAA handling; requires `baa_signed_at`.")
    baa_signed_at: AwareDatetime | None = Field(
        default=None, description="When the BAA was signed; set by platform administrators."
    )
    pii_redaction: Literal["off", "transcripts", "transcripts_and_recordings"] = Field(
        default="off", description="Post-call redaction."
    )
    pii_redaction_entities: Annotated[list[EntityName], AfterValidator(unique_items)] = Field(
        default_factory=lambda: ["pii", "phi", "pci"],
        max_length=20,
        description="Scribe entity categories to redact.",
    )
    viewers_can_see_pii: bool = Field(default=False, description="Viewers see unmasked PII.")
    mfa_required: bool = Field(default=False, description="Members must enrol MFA.")
    session_max_age_days: int = Field(
        default=14, ge=1, le=90, description="Dashboard session sliding expiry window (days)."
    )
    ip_allowlist: list[Cidr] = Field(
        default_factory=list,
        max_length=100,
        description="When non-empty, API keys and sessions are accepted only from these networks.",
    )
    llm: LlmSettings = Field(default_factory=LlmSettings, description="Per-org LLM override.")
    international_enabled: bool = Field(
        default=False, description="International calling; also requires plan and KYC."
    )
    allowed_countries: Annotated[list[CountryCode], AfterValidator(unique_items)] = Field(
        default_factory=lambda: ["US", "CA"],
        max_length=250,
        description="Destinations allowed when international calling is enabled.",
    )
    blocked_prefixes: list[E164Prefix] = Field(
        default_factory=list, max_length=200, description="Extra E.164 prefixes blocked."
    )
    amd_default: Literal["off", "premium"] = Field(default="premium", description="Default AMD mode.")
    background_track_default: Literal["none", "office", "cafe", "restaurant", "phone_static"] = Field(
        default="phone_static", description="Default background track."
    )
    noise_cancellation_default: bool = Field(default=True, description="Default noise cancellation.")
    post_call_eval_setup_id: uuid.UUID | None = Field(default=None, description="Eval run on every call.")
    summary_prompt_default: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = Field(
        default=None, description="Default summary prompt."
    )
    dispositions_default: list[Disposition] = Field(
        default_factory=list, max_length=50, description="Default dispositions."
    )
    low_balance_threshold_micros: int = Field(
        default=5_000_000, ge=0, description="Balance (micro-dollars) that triggers `billing.low_balance`."
    )
    notification_emails: list[Email] = Field(
        default_factory=list, max_length=20, description="Billing and security notifications besides owners."
    )
    data_export_enabled: bool = Field(default=True, description="CSV exports in the dashboard.")
    onboarding_completed_at: AwareDatetime | None = Field(
        default=None, description="Set by the dashboard onboarding wizard."
    )
    delete_carrier_recordings: bool = Field(
        default=True, description="Delete the carrier's recording copy once stored."
    )
    cnam_display_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9 ]{1,15}$")] | None = Field(
        default=None, description="Caller name registered for new numbers."
    )
    batch_concurrency_share: float = Field(
        default=1.0, ge=0.1, le=1.0, description="Share of the org concurrency pool batches may use."
    )
    compliance: ComplianceSettings = Field(
        default_factory=ComplianceSettings, description="Compliance controls."
    )
    privacy: PrivacySettings = Field(default_factory=PrivacySettings, description="Privacy controls.")
    redaction: RedactionSettings = Field(
        default_factory=RedactionSettings, description="Real-time redaction."
    )
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings, description="Post-call analysis.")
    memory: MemorySettings = Field(default_factory=MemorySettings, description="Memory defaults.")
    tools: ToolsSettings = Field(default_factory=ToolsSettings, description="Built-in tools.")
    engine: EngineSettings = Field(default_factory=EngineSettings, description="Dialogue engine.")
    voices: VoicesSettings = Field(default_factory=VoicesSettings, description="Voice resolution.")
    experiments: ExperimentsSettings = Field(
        default_factory=ExperimentsSettings, description="Experiment assignment."
    )
    sms: SmsSettings = Field(default_factory=SmsSettings, description="SMS encoding.")


SETTINGS_GROUPS: Final[frozenset[str]] = frozenset(
    {
        "compliance",
        "llm",
        "privacy",
        "redaction",
        "analysis",
        "memory",
        "tools",
        "engine",
        "voices",
        "experiments",
        "sms",
    }
)

COMPLIANCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "compliance",
        "privacy",
        "redaction",
        "hipaa_mode",
        "baa_signed_at",
        "pii_redaction",
        "pii_redaction_entities",
        "viewers_can_see_pii",
        "mfa_required",
        "session_max_age_days",
        "ip_allowlist",
        "ai_disclosure_required",
        "recording_disclosure_required",
        "honor_opt_out",
        "allow_insecure_webhooks",
        "international_enabled",
        "allowed_countries",
        "blocked_prefixes",
    }
)


def merge_settings(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Apply `patch` (what the client sent) to `stored` (sparse JSON); neither input is mutated.

    A group sent as an object merges into the stored group key by key; other values replace stored ones.
    """
    merged = copy.deepcopy(dict(stored))
    for key, value in patch.items():
        current = merged.get(key)
        if key in SETTINGS_GROUPS and isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = {**current, **copy.deepcopy(dict(value))}
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _paths(patch: Mapping[str, Any]) -> Iterator[str]:
    for key, value in patch.items():
        if key in SETTINGS_GROUPS and isinstance(value, Mapping):
            yield from (f"{key}.{sub}" for sub in value)
        else:
            yield key


def sent_paths(patch: Mapping[str, Any]) -> list[str]:
    """Dotted paths a patch sets: `timezone`, `compliance.calling_hours` (an empty group sets nothing)."""
    return list(_paths(patch))


def needs_compliance(patch: Mapping[str, Any]) -> bool:
    """True when the patch sets any compliance setting (the `org.compliance` permission, specs/04 §7)."""
    return any(path.split(".", 1)[0] in COMPLIANCE_PATHS for path in _paths(patch))


def load_settings(stored: Mapping[str, Any]) -> OrgSettings:
    """Validate stored sparse JSON; unset fields take their defaults."""
    return OrgSettings.model_validate(dict(stored))


def sparse_settings(settings: OrgSettings) -> dict[str, Any]:
    """The JSON to store: explicitly set keys only, with validated (normalized) values."""
    return settings.model_dump(mode="json", exclude_unset=True)


def effective_values(settings: OrgSettings, paths: Iterable[str]) -> dict[str, Any]:
    """The JSON value at each dotted path of `paths` (defaults filled), e.g. for an audit diff.

    Paths come from `sent_paths`: a top-level field or `group.field`.
    """
    dumped = settings.model_dump(mode="json")
    values: dict[str, Any] = {}
    for path in paths:
        value: Any = dumped
        for part in path.split("."):
            value = value[part]
        values[path] = value
    return values


def field_path(loc: tuple[int | str, ...]) -> str:
    """Pydantic error location -> dotted path, e.g. `compliance.calling_hours[0].end`."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part != "[key]":
            path = f"{path}.{part}" if path else part
    return path
