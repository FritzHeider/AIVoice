"""FakeTelnyx call control API for tests (specs/30-testing-and-evals.md §3.2 Control API, Decision 2026-10-29
P03-T02b rules 13 and 14).

``POST /sim/telnyx/inbound`` places a call to an owned number; ``GET /sim/telnyx/calls`` and ``GET
/sim/telnyx/calls/{ccid}`` show call summaries (command log, events sent, DTMF, transfers); ``GET
/sim/telnyx/calls/{ccid}/timeline`` reports sim-clock milestones; ``POST /sim/telnyx/calls/{ccid}/hangup``
makes the far end hang up. Errors use the ``{"error": ...}`` control format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import invalid_request, json_body, sim_error
from va_sim.scenario import Scenario, ScenarioIssue
from va_sim.telnyx.call_models import InboundCall, RemoteHangup
from va_sim.telnyx.calls import Call

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["call_control_routes", "find_call"]


def find_call(state: SimState, request: Request) -> Call | JSONResponse:
    """The call of the path's ``call_control_id`` on any account, else a 404 ``not_found``."""
    call_control_id = request.path_params["call_control_id"]
    found = state.telnyx.call(call_control_id)
    if found is None:
        return sim_error(404, "not_found", f"no call with call_control_id {call_control_id!r}")
    return found[1]


def call_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx
    engine = state.call_engine

    def lookup(request: Request) -> Call | JSONResponse:
        return find_call(state, request)

    def inbound_scenario(body: InboundCall) -> Scenario | JSONResponse | None:
        if body.scenario is None:
            return state.scenario_for_inbound(body.from_)
        scenario = state.scenarios.get(body.scenario)
        return (
            scenario
            if scenario is not None
            else sim_error(404, "not_found", f"no scenario {body.scenario!r}")
        )

    async def inbound(request: Request) -> Response:
        body = await json_body(request, InboundCall, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        owner = store.owner_of(body.to)
        connection_id: str | None = owner[1]["connection_id"] if owner is not None else None
        if owner is None or connection_id is None:
            issue = ScenarioIssue("to", None, None, f"{body.to} is not a number assigned to a connection")
            return invalid_request("the destination cannot receive calls", [issue])
        scenario = inbound_scenario(body)
        if isinstance(scenario, JSONResponse):
            return scenario
        account = owner[0]
        app = account.apps.get(connection_id)
        call = engine.new_call(
            account,
            direction="incoming",
            connection_id=connection_id,
            from_=body.from_,
            to=body.to,
            webhook_url=(app or {}).get("webhook_event_url") or state.webhook_defaults.voice_url,
            base_url=str(request.base_url).rstrip("/"),
            state="parked",
            scenario_id=None if scenario is None else scenario.id,
        )
        timeout = (
            app["first_command_timeout_secs"] if app is not None and app["first_command_timeout"] else None
        )
        engine.start_incoming(call, None if timeout is None else float(timeout))
        ids = {key: getattr(call, key) for key in ("call_control_id", "call_leg_id", "call_session_id")}
        return JSONResponse(ids, status_code=202)

    async def listing(_request: Request) -> Response:
        calls = sorted(store.all_calls(), key=lambda call: call.created_ms)
        return JSONResponse({"calls": [call.summary() for call in calls]})

    async def detail(request: Request) -> Response:
        call = lookup(request)
        return call if isinstance(call, JSONResponse) else JSONResponse(call.summary())

    async def timeline(request: Request) -> Response:
        call = lookup(request)
        return call if isinstance(call, JSONResponse) else JSONResponse(call.timeline())

    async def remote_hangup(request: Request) -> Response:
        call = lookup(request)
        if isinstance(call, JSONResponse):
            return call
        body = await json_body(request, RemoteHangup, allow_empty=True)
        if isinstance(body, JSONResponse):
            return body
        if call.ended:
            return sim_error(409, "call_ended", f"call {call.call_control_id!r} has already ended")
        engine.hang_up(call, body.cause, call.remote_side)
        return JSONResponse(call.summary(), status_code=202)

    return [
        Route("/sim/telnyx/inbound", inbound, methods=["POST"]),
        Route("/sim/telnyx/calls", listing, methods=["GET"]),
        Route("/sim/telnyx/calls/{call_control_id}", detail, methods=["GET"]),
        Route("/sim/telnyx/calls/{call_control_id}/timeline", timeline, methods=["GET"]),
        Route("/sim/telnyx/calls/{call_control_id}/hangup", remote_hangup, methods=["POST"]),
    ]
