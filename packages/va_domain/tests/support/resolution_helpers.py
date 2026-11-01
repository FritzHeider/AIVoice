"""Builders for `va_domain.call_resolution` tests (fixed ids, refs, clock) shared with the api snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from va_domain.call_resolution import (
    CallResolution,
    PathwayRef,
    PlatformModels,
    ResolvedRefs,
    ResolvedVoice,
    RunnerInputs,
    TaskRunner,
    resolve_call_config,
)
from va_domain.calls import BUILTIN_DISPOSITIONS, SendCallRequest
from va_domain.org import load_settings
from va_domain.voices import VoiceSettings

NOW: Final = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
DESTINATION: Final = "+14155550100"
FROM_NUMBER: Final = "+14155550190"
VOICE_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c01")
PHONE_NUMBER_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c02")
PATHWAY_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c03")
PERSONA_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c04")
AGENT_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c05")
AGENT_VERSION_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c06")
EXPERIMENT_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c07")
ARM_ID: Final = UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6c08")
DIALOGUE: Final = "zai-org/GLM-5.3-Flash"
ROUTER: Final = "zai-org/GLM-5.3-Router"
MODELS: Final = PlatformModels(dialogue=DIALOGUE, router=ROUTER)
VOICE_SETTINGS: Final = VoiceSettings(stability=0.3)
BUILTINS: Final = list(BUILTIN_DISPOSITIONS)
BASE_BODY: Final[Mapping[str, Any]] = {"phone_number": DESTINATION, "task": "Hi"}


def make_refs(**overrides: Any) -> ResolvedRefs:
    values: dict[str, Any] = {
        "phone_number": DESTINATION,
        "from_number": FROM_NUMBER,
        "phone_number_id": PHONE_NUMBER_ID,
        "telnyx_account_id": None,
        "voice": ResolvedVoice(VOICE_ID, "21m00Tcm4TlvDq8ikWAM", VOICE_SETTINGS),
        "models": MODELS,
    }
    values.update(overrides)
    return ResolvedRefs(**values)


def pathway_ref(**overrides: Any) -> PathwayRef:
    values: dict[str, Any] = {"pathway_id": PATHWAY_ID, "version": 3, "start_node_id": "greet"}
    values.update(overrides)
    return PathwayRef(**values)


def resolve(
    body: Mapping[str, Any],
    *,
    org: Mapping[str, Any] | None = None,
    runner: RunnerInputs | None = None,
    refs: Mapping[str, Any] | None = None,
    now: datetime = NOW,
) -> CallResolution:
    """Resolve `body` (the full request) for a sparse org settings object."""
    return resolve_call_config(
        load_settings(org or {}),
        SendCallRequest.model_validate(dict(body)),
        runner or TaskRunner(),
        make_refs(**(refs or {})),
        now,
    )


def dumped(resolution: CallResolution) -> dict[str, Any]:
    config: dict[str, Any] = resolution.config.model_dump(mode="json", by_alias=True)
    return config


def rail(kind: str, **fields: Any) -> dict[str, Any]:
    """A guard rail as the resolved config dumps it."""
    return {"type": kind, "name": None, "description": None, "config": {}, "action": None, **fields}


AI_RAIL: Final = rail("tcpa:ai_disclosure")
RECORDING_RAIL: Final = rail("tcpa:recording_disclosure")
