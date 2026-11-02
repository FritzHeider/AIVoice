"""Completed ``PostCallWorkflow`` steps in ``calls.config.postcall_steps`` (specs/18-post-call-analysis.md §1,
Decision P05-T18b).

:func:`mark_step` appends a step name with one guarded UPDATE in the caller's tenant session, so two
attempts never record a step twice and a concurrent ``calls.config`` write is never lost (the append is
computed inside Postgres). :func:`done_steps` reads the array from a loaded config.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import Text, cast, func, literal, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Call
from va_domain.post_call import POSTCALL_STEPS_KEY

__all__ = ["done_steps", "mark_step"]


def done_steps(config: Mapping[str, object] | None) -> tuple[str, ...]:
    """The completed step names in completion order (anything that is not a string list reads as none)."""
    stored = (config or {}).get(POSTCALL_STEPS_KEY)
    if not isinstance(stored, list):
        return ()
    return tuple(step for step in stored if isinstance(step, str))


async def mark_step(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID, step: str) -> bool:
    """Append ``step`` to the call's completed steps; ``False`` when it was already recorded (or the call
    does not exist)."""
    current = func.coalesce(Call.config[POSTCALL_STEPS_KEY], func.jsonb_build_array())
    entry = func.jsonb_build_array(cast(literal(step), Text))
    steps = current.op("||", return_type=JSONB)(entry)
    statement = (
        update(Call)
        .where(Call.id == call_id, Call.org_id == org_id, ~current.bool_op("@>")(entry))
        .values(
            config=Call.config.op("||", return_type=JSONB)(func.jsonb_build_object(POSTCALL_STEPS_KEY, steps))
        )
        .returning(Call.id)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(statement)).first() is not None
