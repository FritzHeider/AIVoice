"""Inbound number configuration (specs/16-phone-numbers.md §4, Decision P05-T12a).

`InboundConfig` is stored in `phone_numbers.inbound_config`. It parses with the Send Call parameter model
(`CallParameters`) plus the inbound runner fields and the routing-only fields; unknown keys are errors.
Only the keys that were set reach the inbound call's request layer (`inbound_request`), so unset fields
keep the lower layers' defaults (org settings, Send Call defaults).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from va_domain.calls import CallParameters
from va_domain.calls.request import AGENT_VERSION_PATTERN
from va_domain.org import field_path
from va_domain.phone import InvalidPhoneNumberError, normalize_e164

__all__ = [
    "MAINTENANCE_MESSAGE_KEY",
    "OUTBOUND_ONLY_FIELDS",
    "ROUTING_FIELDS",
    "InboundConfig",
    "InboundConfigError",
    "Maintenance",
    "load_inbound_config",
    "maintenance_active",
]

MAINTENANCE_MESSAGE_KEY: Final = "maintenance_message"
"""The ``calls.config`` key (not a call configuration field, like ``pricing``) holding the maintenance message
of an inbound call accepted only to play it (specs/16 §4, specs/06 §7 Decision P05-T12c)."""

# Inbound-only keys: number routing (specs/06 §7), never part of the call request.
ROUTING_FIELDS: Final = frozenset(
    {
        "fallback_number",
        "maintenance",
        "allowed_callers",
        "blocked_callers",
        "block_dnc_callers",
        "byo_original_connection_id",
    }
)
# Send Call parameters that cannot apply to a call someone else placed: the caller ID is the called number,
# the caller's region comes from the caller, and nothing is scheduled, retried, simulated, or pre-dialed.
OUTBOUND_ONLY_FIELDS: Final = frozenset(
    {
        "from",
        "from_",
        "dialing_strategy",
        "local_dialing",
        "geospatial_dialing",
        "country",
        "start_time",
        "retry",
        "precall_dtmf_sequence",
        "external_id",
        "test_call",
        "test_mode",
    }
)
MAX_CALLER_LIST: Final = 1000
_OUTBOUND_ONLY_MESSAGE: Final = "{name} cannot be set on an inbound number."
_RUNNER_MESSAGE: Final = "At most one of prompt, pathway_id, agent_id may be set."
_PERSONA_MESSAGE: Final = "persona_id may only be combined with pathway_id."
_WINDOW_MESSAGE: Final = "maintenance.starts_at must not be after ends_at."
_E164_MESSAGE: Final = "must be a valid phone number in E.164 format"
_NAIVE_MESSAGE: Final = "now must be timezone-aware"


def _dialable_e164(value: str) -> str:
    try:
        normalized = normalize_e164(value)
    except InvalidPhoneNumberError as exc:
        raise ValueError(_E164_MESSAGE) from exc
    if normalized != value:
        raise ValueError(_E164_MESSAGE)
    return value


DialableE164 = Annotated[str, AfterValidator(_dialable_e164)]
CallerList = Annotated[list[DialableE164], Field(max_length=MAX_CALLER_LIST)]


class InboundConfigError(ValueError):
    """An invalid stored `inbound_config`; `field` is the dotted path (`None` = the whole object)."""

    def __init__(self, message: str, field: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class Maintenance(BaseModel):
    """A maintenance window: active when `enabled` and now is in [starts_at, ends_at] (open-ended bounds)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    message: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.starts_at is not None and self.ends_at is not None and self.starts_at > self.ends_at:
            raise ValueError(_WINDOW_MESSAGE)
        return self


def maintenance_active(maintenance: Maintenance | None, now: datetime) -> bool:
    """Whether `maintenance` applies at `now` (both bounds inclusive)."""
    if now.tzinfo is None:
        raise ValueError(_NAIVE_MESSAGE)
    if maintenance is None or not maintenance.enabled:
        return False
    if maintenance.starts_at is not None and now < maintenance.starts_at:
        return False
    return maintenance.ends_at is None or now <= maintenance.ends_at


class InboundConfig(CallParameters):
    """Every specs/16 §4 field: Send Call parameters (minus `OUTBOUND_ONLY_FIELDS`), the inbound runner,
    and the routing-only fields (`ROUTING_FIELDS`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    # runner (§4 runner rule)
    prompt: str | None = Field(
        default=None, min_length=1, max_length=16_000, description="Same as Send Call `task`."
    )
    pathway_id: UUID | None = None
    pathway_version: Annotated[StrictInt, Field(ge=0)] | Literal["production", "staging"] | None = None
    persona_id: UUID | None = None
    persona_version: Literal["production", "draft"] | None = None
    agent_id: UUID | None = None
    agent_version: Annotated[str, StringConstraints(pattern=AGENT_VERSION_PATTERN)] | None = None
    # §4 narrows the Send Call range (0.5-240).
    max_duration: float = Field(default=30, ge=1, le=240, description="Hard limit in minutes.")
    # routing (specs/06 §7)
    fallback_number: DialableE164 | None = None
    maintenance: Maintenance = Field(default_factory=Maintenance)
    allowed_callers: CallerList = Field(default_factory=list)
    blocked_callers: CallerList = Field(default_factory=list)
    block_dnc_callers: bool = False
    byo_original_connection_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _inbound_keys(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            for name in data:
                if name in OUTBOUND_ONLY_FIELDS:
                    raise ValueError(_OUTBOUND_ONLY_MESSAGE.format(name=name))
        return data

    @model_validator(mode="after")
    def _runner_rule(self) -> Self:
        runners = [value for value in (self.prompt, self.pathway_id, self.agent_id) if value is not None]
        if len(runners) > 1:
            raise ValueError(_RUNNER_MESSAGE)
        if self.persona_id is not None and (self.prompt is not None or self.agent_id is not None):
            raise ValueError(_PERSONA_MESSAGE)
        return self


def load_inbound_config(stored: Mapping[str, Any]) -> InboundConfig:
    """Validate a stored `phone_numbers.inbound_config` (raises `InboundConfigError`)."""
    try:
        return InboundConfig.model_validate(dict(stored))
    except ValidationError as exc:
        error = exc.errors()[0]
        path = field_path(tuple(error["loc"]))
        raise InboundConfigError(str(error["msg"]), path or None) from exc
