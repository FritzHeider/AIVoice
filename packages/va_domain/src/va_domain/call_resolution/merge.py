"""Layer merging (specs/05-calls-api.md §4.3): which layers apply for a runner, in which order, and how
each parameter combines across them."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel

from va_domain.call_resolution.inputs import (
    AgentRunner,
    PathwayRef,
    PathwayRunner,
    PersonaRunner,
    ResolutionWarning,
    RunnerInputs,
    TaskRunner,
)
from va_domain.call_resolution.layers import (
    RunnerLayer,
    org_layer,
    parse_layer,
    persona_layer,
    persona_prompt,
)
from va_domain.calls import DialingStrategy, GuardRail, InlineTool, SendCallRequest
from va_domain.org import OrgSettings

__all__ = ["MergedParameters", "merge_layers", "merge_values"]

# Objects merged key by key: a layer overrides only the keys it sent.
_KEYED_OBJECTS: Final = frozenset({"voice_settings", "voicemail", "warm_transfer", "retry"})
_TRANSFER_FIELDS: Final = ("transfer_phone_number", "transfer_list")
_REPLACED_BY_REQUEST: Final = ("request_data", "metadata")
_RUNNER_TYPES: Final[Mapping[type, str]] = {
    TaskRunner: "task",
    PathwayRunner: "pathway",
    PersonaRunner: "persona",
    AgentRunner: "agent",
}


@dataclass(frozen=True, slots=True)
class MergedParameters:
    """Every parameter set by some layer (highest layer applied), plus the runner facts derived on the way.

    The API layer reads `voice`, `from_`, `dialing_strategy`, `tools`, `country` to know what to load
    before calling `resolve_call_config`.
    """

    values: Mapping[str, Any]
    task: str | None
    pathway: PathwayRef | None
    warnings: tuple[ResolutionWarning, ...] = field(default_factory=tuple)

    @property
    def voice(self) -> str | None:
        value = self.values.get("voice")
        return value if isinstance(value, str) else None

    @property
    def from_(self) -> str | None:
        value = self.values.get("from")
        return value if isinstance(value, str) else None

    @property
    def dialing_strategy(self) -> DialingStrategy:
        value = self.values.get("dialing_strategy")
        return value if isinstance(value, DialingStrategy) else DialingStrategy()

    @property
    def tools(self) -> list[str | InlineTool]:
        return list(self.values.get("tools", []))

    @property
    def country(self) -> str:
        return str(self.values.get("country", "US"))

    @property
    def timezone(self) -> str:
        return str(self.values.get("timezone", "America/Los_Angeles"))

    @property
    def language(self) -> str:
        return str(self.values.get("language", "babel-en"))


def _patch(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in value.model_fields_set}
    return dict(value)


def _tool_key(tool: str | InlineTool) -> tuple[str, str]:
    if isinstance(tool, InlineTool):
        return ("inline", tool.name)
    prefix, _, ident = tool.partition("-")
    return (prefix, str(UUID(ident)))


def _union_tools(lower: list[str | InlineTool], higher: list[str | InlineTool]) -> list[str | InlineTool]:
    merged: dict[tuple[str, str], str | InlineTool] = {_tool_key(tool): tool for tool in lower}
    for tool in higher:
        key = _tool_key(tool)
        if key not in merged or isinstance(tool, InlineTool):
            merged[key] = tool
    return list(merged.values())


def _union_guard_rails(lower: list[GuardRail], higher: list[GuardRail]) -> list[GuardRail]:
    merged = {(rail.type, rail.name): rail for rail in lower}
    merged.update({(rail.type, rail.name): rail for rail in higher})
    return list(merged.values())


def _merge_maps(lower: dict[str, Any], higher: dict[str, Any]) -> dict[str, Any]:
    return {**lower, **higher}


# Parameters whose value combines with the lower layer's instead of replacing it.
_COMBINERS: Final[Mapping[str, Callable[[Any, Any], Any]]] = {
    "transfer_list": _merge_maps,
    "analysis_schema": _merge_maps,
    "tools": _union_tools,
    "guard_rails": _union_guard_rails,
}


def _merged_value(name: str, current: Any, value: Any) -> Any:
    if name in _KEYED_OBJECTS:
        return {**(current or {}), **_patch(value)}
    combine = _COMBINERS.get(name)
    if combine is None or current is None or value is None:
        return value
    return combine(current, value)


def merge_values(layers: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge layers given lowest first: scalars, arrays, `request_data`/`metadata`, `dialing_strategy` and
    `post_call_evals` are replaced; `tools` and `guard_rails` are unions (higher entry wins in place);
    `voice_settings`/`voicemail`/`warm_transfer`/`retry` merge per sent key (`null` changes nothing) and
    `transfer_list`/`analysis_schema` per entry."""
    merged: dict[str, Any] = {}
    for layer in layers:
        for name, value in layer.items():
            if name in _KEYED_OBJECTS and value is None:
                continue
            merged[name] = _merged_value(name, merged.get(name), value)
    return merged


def _check_runner(request: SendCallRequest, runner: RunnerInputs) -> None:
    expected = _RUNNER_TYPES[type(runner)]
    if request.runner_type != expected:
        msg = f"runner inputs for {expected!r} given for a {request.runner_type!r} request"
        raise ValueError(msg)


def _pathway_of(runner: RunnerInputs) -> PathwayRef | None:
    if isinstance(runner, TaskRunner):
        return None
    return runner.pathway


def _pathway_defaults(pathway: PathwayRef | None) -> list[dict[str, Any]]:
    if pathway is None:
        return []
    return [parse_layer(pathway.call_defaults, "pathway.call_defaults")]


def _task_ignored(request: SendCallRequest) -> ResolutionWarning | None:
    if request.task is None:
        return None
    return ResolutionWarning("TASK_IGNORED", "task is ignored because the call runs a pathway.", "task")


def _persona_layers(
    request: SendCallRequest, runner: PersonaRunner, base: list[dict[str, Any]], explicit: dict[str, Any]
) -> list[dict[str, Any]]:
    persona = persona_layer(runner.config)
    settings = parse_layer(request.persona_settings, "persona_settings")
    if runner.legacy:
        return [*base, persona, explicit, settings]
    replaced: dict[str, Any] = {name: explicit[name] for name in _REPLACED_BY_REQUEST if name in explicit}
    return [*base, explicit, persona, replaced, settings]


def _runner_task(
    request: SendCallRequest, runner: RunnerInputs, pathway: PathwayRef | None, agent_task: str | None
) -> tuple[str | None, list[ResolutionWarning]]:
    if pathway is not None:
        warning = _task_ignored(request)
        return None, [warning] if warning is not None else []
    if isinstance(runner, PersonaRunner):
        return request.task or persona_prompt(runner.config), []
    if isinstance(runner, AgentRunner):
        return agent_task, []
    return request.task, []


def _drop_transfers(values: dict[str, Any]) -> list[ResolutionWarning]:
    warnings: list[ResolutionWarning] = []
    for name in _TRANSFER_FIELDS:
        if name in values:
            del values[name]
            message = f"{name} is ignored for pathway runners; Transfer nodes define the targets."
            warnings.append(ResolutionWarning("TRANSFER_IGNORED", message, name))
    return warnings


def merge_layers(org: OrgSettings, request: SendCallRequest, runner: RunnerInputs) -> MergedParameters:
    """Apply org defaults, the runner layers, and the request in the §4.3 order for `runner`.

    `runner` must match `request.runner_type` (`ValueError` otherwise). An invalid layer raises
    `CallRequestError` (`INVALID_INPUT`).
    """
    _check_runner(request, runner)
    pathway = _pathway_of(runner)
    base = [org_layer(org), *_pathway_defaults(pathway)]
    explicit = request.explicit_fields()
    agent_task: str | None = None
    if isinstance(runner, PersonaRunner):
        layers = _persona_layers(request, runner, base, explicit)
    elif isinstance(runner, AgentRunner):
        settings = parse_layer(runner.call_settings, "agent.call_settings", model=RunnerLayer)
        agent_task = settings.pop("task", None)
        layers = [*base, settings, explicit]
    else:
        layers = [*base, explicit]
    values = merge_values(layers)
    task, warnings = _runner_task(request, runner, pathway, agent_task)
    if pathway is not None:
        warnings.extend(_drop_transfers(values))
    return MergedParameters(values=values, task=task, pathway=pathway, warnings=tuple(warnings))
