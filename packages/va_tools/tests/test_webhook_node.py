"""A Webhook node over the real executor (specs/11 §4, specs/13 §5; Decision P07-T07)."""

from __future__ import annotations

import json

import respx
from graph_helpers import node
from handler_helpers import handler_ctx
from tools_helpers import API, plain_executor

from va_engine.handlers import WebhookHandler, WebhookReport


async def test_rendered_request_extraction_and_response_routing() -> None:
    raw = node(
        "hook",
        node_type="Webhook",
        webhook={
            "url": f"{API}/accounts?phone={{{{from}}}}",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{request_data.account}}"},
            "body": {"name": "{{first_name}}", "visits": "{{visits}}"},
            "responseData": [
                {"name": "account_status", "data": "$.account.status"},
                {"name": "open_orders", "data": "$.orders[?(@.open == true)].id"},
            ],
            "responsePathways": [
                {"variable": "account_status", "operator": "==", "value": "active", "targetNodeId": "active"}
            ],
            "routeOnStatus": {"4xx": "err"},
        },
    )
    answer = {"account": {"status": "Active"}, "orders": [{"id": 1, "open": True}, {"id": 2, "open": True}]}
    with respx.mock(base_url=API) as router:
        route = router.post("/accounts").respond(200, json=answer)
        async with plain_executor() as (executor, _clock):
            ctx = handler_ctx(
                raw,
                http=executor,
                variables={"first_name": "Ada", "visits": 3},
                extra_nodes=[node("active"), node("err")],
            )
            result = await WebhookHandler().on_enter(ctx)

    request = route.calls.last.request
    assert request.url.params["phone"] == "+14155550100"
    assert request.headers["authorization"] == "Bearer A-7"
    assert json.loads(request.content) == {"name": "Ada", "visits": 3}
    assert ctx.state.variables["account_status"] == "Active"
    assert ctx.state.variables["open_orders"] == [1, 2]
    assert result.route_to == "active"
    report = result.reports[-1]
    assert isinstance(report, WebhookReport)
    assert report.event_data()["url"] == f"{API}/accounts"
    assert report.event_data()["variables_set"] == ["account_status", "open_orders"]


async def test_error_status_routes_by_status() -> None:
    raw = node(
        "hook",
        node_type="Webhook",
        webhook={"url": f"{API}/accounts", "routeOnStatus": {"4xx": "err"}, "retries": 2},
    )
    with respx.mock(base_url=API) as router:
        route = router.post("/accounts").respond(404)
        async with plain_executor() as (executor, _clock):
            ctx = handler_ctx(raw, http=executor, extra_nodes=[node("err")])
            result = await WebhookHandler().on_enter(ctx)

    assert route.call_count == 1
    assert result.route_to == "err"
