"""Pure Send Call configuration resolution (specs/05-calls-api.md §4.3, Decision P05-T03b).

`merge_layers` tells the API layer which voice, caller number and tools the call needs; once those are
loaded, `resolve_call_config` produces the `SendCallConfig` persisted in `calls.config`.
"""

from va_domain.call_resolution.inputs import (
    AgentRunner,
    CallResolution,
    PathwayRef,
    PathwayRunner,
    PersonaRunner,
    PlatformModels,
    ResolutionWarning,
    ResolvedRefs,
    ResolvedVoice,
    RunnerInputs,
    TaskRunner,
)
from va_domain.call_resolution.layers import (
    CallDefaults,
    check_call_defaults,
    org_layer,
    parse_layer,
    pathway_call_defaults,
    persona_layer,
)
from va_domain.call_resolution.merge import MergedParameters, merge_layers
from va_domain.call_resolution.resolve import MAX_SCHEDULE_AHEAD, resolve_call_config

__all__ = [
    "MAX_SCHEDULE_AHEAD",
    "AgentRunner",
    "CallDefaults",
    "CallResolution",
    "MergedParameters",
    "PathwayRef",
    "PathwayRunner",
    "PersonaRunner",
    "PlatformModels",
    "ResolutionWarning",
    "ResolvedRefs",
    "ResolvedVoice",
    "RunnerInputs",
    "TaskRunner",
    "check_call_defaults",
    "merge_layers",
    "org_layer",
    "parse_layer",
    "pathway_call_defaults",
    "persona_layer",
    "resolve_call_config",
]
