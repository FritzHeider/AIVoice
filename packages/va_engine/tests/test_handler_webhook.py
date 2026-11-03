"""Webhook node handler (specs/11 §4 "Webhook" row, §3.2.1, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import (
    FakeExtractor,
    FakeHttp,
    RecordingSink,
    handler_ctx,
    ok,
    spoken,
)
from service_helpers import json_log_stream, log_events

from va_engine.handlers import ExtractionReport, WebhookHandler, WebhookReport, redacted_url
from va_engine.models import Say
from va_engine.ports import HttpOutcome

HANDLER = WebhookHandler()


def webhook_node(
    node_id: str = "hook", *, extra: dict[str, Any] | None = None, **config: Any
) -> dict[str, Any]:
    body = {"url": "https://api.acme.test/lookup", **config}
    return node(node_id, node_type="Webhook", webhook=body, **(extra or {}))


def targets() -> list[dict[str, Any]]:
    return [node("active"), node("err"), node("timeout_node"), node("ok_node")]


async def test_extraction_runs_before_the_request_and_feeds_the_body() -> None:
    order: list[str] = []
    http = FakeHttp(order=order)
    extractor = FakeExtractor(values={"first_name": "Ada"}, order=order)
    raw = webhook_node(
        body={"name": "{{first_name}}"},
        extra={"extractVars": [{"name": "first_name", "type": "string", "description": "first name"}]},
    )
    ctx = handler_ctx(raw, http=http, extractor=extractor)

    result = await HANDLER.on_enter(ctx)

    assert order == ["extract:hook", "http:POST"]
    assert http.calls[0].body == {"name": "Ada"}
    assert isinstance(result.reports[0], ExtractionReport)
    assert result.reports[0].event_data() == {"node_id": "hook", "variables": {"first_name": "Ada"}}
    assert isinstance(result.reports[1], WebhookReport)


async def test_no_extraction_without_extract_vars_or_without_values() -> None:
    order: list[str] = []
    extractor = FakeExtractor(values=None, order=order)
    plain = handler_ctx(webhook_node(), http=FakeHttp(order=order), extractor=extractor)
    await HANDLER.on_enter(plain)
    assert order == ["http:POST"]

    order.clear()
    raw = webhook_node(extra={"extractVars": [{"name": "x"}]})
    nothing = handler_ctx(raw, http=FakeHttp(order=order), extractor=extractor)
    result = await HANDLER.on_enter(nothing)
    assert order == ["extract:hook", "http:POST"]
    assert [type(report) for report in result.reports] == [WebhookReport]


async def test_speech_is_queued_before_the_request() -> None:
    order: list[str] = []
    sink = RecordingSink(order=order)
    http = FakeHttp(order=order)
    ctx = handler_ctx(
        webhook_node(speech="One moment, {{first_name}}."),
        http=http,
        sink=sink,
        variables={"first_name": "Ada"},
    )

    result = await HANDLER.on_enter(ctx)

    assert order == ["sink:Say", "http:POST"]
    assert sink.actions == [Say(text="One moment, Ada.", mark="t3.1", interruptible=None)]
    assert result.actions == ()


async def test_speech_leads_the_actions_without_a_sink() -> None:
    raw = webhook_node(speech="Checking.", extra={"modelOptions": {"block_interruptions": True}})
    ctx = handler_ctx(raw, http=FakeHttp())

    stream = json_log_stream()
    result = await HANDLER.on_enter(ctx)

    assert await spoken(result.actions) == [("say", "Checking.", "t3.1", False)]
    assert any(event["event"] == "engine.webhook_speech_late" for event in log_events(stream))


@pytest.mark.parametrize("channel", ["sms", "widget_chat"])
async def test_text_channels_drop_the_speech(channel: Any) -> None:
    sink = RecordingSink()
    ctx = handler_ctx(webhook_node(speech="One moment."), channel=channel, http=FakeHttp(), sink=sink)

    result = await HANDLER.on_enter(ctx)

    assert sink.actions == []
    assert result.actions == ()


async def test_blank_speech_says_nothing() -> None:
    sink = RecordingSink()
    ctx = handler_ctx(webhook_node(speech="{{missing}}  "), http=FakeHttp(), sink=sink)
    await HANDLER.on_enter(ctx)
    assert sink.actions == []


async def test_url_headers_and_body_are_rendered() -> None:
    http = FakeHttp()
    raw = webhook_node(
        url="https://api.acme.test/lookup?phone={{from}}&q={{query}}",
        method="PUT",
        headers={"Authorization": "Bearer {{request_data.account}}"},
        body={"count": "{{count}}", "label": "n={{count}}", "nested": ["{{flag}}"], "fixed": 3},
        timeoutMs=2500,
        retries=2,
        responseData=[{"name": "status", "data": "$.status"}],
    )
    ctx = handler_ctx(raw, http=http, variables={"count": 4, "flag": True, "query": "a b&c"})

    await HANDLER.on_enter(ctx)

    call = http.calls[0]
    assert call.method == "PUT"
    assert call.url == "https://api.acme.test/lookup?phone=%2B14155550100&q=a%20b%26c"
    assert call.headers == {"Authorization": "Bearer A-7"}
    assert call.body == {"count": 4, "label": "n=4", "nested": [True], "fixed": 3}
    assert (call.timeout_ms, call.retries) == (2500, 2)
    assert call.extract == (("status", "$.status"),)


async def test_body_with_a_template_error_is_sent_raw_and_non_json_values_become_text() -> None:
    http = FakeHttp()
    raw = webhook_node(body={"broken": "{{oops", "when": "{{when}}"})
    ctx = handler_ctx(raw, http=http, variables={"when": "ok"})
    await HANDLER.on_enter(ctx)
    assert http.calls[0].body == {"broken": "{{oops", "when": "{{when}}"}

    http = FakeHttp()
    ctx = handler_ctx(webhook_node(body={"when": "{{now}}"}), http=http, builtins={"now": object()})
    await HANDLER.on_enter(ctx)
    assert isinstance(http.calls[0].body, dict)
    assert isinstance(http.calls[0].body["when"], str)


async def test_body_none_is_not_sent_and_template_errors_in_the_url_keep_it_raw() -> None:
    http = FakeHttp()
    ctx = handler_ctx(webhook_node(url="https://api.acme.test/{{bad"), http=http)
    await HANDLER.on_enter(ctx)
    assert http.calls[0].body is None
    assert http.calls[0].url == "https://api.acme.test/{{bad"


async def test_extracted_values_merge_only_when_not_null() -> None:
    http = FakeHttp([ok({"a": 1}, account_status="active", tier=None)])
    ctx = handler_ctx(webhook_node(), http=http, variables={"tier": "gold"})

    result = await HANDLER.on_enter(ctx)

    assert ctx.state.variables == {"tier": "gold", "account_status": "active"}
    report = result.reports[-1]
    assert isinstance(report, WebhookReport)
    assert report.variables_set == ("account_status",)


async def test_error_responses_do_not_merge_values() -> None:
    http = FakeHttp([ok({"status": "x"}, status=404, account_status="gone")])
    ctx = handler_ctx(webhook_node(), http=http)
    await HANDLER.on_enter(ctx)
    assert "account_status" not in ctx.state.variables


async def test_first_matching_response_pathway_wins() -> None:
    pathways = [
        {"variable": "account_status", "operator": "==", "value": "closed", "targetNodeId": "err"},
        {"variable": "account_status", "operator": "==", "value": "active", "targetNodeId": "active"},
        {"variable": "account_status", "operator": "is_not_empty", "targetNodeId": "ok_node"},
    ]
    http = FakeHttp([ok(account_status="active")])
    ctx = handler_ctx(
        webhook_node(responsePathways=pathways, routeOnStatus={"2xx": "ok_node"}),
        http=http,
        extra_nodes=targets(),
    )

    result = await HANDLER.on_enter(ctx)

    assert result.route_to == "active"
    report = result.reports[-1]
    assert isinstance(report, WebhookReport)
    assert report.route_to == "active"


async def test_missing_pathway_target_is_skipped() -> None:
    pathways = [
        {"variable": "s", "operator": "==", "value": "x", "targetNodeId": "nowhere"},
        {"variable": "s", "operator": "==", "value": "x", "targetNodeId": "active"},
    ]
    ctx = handler_ctx(
        webhook_node(responsePathways=pathways), http=FakeHttp([ok(s="x")]), extra_nodes=targets()
    )

    stream = json_log_stream()
    result = await HANDLER.on_enter(ctx)

    assert result.route_to == "active"
    missing = [event for event in log_events(stream) if event["event"] == "engine.webhook_route_missing"]
    assert missing[0]["source"] == "responsePathways"


async def test_response_pathways_are_ignored_after_a_failure() -> None:
    pathways = [{"variable": "s", "operator": "is_empty", "targetNodeId": "active"}]
    http = FakeHttp([ok(status=500)])
    ctx = handler_ctx(
        webhook_node(responsePathways=pathways, routeOnStatus={"5xx": "err"}),
        http=http,
        extra_nodes=targets(),
    )
    result = await HANDLER.on_enter(ctx)
    assert result.route_to == "err"


@pytest.mark.parametrize(
    ("outcome", "route"),
    [
        (ok(), "ok_node"),
        (ok(status=201), "ok_node"),
        (ok(status=302), None),
        (ok(status=404), "err"),
        (ok(status=503), "active"),
        (HttpOutcome(kind="timeout", attempts=2), "timeout_node"),
        (HttpOutcome(kind="connection"), "timeout_node"),
        (HttpOutcome(kind="blocked", attempts=0), "timeout_node"),
        (HttpOutcome(kind="http_error", status=999), None),
    ],
)
async def test_route_on_status(outcome: HttpOutcome, route: str | None) -> None:
    routes = {"2xx": "ok_node", "4xx": "err", "5xx": "active", "timeout": "timeout_node"}
    ctx = handler_ctx(webhook_node(routeOnStatus=routes), http=FakeHttp([outcome]), extra_nodes=targets())
    result = await HANDLER.on_enter(ctx)
    assert result.route_to == route


async def test_no_route_means_normal_routing() -> None:
    ctx = handler_ctx(
        webhook_node(routeOnStatus={"4xx": "err"}), http=FakeHttp([ok()]), extra_nodes=targets()
    )
    result = await HANDLER.on_enter(ctx)
    assert result.route_to is None
    assert result.problem is None


async def test_unknown_status_target_is_skipped() -> None:
    ctx = handler_ctx(webhook_node(routeOnStatus={"2xx": "ghost"}), http=FakeHttp([ok()]))
    result = await HANDLER.on_enter(ctx)
    assert result.route_to is None


async def test_report_event_data_has_no_query_and_no_bodies() -> None:
    http = FakeHttp([ok({"secret": "s3"}, status=200, status_value="v")])
    raw = webhook_node(url="https://user:pw@api.acme.test:8443/v1/lookup?token={{from}}#frag")
    ctx = handler_ctx(raw, http=http)

    result = await HANDLER.on_enter(ctx)

    report = result.reports[-1]
    assert isinstance(report, WebhookReport)
    assert report.event_type == "call.webhook_node"
    assert report.event_data() == {
        "node_id": "hook",
        "node_name": "hook",
        "purpose": "webhook",
        "method": "POST",
        "url": "https://api.acme.test:8443/v1/lookup",
        "outcome": "ok",
        "status_code": 200,
        "attempts": 1,
        "duration_ms": 42,
        "variables_set": ["status_value"],
        "route_to": None,
    }


def test_unparseable_urls_redact_to_nothing() -> None:
    assert redacted_url("https://[::1/x") == ""
    assert redacted_url("https://[::1]:8443/p?q=1") == "https://[::1]:8443/p"
    assert redacted_url("relative/path") == ""


async def test_logs_never_contain_the_url_or_values() -> None:
    http = FakeHttp([ok(account="12345")])
    ctx = handler_ctx(webhook_node(url="https://api.acme.test/x?phone={{from}}"), http=http)

    stream = json_log_stream()
    await HANDLER.on_enter(ctx)

    text = stream.getvalue()
    assert "4155550100" not in text
    assert "api.acme.test" not in text
    assert "12345" not in text
    assert any(event["event"] == "engine.webhook_node" for event in log_events(stream))


async def test_missing_http_port_is_a_problem() -> None:
    result = await HANDLER.on_enter(handler_ctx(webhook_node()))
    assert result.problem == "http_unavailable"
    assert result.reports == ()


async def test_blank_url_is_not_sent() -> None:
    http = FakeHttp()
    ctx = handler_ctx(
        webhook_node(url="{{nothing}}", routeOnStatus={"timeout": "err"}), http=http, extra_nodes=targets()
    )

    result = await HANDLER.on_enter(ctx)

    assert http.calls == []
    assert result.problem == "webhook_url_missing"
    assert result.route_to == "err"
    report = result.reports[-1]
    assert isinstance(report, WebhookReport)
    assert report.event_data()["outcome"] == "invalid_url"
    assert report.event_data()["url"] == ""


async def test_absent_config_means_a_blank_url() -> None:
    ctx = handler_ctx(node("hook", node_type="Webhook"), http=FakeHttp())
    result = await HANDLER.on_enter(ctx)
    assert result.problem == "webhook_url_missing"


async def test_respond_and_waits_for_user() -> None:
    silent = handler_ctx(webhook_node())
    assert (await HANDLER.respond(silent)).actions == ()
    assert HANDLER.waits_for_user(silent.node) is False

    texted = handler_ctx(webhook_node(extra={"text": "Your status is {{s}}."}), variables={"s": "active"})
    assert await spoken((await HANDLER.respond(texted)).actions) == [
        ("say", "Your status is active.", "t3.1", None)
    ]
    assert HANDLER.waits_for_user(texted.node) is True

    prompted = handler_ctx(webhook_node(extra={"prompt": "Explain the status."}))
    assert await spoken((await HANDLER.respond(prompted)).actions) == [("stream", "Hi there", "t3.1", None)]
    assert HANDLER.waits_for_user(prompted.node) is True

    skipped = handler_ctx(webhook_node(extra={"prompt": "p", "modelOptions": {"skipUserResponse": True}}))
    assert HANDLER.waits_for_user(skipped.node) is False
