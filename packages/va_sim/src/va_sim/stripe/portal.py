"""FakeStripe billing portal (specs/30 §4.1; Decision P03-T07a rule 9): ``POST /v1/billing_portal/sessions``
and the page its ``url`` points at, ``GET /sim/stripe/portal/{id}``.

The ``url`` uses the base URL the client reached the simulator with (``http://sim:9400`` in compose).
"""

from __future__ import annotations

import copy
import html
from typing import TYPE_CHECKING, Any, Final

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from va_sim.control import sim_error
from va_sim.stripe.objects import portal_json
from va_sim.stripe.params import check_known, string
from va_sim.stripe.payment_methods import require_customer

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall

__all__ = ["create_portal_session", "portal_page"]

_SESSION_ID_LENGTH: Final = 24
_PARAMS: Final = frozenset({"customer", "return_url", "configuration", "locale"})


async def create_portal_session(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _PARAMS)
    customer_id = string(call.params, "customer", required=True)
    assert customer_id is not None  # noqa: S101 - required=True
    require_customer(call.store, customer_id)
    return_url = string(call.params, "return_url")
    session_id = f"bps_{call.state.ids.alnum(_SESSION_ID_LENGTH)}"
    session = portal_json(
        session_id,
        call.now,
        customer=customer_id,
        return_url=return_url,
        url=f"{call.base_url}/sim/stripe/portal/{session_id}",
    )
    call.store.portal_sessions[session_id] = session
    return copy.deepcopy(session)


async def portal_page(state: SimState, request: Request) -> Response:
    session_id = request.path_params["session_id"]
    session = state.stripe.portal_sessions.get(session_id)
    if session is None:
        return sim_error(404, "not_found", f"no billing portal session with id {session_id!r}")
    customer = html.escape(session["customer"])
    back = ""
    if session["return_url"]:
        back = f'<p><a href="{html.escape(session["return_url"], quote=True)}">Return</a></p>'
    page = (
        "<!doctype html><html><head><title>FakeStripe billing portal</title></head><body>"
        f"<h1>Billing portal</h1><p>Customer <code>{customer}</code></p>{back}</body></html>"
    )
    return HTMLResponse(page)
