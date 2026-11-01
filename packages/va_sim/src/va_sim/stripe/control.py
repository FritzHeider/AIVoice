"""FakeStripe control routes (specs/30 §4.1; Decision P03-T07a rule 11), served on the FakeStripe port only.

``GET``/``PATCH /sim/stripe/config``, ``POST /sim/stripe/api-keys``, ``GET``/``POST /sim/stripe/events``
(list oldest first; fire an arbitrary signed event), and ``GET /sim/stripe/webhooks`` (delivery log).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import json_body
from va_sim.stripe.events import emit_event
from va_sim.stripe.portal import portal_page
from va_sim.stripe.store import StripeConfig

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["stripe_control_routes"]

_KEY_PREFIX: Final = "sk_"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigPatch(_Strict):
    """Only the fields sent change; ``webhook_url: null`` disables delivery."""

    decline: bool | None = None
    decline_code: str | None = Field(default=None, min_length=1)
    webhook_url: str | None = Field(default=None, min_length=1)
    webhook_secret: str | None = Field(default=None, min_length=1)


class ApiKeyBody(_Strict):
    api_key: str = Field(pattern=rf"^{_KEY_PREFIX}\S+$")


class FireBody(_Strict):
    type: str = Field(min_length=1)
    object: dict[str, Any]


def _config_json(config: StripeConfig) -> JSONResponse:
    return JSONResponse(asdict(config))


def stripe_control_routes(state: SimState) -> list[BaseRoute]:
    async def read_config(_request: Request) -> Response:
        return _config_json(state.stripe.config)

    async def patch_config(request: Request) -> Response:
        body = await json_body(request, ConfigPatch, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        changes = {name: getattr(body, name) for name in body.model_fields_set}
        for name in ("decline", "decline_code", "webhook_secret"):
            if name in changes and changes[name] is None:
                del changes[name]
        return _config_json(state.stripe.update_config(**changes))

    async def add_key(request: Request) -> Response:
        body = await json_body(request, ApiKeyBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        state.stripe.keys.add(body.api_key)
        return Response(status_code=204)

    async def events(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"events": list(state.stripe.events.values())})
        body = await json_body(request, FireBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        event = await emit_event(state, body.type, body.object, call=None)
        return JSONResponse(event, status_code=201)

    async def webhooks(_request: Request) -> Response:
        return JSONResponse({"deliveries": [asdict(d) for d in state.stripe_webhooks.deliveries]})

    async def portal(request: Request) -> Response:
        return await portal_page(state, request)

    return [
        Route("/sim/stripe/config", read_config, methods=["GET"]),
        Route("/sim/stripe/config", patch_config, methods=["PATCH"]),
        Route("/sim/stripe/api-keys", add_key, methods=["POST"]),
        Route("/sim/stripe/events", events, methods=["GET", "POST"]),
        Route("/sim/stripe/webhooks", webhooks, methods=["GET"]),
        Route("/sim/stripe/portal/{session_id}", portal, methods=["GET"]),
    ]
