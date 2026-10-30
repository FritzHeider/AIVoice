"""Everything FakeTelnyx serves beyond the shared ``/sim/*`` control API.

Spec: specs/30-testing-and-evals.md §3.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import BaseRoute

from va_sim.telnyx.call_control import call_control_routes
from va_sim.telnyx.call_routes import call_routes
from va_sim.telnyx.conferences import conference_routes
from va_sim.telnyx.control import telnyx_control_routes
from va_sim.telnyx.messages import message_routes
from va_sim.telnyx.numbers import number_routes
from va_sim.telnyx.resources import resource_routes

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["telnyx_routes"]


def telnyx_routes(state: SimState) -> list[BaseRoute]:
    """The ``/v2`` REST API and the ``/sim/telnyx/*`` control routes."""
    return [
        *resource_routes(state),
        *number_routes(state),
        *message_routes(state),
        *call_routes(state),
        *conference_routes(state),
        *telnyx_control_routes(state),
        *call_control_routes(state),
    ]
