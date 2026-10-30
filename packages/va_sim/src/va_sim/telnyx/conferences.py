"""FakeTelnyx conferences (specs/06-telephony-telnyx.md §2.1, §10.2; specs/30-testing-and-evals.md §3.2,
Decision 2026-10-29 P03-T02b rule 12).

``POST /v2/conferences`` (the creating leg joins at once), ``GET /v2/conferences/{id}``, and
``POST /v2/conferences/{id}/actions/join|leave``. Participants must be answered legs of the same account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.telnyx.call_models import ConferenceCreate, ConferenceJoin, ConferenceLeave
from va_sim.telnyx.call_routes import OK, log_command
from va_sim.telnyx.calls import Call, Conference
from va_sim.telnyx.errors import call_ended, invalid, invalid_state, not_found
from va_sim.telnyx.http import authenticated, data, parse_body
from va_sim.telnyx.store import Account

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["conference_routes"]

type ParticipantBody = ConferenceCreate | ConferenceJoin | ConferenceLeave


def _participant(account: Account, body: ParticipantBody) -> Call | JSONResponse:
    """The leg named by ``call_control_id``: it must exist and still be up."""
    call = account.calls.get(body.call_control_id)
    if call is None:
        return invalid(f"Call {body.call_control_id!r} does not exist.", "/call_control_id")
    if call.ended:
        return call_ended(call.call_control_id)
    return call


def _conference(account: Account, request: Request) -> Conference | JSONResponse:
    conference_id = request.path_params["conference_id"]
    conference = account.conferences.get(conference_id)
    return conference if conference is not None else not_found("conference", conference_id)


def _duplicate(call: Call, body: ParticipantBody) -> bool:
    return body.command_id is not None and body.command_id in call.command_ids


async def _membership[B: (ConferenceJoin, ConferenceLeave)](
    request: Request, account: Account, model: type[B]
) -> tuple[Conference, Call, B] | JSONResponse:
    """The conference, the participant leg, and the body of a join or leave command."""
    conference = _conference(account, request)
    if isinstance(conference, JSONResponse):
        return conference
    body = await parse_body(request, model)
    if isinstance(body, JSONResponse):
        return body
    call = _participant(account, body)
    if isinstance(call, JSONResponse):
        return call
    return conference, call, body


def conference_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx
    engine = state.call_engine

    async def create(request: Request, account: Account) -> Response:
        body = await parse_body(request, ConferenceCreate)
        if isinstance(body, JSONResponse):
            return body
        call = _participant(account, body)
        if isinstance(call, JSONResponse):
            return call
        if _duplicate(call, body) and call.conference is not None:
            log_command(state, call, "conference_create", body.command_id, duplicate=True)
            return data(call.conference.record)
        if not call.answered or call.conference is not None:
            return invalid_state("Call is not in a state that accepts conference_create.")
        log_command(state, call, "conference_create", body.command_id, duplicate=False)
        if body.client_state is not None:
            call.client_state = body.client_state
        return data(engine.create_conference(account, call, body.name).record)

    async def detail(request: Request, account: Account) -> Response:
        conference = _conference(account, request)
        return conference if isinstance(conference, JSONResponse) else data(conference.record)

    async def join(request: Request, account: Account) -> Response:
        found = await _membership(request, account, ConferenceJoin)
        if isinstance(found, JSONResponse):
            return found
        conference, call, body = found
        if _duplicate(call, body):
            log_command(state, call, "conference_join", body.command_id, duplicate=True)
            return data(OK)
        if conference.record["status"] != "in_progress":
            return invalid_state(f"Conference {conference.conference_id!r} has ended.")
        if not call.answered or call.conference is not None:
            return invalid_state("Call is not in a state that accepts conference_join.")
        log_command(state, call, "conference_join", body.command_id, duplicate=False)
        if body.client_state is not None:
            call.client_state = body.client_state
        engine.join_conference(call, conference, end_on_exit=body.end_conference_on_exit)
        return data(OK)

    async def leave(request: Request, account: Account) -> Response:
        found = await _membership(request, account, ConferenceLeave)
        if isinstance(found, JSONResponse):
            return found
        conference, call, body = found
        if _duplicate(call, body):
            log_command(state, call, "conference_leave", body.command_id, duplicate=True)
            return data(OK)
        if call.conference is not conference:
            return invalid_state("Call is not a participant of this conference.")
        log_command(state, call, "conference_leave", body.command_id, duplicate=False)
        engine.send(engine.leave_conference(call))
        return data(OK)

    return [
        Route("/v2/conferences", authenticated(store, create), methods=["POST"]),
        Route("/v2/conferences/{conference_id}", authenticated(store, detail), methods=["GET"]),
        Route("/v2/conferences/{conference_id}/actions/join", authenticated(store, join), methods=["POST"]),
        Route("/v2/conferences/{conference_id}/actions/leave", authenticated(store, leave), methods=["POST"]),
    ]
