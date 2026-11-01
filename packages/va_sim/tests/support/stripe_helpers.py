"""FakeStripe test helpers: states, an in-process client, an independent form encoder and signature check.

Endpoint tests run in-process on an accelerated clock with webhooks delivered to a
:class:`telnyx_helpers.RecordingTransport`; the official ``stripe`` SDK tests use :func:`served_stripe` on the
wall clock (so ``stripe.Webhook.construct_event``'s 300 s tolerance holds).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import orjson
from sim_helpers import LATENCIES
from starlette.applications import Starlette
from telnyx_helpers import RecordingTransport

from va_sim.clock import AcceleratedClock, RealtimeClock
from va_sim.server import running_sims, simulator_routes
from va_sim.settings import StripeDefaults
from va_sim.state import SimState

__all__ = [
    "API_KEY",
    "SECRET",
    "WEBHOOK_URL",
    "checkout",
    "encode",
    "event_types",
    "events_of",
    "monthly_price",
    "ok",
    "post",
    "realtime_stripe_state",
    "renew",
    "served_stripe",
    "signature_ok",
    "stripe_client",
    "stripe_state",
    "subscribed_customer",
    "visit",
]

API_KEY: Final = "sk_test_sim"
SECRET: Final = "whsec_sim"  # noqa: S105 - simulator dev value
WEBHOOK_URL: Final = "http://api:8000/v1/billing/stripe/webhook"


def stripe_state(transport: RecordingTransport | None = None, *, seed: int = 0) -> SimState:
    """An accelerated state delivering FakeStripe webhooks to ``transport`` (a fresh recorder when None)."""
    clock = AcceleratedClock()
    receiver = transport if transport is not None else RecordingTransport()
    receiver.clock = clock
    return SimState(
        clock=clock,
        latencies=LATENCIES,
        seed=seed,
        webhook_transport=receiver,
        stripe_defaults=StripeDefaults(webhook_url=WEBHOOK_URL),
    )


def realtime_stripe_state(transport: httpx.AsyncBaseTransport, webhook_url: str = WEBHOOK_URL) -> SimState:
    return SimState(
        clock=RealtimeClock(),
        latencies=LATENCIES,
        webhook_transport=transport,
        stripe_defaults=StripeDefaults(webhook_url=webhook_url),
    )


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _flatten(f"{prefix}[{key}]", item, out)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{index}]", item, out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    else:
        out.append((prefix, str(value)))


def encode(params: Mapping[str, Any]) -> str:
    """Stripe bracket-notation form body, written independently of ``va_sim.stripe.form``."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        _flatten(key, value, pairs)
    return urlencode(pairs)


async def post(
    client: httpx.AsyncClient, path: str, params: Mapping[str, Any] | None = None, **headers: str
) -> httpx.Response:
    """POST ``params`` form-encoded (header names use ``_`` for ``-``)."""
    return await client.post(
        path,
        content=encode(params or {}),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            **{name.replace("_", "-"): value for name, value in headers.items()},
        },
    )


@asynccontextmanager
async def stripe_client(state: SimState, key: str | None = API_KEY) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process FakeStripe client (Bearer ``key`` unless None); closes the state after."""
    app = Starlette(routes=simulator_routes("stripe", state))
    headers = {"authorization": f"Bearer {key}"} if key is not None else {}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sim:9400", headers=headers
        ) as client:
            yield client
    finally:
        await state.aclose()


@asynccontextmanager
async def served_stripe(state: SimState) -> AsyncIterator[str]:
    """FakeStripe on an ephemeral port; yields its base URL."""
    async with running_sims(state, ports={"stripe": 0}) as urls:
        yield urls["stripe"]


def signature_ok(body: bytes, header: str, secret: str = SECRET) -> tuple[bool, int]:
    """Independent check of a ``Stripe-Signature`` header: (valid, timestamp)."""
    parts = dict(item.split("=", 1) for item in header.split(","))
    timestamp = parts["t"]
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"]), int(timestamp)


def events_of(transport: RecordingTransport, event_type: str | None = None) -> list[dict[str, Any]]:
    """Webhook bodies the receiver got (optionally of one type)."""
    events = [orjson.loads(item.body) for item in transport.received]
    return [event for event in events if event_type is None or event["type"] == event_type]


def event_types(transport: RecordingTransport) -> list[str]:
    """Types of the webhooks the receiver got, in order."""
    return [event["type"] for event in events_of(transport)]


def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    """The JSON body of a response that must have ``status``."""
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


async def monthly_price(
    client: httpx.AsyncClient, amount: int = 2000, *, interval: str | None = "month", name: str = "Build plan"
) -> dict[str, Any]:
    """A price with a fresh product (recurring every ``interval`` unless None)."""
    params: dict[str, Any] = {"currency": "usd", "unit_amount": amount, "product_data": {"name": name}}
    if interval is not None:
        params["recurring"] = {"interval": interval}
    return ok(await post(client, "/v1/prices", params))


async def checkout(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    """Create a checkout session (``success_url`` defaults to a dashboard URL with the session template)."""
    body = {"success_url": "http://localhost:3000/billing?session={CHECKOUT_SESSION_ID}", **params}
    return ok(await post(client, "/v1/checkout/sessions", body))


async def visit(client: httpx.AsyncClient, session: Mapping[str, Any], **query: str) -> httpx.Response:
    """GET the hosted checkout page of ``session`` (no redirects followed)."""
    return await client.get(f"/sim/stripe/checkout/{session['id']}", params=query)


async def renew(client: httpx.AsyncClient, sub_id: str, **body: Any) -> httpx.Response:
    return await client.post(f"/sim/stripe/subscriptions/{sub_id}/renew", json=body)


async def subscribed_customer(
    client: httpx.AsyncClient, price: Mapping[str, Any], card: str = "pm_card_visa", **params: Any
) -> tuple[str, dict[str, Any]]:
    """A customer paying with ``card`` by default, subscribed to ``price``: (customer id, create body)."""
    cus = ok(await post(client, "/v1/customers", {"email": "owner@acme.test"}))["id"]
    pm = ok(await post(client, f"/v1/payment_methods/{card}/attach", {"customer": cus}))["id"]
    await post(client, f"/v1/customers/{cus}", {"invoice_settings": {"default_payment_method": pm}})
    response = await post(
        client, "/v1/subscriptions", {"customer": cus, "items": [{"price": price["id"]}], **params}
    )
    return cus, response.json()
