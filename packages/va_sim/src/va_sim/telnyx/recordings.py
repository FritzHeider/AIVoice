"""FakeTelnyx recordings REST (specs/06-telephony-telnyx.md §2.1 ``delete_recording``; specs/30 §3.2 Decision
P05-T14).

``DELETE /v2/recordings/{id}`` removes a stored recording WAV (any authenticated account: the simulator keeps
recordings in one process-wide map), after which ``/sim/recordings/{id}.wav`` answers 404. An unknown id is a
Telnyx 404 error body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.telnyx.errors import not_found
from va_sim.telnyx.http import authenticated, data
from va_sim.telnyx.store import Account

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["recording_routes"]


def recording_routes(state: SimState) -> list[BaseRoute]:
    """``DELETE /v2/recordings/{recording_id}``."""

    async def delete_recording(request: Request, account: Account) -> Response:
        del account  # every authenticated account may delete: recordings are process-wide in the simulator
        recording_id = request.path_params["recording_id"]
        if state.recordings.pop(recording_id, None) is None:
            return not_found("recording", recording_id)
        return data(
            {
                "id": recording_id,
                "record_type": "recording",
                "status": "deleted",
                "updated_at": iso_z(state.clock.now()),
            }
        )

    return [
        Route(
            "/v2/recordings/{recording_id}", authenticated(state.telnyx, delete_recording), methods=["DELETE"]
        )
    ]
