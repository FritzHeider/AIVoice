"""FakeStripe events (specs/30 §4.1 Decision P03-T07a rule 10): emitting, ``GET /v1/events/{id}``, and
``GET /v1/events``.

An event snapshots its data object at emit time, is logged, and is delivered as a signed webhook. Events
caused by an API request carry its request id, idempotency key, and ``Stripe-Version`` (else
:data:`va_sim.stripe.objects.DEFAULT_API_VERSION`); events fired through the control API carry
``request: {id: null, idempotency_key: null}``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.errors import resource_missing
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.objects import DEFAULT_API_VERSION, event_json
from va_sim.stripe.params import check_known, string

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall

__all__ = ["emit_event", "list_events", "retrieve_event"]

_EVENT_ID_LENGTH: Final = 24
_LIST_PARAMS: Final = LIST_PARAMS | {"type"}


async def emit_event(
    state: SimState,
    event_type: str,
    data_object: Mapping[str, Any],
    *,
    call: StripeCall | None,
    previous_attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create, log, and deliver an event; returns a copy of it."""
    event = event_json(
        f"evt_{state.ids.alnum(_EVENT_ID_LENGTH)}",
        int(state.clock.now().timestamp()),
        event_type=event_type,
        data_object=copy.deepcopy(dict(data_object)),
        api_version=call.api_version if call is not None else DEFAULT_API_VERSION,
        request_id=call.request_id if call is not None else None,
        idempotency_key=call.idempotency_key if call is not None else None,
        previous_attributes=copy.deepcopy(dict(previous_attributes)) if previous_attributes else None,
    )
    state.stripe.record_event(event)
    await state.stripe_webhooks.emit(event)
    return copy.deepcopy(event)


async def retrieve_event(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    event_id = call.path_params["event_id"]
    event = call.store.events.get(event_id)
    if event is None:
        raise resource_missing("event", event_id)
    return copy.deepcopy(event)


async def list_events(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, _LIST_PARAMS)
    event_type = string(call.params, "type")
    events = (e for e in call.store.newest_events() if event_type is None or e["type"] == event_type)
    return paginate(events, call.params, "/v1/events")
