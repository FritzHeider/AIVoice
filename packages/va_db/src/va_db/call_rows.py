"""The ``calls`` row of a newly created call (specs/03-data-model.md §6), shared by Send Call dispatch
(outbound, specs/05 §5) and the edge's inbound accept (specs/06 §7 Decision P05-T12c).

The runner, voice, number, and request columns come from the resolved
:class:`~va_domain.calls.SendCallConfig`; ``calls.config`` is the config's JSON plus non-config keys of the
creator (``pricing``, ``maintenance_message``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from va_db.models import Call, CallDirection, CallStatus
from va_domain.call_workflow import call_workflow_id
from va_domain.calls import SendCallConfig

__all__ = ["NewCall", "call_row"]


@dataclass(frozen=True, slots=True)
class NewCall:
    """The row facts that differ between an outbound and an inbound call."""

    direction: CallDirection
    status: CallStatus
    queue_status: str
    to_number: str | None
    from_number: str | None
    test_call: bool = False
    credit_hold_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


def call_row(
    call_id: uuid.UUID,
    org_id: uuid.UUID,
    config: SendCallConfig,
    new: NewCall,
    extra_config: Mapping[str, Any],
) -> Call:
    """The unsaved ``calls`` row with ``workflow_id`` = ``call-<call_id>``."""
    return Call(
        id=call_id,
        org_id=org_id,
        direction=new.direction,
        to_number=new.to_number,
        from_number=new.from_number,
        status=new.status,
        queue_status=new.queue_status,
        runner_type=config.runner_type,
        pathway_id=config.pathway_id,
        pathway_version=config.pathway_version,
        persona_id=config.persona_id,
        persona_version=config.persona_version,
        agent_id=config.agent_id,
        agent_version_id=config.agent_version_id,
        experiment_id=config.experiment_id,
        experiment_arm_id=config.experiment_arm_id,
        voice_id=config.voice_id,
        phone_number_id=config.phone_number_id,
        memory_id=config.memory_id,
        contact_id=new.contact_id,
        external_id=config.external_id,
        start_time=config.start_time,
        test_call=new.test_call,
        request_data=config.request_data,
        metadata_=config.metadata,
        config={**config.model_dump(mode="json"), **extra_config},
        credit_hold_id=new.credit_hold_id,
        workflow_id=call_workflow_id(call_id),
    )
