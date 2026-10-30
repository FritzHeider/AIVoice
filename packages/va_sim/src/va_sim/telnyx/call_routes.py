"""FakeTelnyx Call Control REST: ``POST /v2/calls`` (dial) and ``POST /v2/calls/{ccid}/actions/{action}``
(specs/06-telephony-telnyx.md §2.1; specs/30-testing-and-evals.md §3.2, Decision 2026-10-29 P03-T02b).

Every command answers 200 ``{"data": {"result": "ok"}}``; its webhooks follow after the response. A repeated
``command_id`` (per call; per account for dial) answers the same success without running again and is logged
with ``duplicate: true``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.telnyx.call_actions import Action, call_actions, pending_media, state_error, transfer_outcome
from va_sim.telnyx.call_models import DialRequest
from va_sim.telnyx.calls import Call, CommandEntry, LegOutcome, outcome_of
from va_sim.telnyx.errors import call_ended, invalid, not_found
from va_sim.telnyx.http import authenticated, data, parse_body
from va_sim.telnyx.media_format import check_stream_params
from va_sim.telnyx.store import Account, Record

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["OK", "call_routes", "dial_response", "log_command"]

OK: dict[str, str] = {"result": "ok"}


def dial_response(call: Call) -> Record:
    return {
        "record_type": "call",
        "call_control_id": call.call_control_id,
        "call_leg_id": call.call_leg_id,
        "call_session_id": call.call_session_id,
        "is_alive": call.is_alive,
        "client_state": call.client_state,
    }


def log_command(state: SimState, call: Call, name: str, command_id: str | None, *, duplicate: bool) -> None:
    """Append to the call's command log; a first-time ``command_id`` is remembered for dedup."""
    call.commands.append(CommandEntry(name, command_id, iso_z(state.clock.now()), duplicate))
    if command_id is not None:
        call.command_ids.add(command_id)


def call_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx
    engine = state.call_engine
    actions = call_actions(state)

    def outcome(body: DialRequest, link: Call | None) -> tuple[LegOutcome, str | None]:
        scenario = state.scenario_for_destination(body.to)
        if scenario is not None:
            return outcome_of(scenario.answer), scenario.id
        if link is not None:
            return transfer_outcome(state, link), link.scenario_id
        return outcome_of(None), None

    async def dial(request: Request, account: Account) -> Response:
        body = await parse_body(request, DialRequest)
        if isinstance(body, JSONResponse):
            return body
        known = account.calls.get(account.dial_commands.get(body.command_id or "", ""))
        if known is not None:
            log_command(state, known, "dial", body.command_id, duplicate=True)
            return data(dial_response(known))
        app = account.apps.get(body.connection_id)
        if app is None:
            return invalid(f"Connection {body.connection_id!r} does not exist.", "/connection_id")
        unsupported = check_stream_params(body)
        if unsupported is not None:
            return unsupported
        link = account.calls.get(body.link_to) if body.link_to is not None else None
        if body.link_to is not None and link is None:
            return invalid(f"Call {body.link_to!r} does not exist.", "/link_to")
        leg_outcome, scenario_id = outcome(body, link)
        call = engine.new_call(
            account,
            call_session_id=None if link is None else link.call_session_id,
            direction="outgoing",
            connection_id=body.connection_id,
            from_=body.from_,
            to=body.to,
            webhook_url=body.webhook_url or app["webhook_event_url"] or state.webhook_defaults.voice_url,
            base_url=str(request.base_url).rstrip("/"),
            state="bridging",
            client_state=body.client_state,
            scenario_id=scenario_id,
            outcome=leg_outcome,
            amd=body.answering_machine_detection,
            time_limit_s=float(body.time_limit_secs),
            pending=pending_media(body),
            bridge_on_answer=link if body.bridge_on_answer else None,
        )
        log_command(state, call, "dial", body.command_id, duplicate=False)
        if body.command_id is not None:
            account.dial_commands[body.command_id] = call.call_control_id
        engine.start_outgoing(call, float(body.timeout_secs))
        return data(dial_response(call))

    def target(request: Request, account: Account) -> tuple[str, Action, Call] | JSONResponse:
        name = request.path_params["action"]
        action = actions.get(name)
        if action is None:
            return not_found("call action", name)
        call_control_id = request.path_params["call_control_id"]
        call = account.calls.get(call_control_id)
        return (name, action, call) if call is not None else not_found("call", call_control_id)

    async def command(request: Request, account: Account) -> Response:
        found = target(request, account)
        if isinstance(found, JSONResponse):
            return found
        name, action, call = found
        body = await parse_body(request, action.model)
        if isinstance(body, JSONResponse):
            return body
        if body.command_id is not None and body.command_id in call.command_ids:
            log_command(state, call, name, body.command_id, duplicate=True)
            return data(OK)
        error = (
            (call_ended(call.call_control_id) if call.ended else None)
            or state_error(action.needs, call, name)
            or action.check(account, call, body)
        )
        if error is not None:
            return error
        log_command(state, call, name, body.command_id, duplicate=False)
        if body.client_state is not None:
            call.client_state = body.client_state
        action.run(account, call, body)
        return data(OK)

    return [
        Route("/v2/calls", authenticated(store, dial), methods=["POST"]),
        Route(
            "/v2/calls/{call_control_id}/actions/{action}", authenticated(store, command), methods=["POST"]
        ),
    ]
