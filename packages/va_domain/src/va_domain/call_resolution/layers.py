"""Config layers below the request (specs/05-calls-api.md §4.3): org defaults, pathway `call_defaults`,
persona config, agent `call_settings`, and `persona_settings`.

Each layer is parsed with the Send Call parameter model and reduced to the parameters it actually sets
(`CallParameters.explicit_fields`, aliases folded). An invalid layer raises `CallRequestError`
(`INVALID_INPUT`) whose `field` names the layer source, e.g. `call_defaults.record`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import ConfigDict, Field, ValidationError

from va_domain.calls import CallParameters, CallRequestError, GuardRail, PostCallEvals
from va_domain.calls.parts import WEBHOOK_EVENTS
from va_domain.languages import lookup_language
from va_domain.org import OrgSettings, field_path

__all__ = [
    "DERIVED_FIELDS",
    "REQUEST_ONLY_FIELDS",
    "CallDefaults",
    "RunnerLayer",
    "check_call_defaults",
    "org_layer",
    "parse_layer",
    "pathway_call_defaults",
    "persona_layer",
    "persona_prompt",
]

# §4.4 "derived" fields an org/pathway/agent layer may set although they are not request parameters.
DERIVED_FIELDS: Final = ("no_input_timeout_ms", "max_reprompts", "greeting_timeout_ms")
# Parameters that only make sense per request: a default would schedule, simulate, or mark every call.
REQUEST_ONLY_FIELDS: Final = frozenset({"start_time", "test_call", "test_mode"})
_REQUEST_ONLY_MESSAGE: Final = "{name} can only be set in the Send Call request body."
_RETRY_WAIT_MESSAGE: Final = "retry.wait is required."
_BACKGROUND_TRACKS: Final[Mapping[str, str | None]] = {
    "phone_static": None,
    "none": "none",
    "office": "office",
    "cafe": "cafe",
    "restaurant": "restaurant",
}
# GlobalConfig keys (specs/11 §3.4) that seed the pathway layer under a call parameter name.
_PATHWAY_GLOBALS: Final[Mapping[str, str]] = {
    "defaultVoice": "voice",
    "defaultLanguage": "language",
    "defaultTemperature": "temperature",
}
_PATHWAY_DEFAULTS_MESSAGE: Final = "call_defaults must be an object."
# PersonaConfig keys that are call parameters under the same name (specs/20 §3.1).
_PERSONA_DIRECT: Final = (
    "first_sentence",
    "voice",
    "voice_settings",
    "language",
    "tools",
    "knowledge_base_ids",
    "guard_rails",
    "dispositions",
    "summary_prompt",
    "analysis_schema",
    "post_call_evals",
    "webhook",
    "webhook_events",
    "metadata",
)


class CallDefaults(CallParameters):
    """A lower config layer: Send Call parameters plus the derived §4.4 fields; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    no_input_timeout_ms: int | None = Field(default=None, ge=3000, le=30_000)
    max_reprompts: int | None = Field(default=None, ge=0, le=5)
    greeting_timeout_ms: int | None = Field(default=None, ge=1000, le=30_000)


class RunnerLayer(CallDefaults):
    """Agent `call_settings`: a lower layer that may also carry the agent's `task` prompt."""

    task: str | None = Field(default=None, min_length=1, max_length=16_000)


def _invalid(message: str, field: str) -> CallRequestError:
    return CallRequestError("INVALID_INPUT", message, field)


def _source_path(loc: tuple[int | str, ...], prefix: str, origins: Mapping[str, str]) -> str:
    path = field_path(loc)
    head, dot, rest = path.partition(".")
    bracket = head.find("[")
    name, suffix = (head, "") if bracket < 0 else (head[:bracket], head[bracket:])
    source = origins.get(name, name)
    return f"{prefix}.{source}{suffix}{dot}{rest}" if path else prefix


def parse_layer(
    raw: Mapping[str, Any],
    prefix: str,
    *,
    model: type[CallDefaults] = CallDefaults,
    origins: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Parameter name -> value for what the layer sets (aliases folded, derived fields included).

    `prefix` names the layer in error fields; `origins` maps a parameter back to its key in the source
    object when the two differ (persona `memory.enabled` -> `memory_enabled`).
    """
    names = origins or {}
    for name in raw:
        if name in REQUEST_ONLY_FIELDS:
            raise _invalid(_REQUEST_ONLY_MESSAGE.format(name=name), _source_path((name,), prefix, names))
    try:
        layer = model.model_validate(dict(raw))
    except ValidationError as exc:
        error = exc.errors()[0]
        raise _invalid(str(error["msg"]), _source_path(tuple(error["loc"]), prefix, names)) from exc
    if layer.retry is not None and layer.retry.wait is None:
        raise _invalid(_RETRY_WAIT_MESSAGE, _source_path(("retry", "wait"), prefix, names))
    values = layer.explicit_fields()
    extra = [*DERIVED_FIELDS, "task"] if isinstance(layer, RunnerLayer) else list(DERIVED_FIELDS)
    values.update({name: getattr(layer, name) for name in extra if name in layer.model_fields_set})
    return values


def check_call_defaults(raw: Mapping[str, Any]) -> None:
    """Validate an org `call_defaults` object before `PATCH /v1/org/settings` stores it."""
    parse_layer(raw, "call_defaults")


def _org_language(org: OrgSettings) -> str:
    if lookup_language(org.default_language) is None:
        msg = f"unsupported language {org.default_language!r}"
        raise _invalid(msg, "default_language")
    return org.default_language


def _org_webhook_events(org: OrgSettings) -> list[str]:
    for index, name in enumerate(org.default_webhook_events):
        if name not in WEBHOOK_EVENTS:
            msg = f"unknown webhook event {name!r}"
            raise _invalid(msg, f"default_webhook_events[{index}]")
    return list(org.default_webhook_events)


def _org_guard_rails(org: OrgSettings) -> list[GuardRail]:
    rails: list[GuardRail] = []
    for index, item in enumerate(org.default_guard_rails):
        try:
            rails.append(GuardRail.model_validate(item.model_dump(mode="json")))
        except ValidationError as exc:
            error = exc.errors()[0]
            path = field_path(tuple(error["loc"]))
            raise _invalid(
                str(error["msg"]), f"default_guard_rails[{index}]{'.' + path if path else ''}"
            ) from exc
    return rails


def _mapped_org_defaults(org: OrgSettings) -> dict[str, Any]:
    """The explicitly set `default_*` settings that map one-to-one onto a call parameter."""
    sent = org.model_fields_set
    mapped = {
        "default_record": ("record", org.default_record),
        "default_max_duration_min": ("max_duration", float(org.default_max_duration_min)),
        "default_webhook": ("webhook", org.default_webhook),
        "timezone": ("timezone", org.timezone),
        "default_country": ("country", org.default_country),
        "summary_prompt_default": ("summary_prompt", org.summary_prompt_default),
        "dispositions_default": ("dispositions", list(org.dispositions_default)),
        "noise_cancellation_default": ("noise_cancellation", org.noise_cancellation_default),
        "background_track_default": ("background_track", _BACKGROUND_TRACKS[org.background_track_default]),
    }
    values = {name: value for setting, (name, value) in mapped.items() if setting in sent}
    if "default_language" in sent:
        values["language"] = _org_language(org)
    if "default_webhook_events" in sent:
        values["webhook_events"] = _org_webhook_events(org)
    return values


def _derived_org_defaults(org: OrgSettings) -> dict[str, Any]:
    """Org settings that imply a call parameter (AMD mode, memory, temperature, evals, guard rails)."""
    values: dict[str, Any] = {}
    if "amd_default" in org.model_fields_set and org.amd_default == "off":
        values["answered_by_enabled"] = False
    if "default_enabled" in org.memory.model_fields_set:
        values["memory_enabled"] = org.memory.default_enabled
    if org.llm.temperature_default is not None:
        values["temperature"] = org.llm.temperature_default
    if org.post_call_eval_setup_id is not None:
        values["post_call_evals"] = PostCallEvals(workbench_setup_id=org.post_call_eval_setup_id)
    if org.default_guard_rails:
        values["guard_rails"] = _org_guard_rails(org)
    return values


def org_layer(org: OrgSettings) -> dict[str, Any]:
    """The org layer: explicitly set `default_*` settings, then `call_defaults` over them.

    `default_voice`, `default_voice_id` and `default_from_number` are not layered: the caller applies them
    when no layer chose a voice / caller number (they need a database lookup).
    """
    return {
        **_mapped_org_defaults(org),
        **_derived_org_defaults(org),
        **parse_layer(org.call_defaults, "call_defaults"),
    }


def pathway_call_defaults(global_config: Mapping[str, Any]) -> dict[str, Any]:
    """The raw pathway layer of a version's `global_config`: `defaultVoice`/`defaultLanguage`/
    `defaultTemperature` (when not null) as `voice`/`language`/`temperature`, then `call_defaults` over them.

    Values are validated later with the other layers (`merge_layers`, prefix `pathway.call_defaults`); a
    `call_defaults` that is present but not an object raises `INVALID_INPUT` here.
    """
    values = {
        name: global_config[key]
        for key, name in _PATHWAY_GLOBALS.items()
        if global_config.get(key) is not None
    }
    defaults = global_config.get("call_defaults")
    if defaults is None:
        return values
    if not isinstance(defaults, Mapping):
        raise _invalid(_PATHWAY_DEFAULTS_MESSAGE, "pathway.call_defaults")
    return {**values, **defaults}


def _is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, list | dict) and not value)


def persona_layer(config: Mapping[str, Any]) -> dict[str, Any]:
    """The persona config (specs/20 §3.1) as call parameters: keys set to `null` or an empty list/object are
    not set; `prompt` is not a parameter (see `persona_prompt`)."""
    raw: dict[str, Any] = {}
    origins: dict[str, str] = {}
    settings = config.get("call_settings")
    if isinstance(settings, Mapping):
        for name, value in settings.items():
            if not _is_unset(value):
                raw[name] = value
                origins[name] = f"call_settings.{name}"
    for name in _PERSONA_DIRECT:
        value = config.get(name)
        if not _is_unset(value):
            raw[name] = value
    memory = config.get("memory")
    if isinstance(memory, Mapping) and memory.get("enabled") is not None:
        raw["memory_enabled"] = memory["enabled"]
        origins["memory_enabled"] = "memory.enabled"
    return parse_layer(raw, "persona.config", origins=origins)


def persona_prompt(config: Mapping[str, Any]) -> str | None:
    """The persona's prompt (`task` of a prompt persona), stripped; `None` when absent or blank."""
    prompt = config.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return prompt.strip()
