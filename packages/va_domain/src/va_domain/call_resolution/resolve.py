"""`resolve_call_config`: merged layers + loaded references -> `SendCallConfig` (specs/05-calls-api.md §4.3,
§4.4; Decision P05-T03b). Pure: no I/O, the clock comes in as `now`."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from va_domain.call_resolution.guard_rails import with_forced_rails
from va_domain.call_resolution.inputs import (
    AgentRunner,
    CallResolution,
    PersonaRunner,
    PlatformModels,
    ResolvedRefs,
    RunnerInputs,
)
from va_domain.call_resolution.merge import MergedParameters, merge_layers
from va_domain.calls import (
    BUILTIN_DISPOSITIONS,
    DEFAULT_ERROR_PHRASE,
    CallRequestError,
    InlineTool,
    SendCallConfig,
    SendCallRequest,
)
from va_domain.org import OrgSettings
from va_domain.voices import VoiceSettings

__all__ = ["MAX_SCHEDULE_AHEAD", "resolve_call_config"]

MAX_SCHEDULE_AHEAD: Final = timedelta(days=90)
_LEGACY_DIALOGUE: Final = frozenset({"base", "enhanced"})
_LEGACY_ROUTER: Final = "turbo"
# Merged parameter -> `SendCallConfig` field of the same meaning.
_COPIED: Final[Mapping[str, str]] = {
    name: name
    for name in (
        "first_sentence",
        "wait_for_greeting",
        "language",
        "temperature",
        "max_tokens",
        "pronunciation_guide",
        "interruptibility",
        "block_interruptions",
        "noise_cancellation",
        "background_track",
        "hd_audio",
        "country",
        "dialing_strategy",
        "timezone",
        "transfer_phone_number",
        "transfer_list",
        "memory_id",
        "memory_enabled",
        "ivr_mode",
        "answered_by_enabled",
        "summary_prompt",
        "analysis_schema",
        "analysis_prompt",
        "citation_schema_ids",
        "post_call_evals",
        "stream_post_transfer_transcript",
        "request_data",
        "metadata",
        "external_id",
        "webhook",
        "webhook_events",
        "dynamic_data",
        "keywords",
        "ignore_button_press",
        "precall_dtmf_sequence",
        "test_mode",
        "no_input_timeout_ms",
        "max_reprompts",
        "greeting_timeout_ms",
    )
} | {"interruption_threshold": "interruption_threshold_ms", "max_duration": "max_duration_min"}
_START_TIME_PAST: Final = "start_time must be in the future."
_START_TIME_FAR: Final = "start_time must be at most 90 days ahead."


def _invalid(message: str, field: str) -> CallRequestError:
    return CallRequestError("INVALID_INPUT", message, field)


def _models(org: OrgSettings, models: PlatformModels, requested: str) -> tuple[str, str]:
    """(dialogue_model, router_model) for the request `model` value."""
    router = org.llm.router_model or models.router
    if requested in _LEGACY_DIALOGUE:
        return org.llm.dialogue_model or models.dialogue, router
    if requested == _LEGACY_ROUTER:
        return router, router
    allowed = {models.dialogue, models.router}
    allowed.update(
        name
        for name in (
            org.llm.dialogue_model,
            org.llm.router_model,
            org.llm.analysis_model,
            org.llm.fallback_model,
        )
        if name is not None
    )
    if requested not in allowed and not org.llm.allow_model_override:
        msg = f"Model {requested!r} is not allowed for this organization."
        raise _invalid(msg, "model")
    return requested, router


def _voice_settings(org: OrgSettings, refs: ResolvedRefs, patch: Mapping[str, Any]) -> VoiceSettings:
    base = org.voice_overrides.get(refs.voice.voice_id, refs.voice.settings)
    sent = {name: value for name, value in patch.items() if value is not None}
    return base.model_copy(update=sent)


def _start_time(value: datetime, timezone: str, now: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=ZoneInfo(timezone))
    moment = aware.astimezone(UTC)
    if moment <= now:
        raise _invalid(_START_TIME_PAST, "start_time")
    if moment > now + MAX_SCHEDULE_AHEAD:
        raise _invalid(_START_TIME_FAR, "start_time")
    return moment


def _tools(tools: list[str | InlineTool]) -> dict[str, Any]:
    tool_ids: list[UUID] = []
    kb_ids: list[UUID] = []
    inline: list[InlineTool] = []
    for tool in tools:
        if isinstance(tool, InlineTool):
            inline.append(tool)
            continue
        prefix, _, ident = tool.partition("-")
        (kb_ids if prefix == "KB" else tool_ids).append(UUID(ident))
    return {"tool_ids": tool_ids, "knowledge_base_ids": kb_ids, "inline_tools": inline}


def _dispositions(values: Mapping[str, Any]) -> list[str]:
    configured = list(values.get("dispositions") or [])
    return configured + [name for name in BUILTIN_DISPOSITIONS if name not in configured]


def _voicemail(values: Mapping[str, Any]) -> dict[str, Any]:
    voicemail = dict(values.get("voicemail") or {})
    if "voicemail_detect_timeout_ms" in values:
        voicemail["detect_timeout_ms"] = values["voicemail_detect_timeout_ms"]
    if values.get("ivr_mode"):
        voicemail.update(action="ignore", sensitive=False)
    return voicemail


def _runner_fields(
    merged: MergedParameters, request: SendCallRequest, runner: RunnerInputs
) -> dict[str, Any]:
    fields: dict[str, Any] = {"runner_type": request.runner_type, "task": merged.task}
    pathway = merged.pathway
    if pathway is not None:
        fields.update(
            pathway_id=pathway.pathway_id,
            pathway_version=pathway.version,
            start_node_id=request.start_node_id or pathway.start_node_id,
            error_phrase=pathway.error_phrase or DEFAULT_ERROR_PHRASE,
        )
    if isinstance(runner, PersonaRunner):
        fields.update(persona_id=runner.persona_id, persona_version=runner.version)
    if isinstance(runner, AgentRunner):
        fields.update(
            agent_id=runner.agent_id,
            agent_version_id=runner.agent_version_id,
            experiment_id=runner.experiment_id,
            experiment_arm_id=runner.experiment_arm_id,
        )
    return fields


def _parameter_fields(
    org: OrgSettings, merged: MergedParameters, refs: ResolvedRefs, now: datetime
) -> dict[str, Any]:
    values = merged.values
    fields = {target: values[name] for name, target in _COPIED.items() if name in values}
    fields["dialogue_model"], fields["router_model"] = _models(org, refs.models, values.get("model", "base"))
    fields["voice_settings"] = _voice_settings(org, refs, values.get("voice_settings", {}))
    fields.update(_tools(merged.tools))
    fields["voicemail"] = _voicemail(values)
    fields["dispositions"] = _dispositions(values)
    if "warm_transfer" in values:
        fields["warm_transfer"] = values["warm_transfer"]
    if "retry" in values:
        fields["retry"] = values["retry"]
    if values.get("start_time") is not None:
        fields["start_time"] = _start_time(values["start_time"], merged.timezone, now)
    record = bool(values.get("record", False))
    fields["record"] = (
        record or fields.get("post_call_evals") is not None or bool(fields.get("citation_schema_ids"))
    )
    fields["guard_rails"] = with_forced_rails(
        list(values.get("guard_rails", [])),
        org,
        record=fields["record"],
        all_party_consent=refs.all_party_consent,
        ai_disclosure_state=refs.ai_disclosure_state,
    )
    return fields


def resolve_call_config(
    org: OrgSettings,
    request: SendCallRequest,
    runner: RunnerInputs,
    refs: ResolvedRefs,
    now: datetime,
) -> CallResolution:
    """The `SendCallConfig` a call runs with: platform defaults -> org -> runner layers -> request (§4.3).

    `org` must come from `load_settings` (explicitly set fields are layered, defaults are not); `runner`
    must match `request.runner_type`; `now` must be timezone-aware. Rule failures raise
    `CallRequestError` (`INVALID_INPUT` with `field`).
    """
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    merged = merge_layers(org, request, runner)
    fields = _parameter_fields(org, merged, refs, now.astimezone(UTC))
    fields.update(_runner_fields(merged, request, runner))
    fields.update(
        phone_number=refs.phone_number,
        from_number=refs.from_number,
        phone_number_id=refs.phone_number_id,
        telnyx_account_id=refs.telnyx_account_id,
        voice_id=refs.voice.voice_id,
        provider_voice_id=refs.voice.provider_voice_id,
    )
    return CallResolution(config=SendCallConfig.model_validate(fields), warnings=merged.warnings)
