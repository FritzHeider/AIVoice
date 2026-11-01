"""Inputs and outputs of the pure Send Call resolution (specs/05-calls-api.md §4.3 Decision P05-T03b).

Everything here is loaded beforehand by the API layer (pathway/persona/agent rows, the voice, the caller
number, platform model ids): resolution itself does no I/O and reads no clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from va_domain.calls import SendCallConfig
from va_domain.voices import VoiceSettings

__all__ = [
    "AgentRunner",
    "CallResolution",
    "PathwayRef",
    "PathwayRunner",
    "PersonaRunner",
    "PlatformModels",
    "ResolutionWarning",
    "ResolvedRefs",
    "ResolvedVoice",
    "RunnerInputs",
    "TaskRunner",
]


@dataclass(frozen=True, slots=True)
class PathwayRef:
    """A pathway version already resolved to an integer, with what its `global_config` contributes."""

    pathway_id: UUID
    version: int
    call_defaults: Mapping[str, Any] = field(default_factory=dict)
    error_phrase: str | None = None
    start_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRunner:
    """A free-form prompt (`task`) call: no runner layer."""


@dataclass(frozen=True, slots=True)
class PathwayRunner:
    """A `pathway_id` call; `pathway.call_defaults` is the runner layer."""

    pathway: PathwayRef


@dataclass(frozen=True, slots=True)
class PersonaRunner:
    """A `persona_id` call. `config` is the stored `PersonaConfig` JSON (specs/20 §3.1); `pathway` is the
    effective pathway (the request's `pathway_id` override already applied), `None` for a prompt persona."""

    persona_id: UUID
    version: int | None
    legacy: bool
    config: Mapping[str, Any]
    pathway: PathwayRef | None = None


@dataclass(frozen=True, slots=True)
class AgentRunner:
    """An `agent_id` call: the resolved agent version's `call_settings` and its frozen pathway."""

    agent_id: UUID
    agent_version_id: UUID
    call_settings: Mapping[str, Any]
    pathway: PathwayRef | None = None
    experiment_id: UUID | None = None
    experiment_arm_id: UUID | None = None


RunnerInputs = TaskRunner | PathwayRunner | PersonaRunner | AgentRunner


@dataclass(frozen=True, slots=True)
class ResolvedVoice:
    """The voice row chosen for the call and its stored settings."""

    voice_id: UUID
    provider_voice_id: str
    settings: VoiceSettings


@dataclass(frozen=True, slots=True)
class PlatformModels:
    """`LLM_DIALOGUE_MODEL` and `LLM_ROUTER_MODEL` of the deployment."""

    dialogue: str
    router: str


@dataclass(frozen=True, slots=True)
class ResolvedRefs:
    """Lookups done before resolution: destination (E.164), caller number, voice, models, consent facts."""

    phone_number: str
    from_number: str
    phone_number_id: UUID | None
    telnyx_account_id: UUID | None
    voice: ResolvedVoice
    models: PlatformModels
    all_party_consent: bool = False
    ai_disclosure_state: bool = False


@dataclass(frozen=True, slots=True)
class ResolutionWarning:
    """A non-fatal resolution note carried by the `call.queued` event (`TASK_IGNORED`, `TRANSFER_IGNORED`)."""

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class CallResolution:
    """The resolved configuration and the warnings produced on the way."""

    config: SendCallConfig
    warnings: tuple[ResolutionWarning, ...] = ()
