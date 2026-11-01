"""The call an activity works on, read in the caller's tenant session."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Call
from va_db.repositories import CallRepo, NotFoundError
from va_workflows.activities.errors import call_not_found
from va_workflows.activities.inputs import CallActivityInput

__all__ = ["load_call"]


async def load_call(session: AsyncSession, request: CallActivityInput) -> Call:
    """The call of ``request``; a non-retryable ``CallNotFound`` when absent or another org's."""
    try:
        return await CallRepo(session, request.org_id).get(request.call_id)
    except NotFoundError:
        raise call_not_found() from None
