"""FakeTelnyx control routes (specs/30-testing-and-evals.md §3.2 Control API; Decision 2026-10-29).

``GET /sim/telnyx/public-key``, ``POST /sim/telnyx/accounts``, ``GET``/``PATCH /sim/telnyx/config``, ``GET
/sim/telnyx/webhooks`` (delivery log), ``GET /sim/telnyx/messages`` (every account's messages), and ``POST
/sim/telnyx/messages/inbound`` (emits ``message.received``). Errors use the ``{"error": ...}`` control format.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.control import invalid_request, json_body
from va_sim.scenario import ScenarioIssue
from va_sim.telnyx.messages import message_record
from va_sim.telnyx.models import AccountCreate, InboundMessage, TelnyxConfigPatch

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["telnyx_control_routes"]


def telnyx_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx

    async def public_key(_request: Request) -> Response:
        return JSONResponse({"public_key": state.signing_key.public_key_b64})

    async def create_account(request: Request) -> Response:
        body = await json_body(request, AccountCreate, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.account(body.api_key)
        return JSONResponse({"api_key": body.api_key}, status_code=201)

    async def read_config(_request: Request) -> Response:
        return JSONResponse(store.config.model_dump(mode="json"))

    async def patch_config(request: Request) -> Response:
        body = await json_body(request, TelnyxConfigPatch, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.config = store.config.model_copy(update=body.model_dump(exclude_none=True))
        return JSONResponse(store.config.model_dump(mode="json"))

    async def webhooks(_request: Request) -> Response:
        return JSONResponse({"deliveries": [asdict(delivery) for delivery in state.webhooks.deliveries]})

    async def messages(_request: Request) -> Response:
        return JSONResponse({"messages": list(store.all_messages())})

    async def inbound(request: Request) -> Response:
        body = await json_body(request, InboundMessage, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        owner = store.owner_of(body.to)
        if owner is None:
            issue = ScenarioIssue("to", None, None, f"{body.to} is not a number of any account")
            return invalid_request("the destination is not an owned number", [issue])
        account, number = owner
        profile = account.messaging_profiles.get(number["messaging_profile_id"] or "")
        profile_url: str | None = profile["webhook_url"] if profile is not None else None
        url = profile_url or state.webhook_defaults.messaging_url
        record = message_record(
            message_id=state.ids.uuid(),
            direction="inbound",
            from_number=body.from_,
            to_number=body.to,
            to_status="webhook_delivered",
            text=body.text,
            media_urls=body.media_urls,
            subject=None,
            messaging_profile_id=number["messaging_profile_id"],
            webhook_url=url,
            webhook_failover_url=None,
            received_at=iso_z(state.clock.now()),
            valid_until=None,
        )
        account.messages[record["id"]] = record
        await state.webhooks.emit(url, "message.received", record)
        return JSONResponse({"id": record["id"]}, status_code=202)

    return [
        Route("/sim/telnyx/public-key", public_key, methods=["GET"]),
        Route("/sim/telnyx/accounts", create_account, methods=["POST"]),
        Route("/sim/telnyx/config", read_config, methods=["GET"]),
        Route("/sim/telnyx/config", patch_config, methods=["PATCH"]),
        Route("/sim/telnyx/webhooks", webhooks, methods=["GET"]),
        Route("/sim/telnyx/messages", messages, methods=["GET"]),
        Route("/sim/telnyx/messages/inbound", inbound, methods=["POST"]),
    ]
