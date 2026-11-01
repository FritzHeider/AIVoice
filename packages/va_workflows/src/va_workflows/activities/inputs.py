"""Inputs shared by the call activities. Workflows import this module, so it must stay free of I/O imports.

Activities take and return pydantic models; the worker's Temporal client uses
``temporalio.contrib.pydantic.pydantic_data_converter`` so they cross the wire as JSON.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["CallActivityInput"]


class CallActivityInput(BaseModel):
    """The call an activity works on; ``org_id`` scopes every database read to the call's tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: UUID
    org_id: UUID
