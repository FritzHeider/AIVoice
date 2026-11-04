"""Webhook node handler (specs/11 §4 "Webhook" row, §3.2.1; Decision P07-T07 under specs/11 §4).

``on_enter``:

1. ``extractVars`` run first through the :class:`~va_engine.ports.ExtractionPort` and their values are merged,
   so the request can use them (:class:`~va_engine.handlers.reports.ExtractionReport`).
2. Voice channels queue the rendered ``speech`` on the :class:`~va_engine.ports.ActionSink` before the request
   (without a sink it is the result's first action); text channels drop it.
3. URL (values percent-encoded), header values and the body (type-preserving) are rendered and sent through
   the :class:`~va_engine.ports.HttpPort` with the ``responseData`` JSONPath pairs.
4. On a 2xx response the non-null extracted values are merged; the first ``responsePathways`` condition that
   holds picks the next node, else ``routeOnStatus`` by status class (failures without a response use the
   ``timeout`` key), else normal routing. A target that is not in the graph is skipped
   (``engine.webhook_route_missing``).

``respond`` speaks ``text``/``prompt`` when the node has one; without either the node routes at once.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.handlers.base import (
    HandlerResult,
    TurnContext,
    has_content,
    interruptible_of,
    is_text,
    speak_node,
)
from va_engine.handlers.conditions import evaluate
from va_engine.handlers.reports import ExtractionReport, NodeReport, WebhookPurpose, WebhookReport
from va_engine.models.nodes import PathwayNode, RouteOnStatus, WebhookConfig
from va_engine.models.results import Action, Say
from va_engine.ports import HttpCall, HttpMethod, HttpOutcome, StatusClass

__all__ = [
    "HTTP_UNAVAILABLE",
    "URL_MISSING",
    "WebhookHandler",
    "merge_extracted",
    "node_waits",
    "queue_speech",
    "route_target",
    "send_request",
]

HTTP_UNAVAILABLE: Final = "http_unavailable"
URL_MISSING: Final = "webhook_url_missing"

_log = get_logger(__name__)


def node_waits(node: PathwayNode) -> bool:
    """A node with a ``text``/``prompt`` reply waits for the user unless ``skipUserResponse``."""
    if node.data.model_options.skip_user_response:
        return False
    return has_content(node.data.text) or has_content(node.data.prompt)


def queue_speech(ctx: TurnContext, speech: str | None, *, field_name: str) -> tuple[Action, ...]:
    """Speak ``speech`` (rendered) while the node works: queued on the sink at once when there is one, else
    returned to lead the result's actions. Text channels and blank speech say nothing."""
    if is_text(ctx.channel) or not has_content(speech):
        return ()
    text = ctx.render(speech or "", field_name=field_name).strip()
    if not text:
        return ()
    say = Say(text=text, mark=ctx.next_mark(), interruptible=interruptible_of(ctx.node))
    if ctx.sink is None:
        _log.info("engine.webhook_speech_late", node_id=ctx.node.id)
        return (say,)
    ctx.sink.queue(say)
    return ()


def merge_extracted(ctx: TurnContext, outcome: HttpOutcome) -> tuple[str, ...]:
    """Merge the non-null extracted values of a 2xx response into the variables; the names set."""
    if not outcome.succeeded:
        return ()
    names: list[str] = []
    for name, value in outcome.extracted.items():
        if value is not None:
            ctx.state.variables[name] = value
            names.append(name)
    return tuple(names)


async def send_request(
    ctx: TurnContext, call: HttpCall, *, purpose: WebhookPurpose
) -> tuple[HttpOutcome, WebhookReport] | None:
    """Send ``call`` through the context's HTTP port; ``None`` without a port. A blank URL is not sent."""
    if not call.url.strip():
        outcome = HttpOutcome(kind="invalid_url", attempts=0)
        return outcome, _report(ctx, call, outcome, purpose=purpose)
    if ctx.http is None:
        return None
    outcome = await ctx.http.execute(call)
    return outcome, _report(ctx, call, outcome, purpose=purpose)


def _report(
    ctx: TurnContext, call: HttpCall, outcome: HttpOutcome, *, purpose: WebhookPurpose
) -> WebhookReport:
    return WebhookReport(
        node_id=ctx.node.id,
        node_name=ctx.node.data.name,
        purpose=purpose,
        method=call.method,
        url=call.url,
        outcome=outcome.kind,
        status_code=outcome.status,
        attempts=outcome.attempts,
        duration_ms=outcome.duration_ms,
    )


def route_target(ctx: TurnContext, target: str | None, *, source: str) -> str | None:
    """``target`` when it names a node of the graph; a missing node is logged and skipped."""
    if not target:
        return None
    if ctx.graph.node(target) is None:
        _log.warning("engine.webhook_route_missing", node_id=ctx.node.id, source=source)
        return None
    return target


def _status_target(routes: RouteOnStatus | None, status_class: StatusClass | None) -> str | None:
    if routes is None:
        return None
    match status_class:
        case "2xx":
            return routes.status_2xx
        case "4xx":
            return routes.status_4xx
        case "5xx":
            return routes.status_5xx
        case "timeout":
            return routes.timeout
        case _:
            return None


def _choose_route(ctx: TurnContext, config: WebhookConfig, outcome: HttpOutcome) -> str | None:
    if outcome.succeeded:
        variables = ctx.state.variables
        for pathway in config.response_pathways:
            if evaluate(pathway.operator, variables.get(pathway.variable), pathway.value):
                chosen = route_target(ctx, pathway.target_node_id, source="responsePathways")
                if chosen is not None:
                    return chosen
    status_target = _status_target(config.route_on_status, outcome.status_class())
    return route_target(ctx, status_target, source="routeOnStatus")


def _rendered_headers(ctx: TurnContext, headers: dict[str, str]) -> dict[str, str]:
    return {name: ctx.render(value, field_name=f"headers.{name}") for name, value in headers.items()}


def _call_of(ctx: TurnContext, config: WebhookConfig) -> HttpCall:
    method: HttpMethod = config.method
    body: JsonValue = None
    if config.body is not None:
        body = ctx.render_json(config.body, field_name="webhook.body")
    return HttpCall(
        method=method,
        url=ctx.render_url(config.url, field_name="webhook.url").strip(),
        headers=_rendered_headers(ctx, config.headers),
        body=body,
        timeout_ms=config.timeout_ms,
        retries=config.retries,
        extract=tuple((item.name, item.data) for item in config.response_data),
        source_id=ctx.node.id,
    )


async def _extract(ctx: TurnContext) -> tuple[NodeReport, ...]:
    if not ctx.node.data.extract_vars or ctx.extractor is None:
        return ()
    result = await ctx.extractor.extract(ctx)
    if result is None:
        return ()
    ctx.state.variables.update(result.values)
    return (ExtractionReport(node_id=ctx.node.id, result=result),)


class WebhookHandler:
    """Webhook nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Extraction, speech, the request, variable mapping and the response route."""
        config = ctx.node.data.webhook or WebhookConfig()
        reports = await _extract(ctx)
        speech = queue_speech(ctx, config.speech, field_name="webhook.speech")
        call = _call_of(ctx, config)
        sent = await send_request(ctx, call, purpose="webhook")
        if sent is None:
            _log.warning("engine.http_unavailable", node_id=ctx.node.id)
            return HandlerResult(speech, problem=HTTP_UNAVAILABLE, reports=reports)
        outcome, report = sent
        problem = URL_MISSING if outcome.kind == "invalid_url" and not call.url else None
        variables_set = merge_extracted(ctx, outcome)
        route_to = _choose_route(ctx, config, outcome)
        report = replace(report, variables_set=variables_set, route_to=route_to)
        _log.info(
            "engine.webhook_node",
            node_id=ctx.node.id,
            outcome=outcome.kind,
            status=outcome.status,
            attempts=outcome.attempts,
            route_to=route_to,
        )
        return HandlerResult(speech, problem=problem, route_to=route_to, reports=(*reports, report))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The ``text``/``prompt`` reply (using the response variables); nothing without either."""
        return speak_node(ctx, prompt_required=True)

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Only when the node has a reply (``text``/``prompt``) and not ``skipUserResponse``."""
        return node_waits(node)
