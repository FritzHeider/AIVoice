"""The hosted checkout page ``GET /sim/stripe/checkout/{id}`` (specs/30 §4.1 Decision P03-T07b rule 4).

Without ``outcome`` it shows the session with links to complete or cancel it. ``outcome=success`` (optional
``card=<magic payment method id>``, default ``pm_card_visa``) completes it and redirects (303) to
``success_url`` with ``{CHECKOUT_SESSION_ID}`` substituted; a decline answers 402 HTML and leaves the session
open. ``outcome=cancel`` expires it and redirects to ``cancel_url`` (200 HTML without one). Unknown session →
404, a session that is not open → 409, a bad ``outcome`` or ``card`` → 400 (``/sim`` error bodies).
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from va_sim.control import sim_error
from va_sim.stripe.checkout import expire_session
from va_sim.stripe.checkout_complete import CheckoutDeclinedError, complete_session
from va_sim.stripe.objects import TEST_CARDS

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["checkout_page"]

_DEFAULT_CARD: Final = "pm_card_visa"
_SESSION_ID_TEMPLATE: Final = "{CHECKOUT_SESSION_ID}"
_CENTS: Final = 100


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title></head><body>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status,
    )


def _summary(session: Mapping[str, Any]) -> str:
    parts = [f"<p>Session <code>{html.escape(session['id'])}</code> ({html.escape(session['mode'])})</p>"]
    if session["amount_total"] is not None:
        amount = f"{session['amount_total'] / _CENTS:.2f} {str(session['currency']).upper()}"
        parts.append(f"<p>Total {html.escape(amount)}</p>")
    return "".join(parts)


def _show(session: Mapping[str, Any]) -> HTMLResponse:
    links = (
        '<p><a href="?outcome=success">Pay</a> · '
        '<a href="?outcome=success&amp;card=pm_card_chargeDeclined">Pay with a declined card</a> · '
        '<a href="?outcome=cancel">Cancel</a></p>'
    )
    return _page("FakeStripe Checkout", _summary(session) + links)


async def checkout_page(state: SimState, request: Request) -> Response:
    session_id = request.path_params["session_id"]
    session = state.stripe.checkout_sessions.get(session_id)
    if session is None:
        return sim_error(404, "not_found", f"no checkout session with id {session_id!r}")
    if session["status"] != "open":
        return sim_error(409, "conflict", f"checkout session {session_id} is {session['status']}, not open")
    outcome = request.query_params.get("outcome")
    if outcome is None:
        return _show(session)
    if outcome == "cancel":
        return await _cancel(state, session)
    if outcome != "success":
        return sim_error(400, "invalid_request", f"outcome must be success or cancel, not {outcome!r}")
    return await _complete(state, session, request.query_params.get("card", _DEFAULT_CARD))


async def _cancel(state: SimState, session: dict[str, Any]) -> Response:
    await expire_session(state, session, call=None)
    if session["cancel_url"] is None:
        return _page("Checkout canceled", _summary(session))
    return RedirectResponse(session["cancel_url"], status_code=303)


async def _complete(state: SimState, session: dict[str, Any], card_id: str) -> Response:
    card = TEST_CARDS.get(card_id)
    if card is None:
        cards = ", ".join(TEST_CARDS)
        return sim_error(400, "invalid_request", f"card must be one of {cards}, not {card_id!r}")
    try:
        await complete_session(state, session, card)
    except CheckoutDeclinedError as declined:
        decision = declined.decision
        code = html.escape(decision.decline_code or "")
        body = f"<p>{html.escape(decision.message)} (<code>{code}</code>)</p>"
        return _page("Payment declined", _summary(session) + body, status=402)
    target = session["success_url"].replace(_SESSION_ID_TEMPLATE, session["id"])
    return RedirectResponse(target, status_code=303)
