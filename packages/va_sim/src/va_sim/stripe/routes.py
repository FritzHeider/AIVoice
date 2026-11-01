"""Everything FakeStripe serves beyond the shared ``/sim/*`` control API (specs/30 §4.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from starlette.routing import BaseRoute, Route

from va_sim.stripe.catalog import create_price, create_product, list_prices, retrieve_price, retrieve_product
from va_sim.stripe.checkout import (
    create_checkout_session,
    expire_checkout_session,
    list_checkout_line_items,
    retrieve_checkout_session,
)
from va_sim.stripe.checkout_complete import retrieve_setup_intent
from va_sim.stripe.control import stripe_control_routes
from va_sim.stripe.customers import create_customer, retrieve_customer, update_customer
from va_sim.stripe.errors import unrecognized_url
from va_sim.stripe.events import list_events, retrieve_event
from va_sim.stripe.http import Handler, StripeCall, stripe_endpoint
from va_sim.stripe.invoices import list_invoices, pay_invoice, retrieve_invoice
from va_sim.stripe.payment_intents import create_payment_intent, retrieve_payment_intent
from va_sim.stripe.payment_methods import (
    attach_payment_method,
    customer_payment_methods,
    retrieve_payment_method,
)
from va_sim.stripe.portal import create_portal_session
from va_sim.stripe.subscription_update import update_subscription
from va_sim.stripe.subscriptions import (
    cancel_subscription,
    create_subscription,
    list_subscriptions,
    retrieve_subscription,
)

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["stripe_routes"]

_ALL_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE"]

# (path, method, handler); the order matters only for the catch-all, which comes last.
_API: Final[tuple[tuple[str, str, Handler], ...]] = (
    ("/v1/customers", "POST", create_customer),
    ("/v1/customers/{customer_id}", "GET", retrieve_customer),
    ("/v1/customers/{customer_id}", "POST", update_customer),
    ("/v1/customers/{customer_id}/payment_methods", "GET", customer_payment_methods),
    ("/v1/payment_methods/{pm_id}", "GET", retrieve_payment_method),
    ("/v1/payment_methods/{pm_id}/attach", "POST", attach_payment_method),
    ("/v1/payment_intents", "POST", create_payment_intent),
    ("/v1/payment_intents/{pi_id}", "GET", retrieve_payment_intent),
    ("/v1/billing_portal/sessions", "POST", create_portal_session),
    ("/v1/products", "POST", create_product),
    ("/v1/products/{product_id}", "GET", retrieve_product),
    ("/v1/prices", "POST", create_price),
    ("/v1/prices", "GET", list_prices),
    ("/v1/prices/{price_id}", "GET", retrieve_price),
    ("/v1/checkout/sessions", "POST", create_checkout_session),
    ("/v1/checkout/sessions/{cs_id}", "GET", retrieve_checkout_session),
    ("/v1/checkout/sessions/{cs_id}/line_items", "GET", list_checkout_line_items),
    ("/v1/checkout/sessions/{cs_id}/expire", "POST", expire_checkout_session),
    ("/v1/setup_intents/{seti_id}", "GET", retrieve_setup_intent),
    ("/v1/subscriptions", "POST", create_subscription),
    ("/v1/subscriptions", "GET", list_subscriptions),
    ("/v1/subscriptions/{sub_id}", "GET", retrieve_subscription),
    ("/v1/subscriptions/{sub_id}", "POST", update_subscription),
    ("/v1/subscriptions/{sub_id}", "DELETE", cancel_subscription),
    ("/v1/invoices", "GET", list_invoices),
    ("/v1/invoices/{in_id}", "GET", retrieve_invoice),
    ("/v1/invoices/{in_id}/pay", "POST", pay_invoice),
    ("/v1/events", "GET", list_events),
    ("/v1/events/{event_id}", "GET", retrieve_event),
)


async def _unrecognized(call: StripeCall) -> dict[str, Any]:
    raise unrecognized_url(call.method, call.path)


def stripe_routes(state: SimState) -> list[BaseRoute]:
    """The Stripe REST subset, the ``/sim/stripe/*`` control routes, and a catch-all 404 for other ``/v1``
    URLs (also answering known paths called with another method, as Stripe does)."""
    routes: list[BaseRoute] = [
        Route(path, stripe_endpoint(state, handler), methods=[method]) for path, method, handler in _API
    ]
    routes.extend(stripe_control_routes(state))
    routes.append(Route("/v1/{path:path}", stripe_endpoint(state, _unrecognized), methods=_ALL_METHODS))
    return routes
