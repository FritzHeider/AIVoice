"""Node handler test helpers (specs/11 §4, Decisions P07-T06 and P07-T07).

:func:`handler_ctx` builds a :class:`TurnContext` around one raw node (in a small graph with a start node
and an ``after`` node) with a :class:`FakeDialogue` and :class:`FakeLines`; :func:`spoken` drains the actions
into comparable tuples. The port fakes (:class:`FakeHttp`, :class:`FakeSms`, :class:`FakeExtractor`,
:class:`RecordingSink`) can share one ``order`` log to assert what happened first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from graph_helpers import graph, node, start

from va_engine.extraction import ExtractionResult
from va_engine.handlers import Channel, LineUnavailableError, TurnContext
from va_engine.models import (
    Action,
    EndCallAction,
    EngineState,
    HumanTakeover,
    Say,
    SayStream,
    SendDtmfAction,
    SwitchGraph,
    TransferAction,
    Wait,
)
from va_engine.ports import (
    ActionSink,
    ExtractionPort,
    HttpCall,
    HttpOutcome,
    HttpOutcomeKind,
    HttpPort,
    SmsOutcome,
    SmsPort,
    SmsRequest,
)

__all__ = [
    "CHANNELS",
    "NOW_MS",
    "FakeDialogue",
    "FakeExtractor",
    "FakeHttp",
    "FakeLines",
    "FakeSms",
    "RecordingSink",
    "handler_ctx",
    "ok",
    "spoken",
    "unavailable",
]

NOW_MS: Final = 1_000
CHANNELS: Final[tuple[Channel, ...]] = ("voice", "sms", "widget_chat", "widget_voice")


@dataclass(slots=True)
class FakeDialogue:
    """Streams ``tokens``; records the node id of every started stream."""

    tokens: tuple[str, ...] = ("Hi ", "there")
    streamed: list[str] = field(default_factory=list)

    def stream(self, ctx: TurnContext) -> AsyncIterator[str]:
        return self._tokens(ctx.node.id)

    async def _tokens(self, node_id: str) -> AsyncIterator[str]:
        self.streamed.append(node_id)
        for token in self.tokens:
            yield token


@dataclass(slots=True)
class FakeLines:
    """Answers ``reply`` (or raises it when it is an exception); records every instruction."""

    reply: str | Exception = "Composed line."
    instructions: list[str] = field(default_factory=list)

    async def compose(self, ctx: TurnContext, instruction: str) -> str:
        del ctx
        self.instructions.append(instruction)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def ok(body: Any = None, *, status: int = 200, **extracted: Any) -> HttpOutcome:
    """A response outcome (``ok`` for 2xx, else ``http_error``)."""
    kind: HttpOutcomeKind = "ok" if 200 <= status < 300 else "http_error"
    return HttpOutcome(kind=kind, status=status, body=body, extracted=extracted, attempts=1, duration_ms=42)


@dataclass(slots=True)
class FakeHttp:
    """Answers each call with the next scripted outcome (or ``answer(call)``); records calls."""

    outcomes: list[HttpOutcome] = field(default_factory=list)
    answer: Callable[[HttpCall], HttpOutcome] | None = None
    calls: list[HttpCall] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    async def execute(self, call: HttpCall) -> HttpOutcome:
        self.calls.append(call)
        self.order.append(f"http:{call.method}")
        if self.answer is not None:
            return self.answer(call)
        return self.outcomes.pop(0) if self.outcomes else ok()


@dataclass(slots=True)
class FakeSms:
    """Answers ``outcome``; records requests."""

    outcome: SmsOutcome = field(default_factory=lambda: SmsOutcome(sent=True, message_id="msg-1"))
    requests: list[SmsRequest] = field(default_factory=list)

    async def send(self, request: SmsRequest) -> SmsOutcome:
        self.requests.append(request)
        return self.outcome


@dataclass(slots=True)
class FakeExtractor:
    """Extracts ``values`` (``None`` = nothing to extract); logs into ``order``."""

    values: dict[str, Any] | None = None
    order: list[str] = field(default_factory=list)

    async def extract(self, ctx: TurnContext) -> ExtractionResult | None:
        self.order.append(f"extract:{ctx.node.id}")
        if self.values is None:
            return None
        return ExtractionResult(values=dict(self.values))


@dataclass(slots=True)
class RecordingSink:
    """Records queued actions; logs into ``order``."""

    actions: list[Action] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def queue(self, action: Action) -> None:
        self.actions.append(action)
        self.order.append(f"sink:{type(action).__name__}")


def unavailable() -> LineUnavailableError:
    return LineUnavailableError("llm_unavailable")


def handler_ctx(  # noqa: PLR0913 - test knobs, all keyword-only
    raw_node: dict[str, Any],
    *,
    channel: Channel = "voice",
    variables: Mapping[str, Any] | None = None,
    transfer_list: Mapping[str, str] | None = None,
    global_config: dict[str, Any] | None = None,
    extra_nodes: list[dict[str, Any]] | None = None,
    dialogue: FakeDialogue | None = None,
    lines: FakeLines | None = None,
    turn_index: int = 3,
    http: HttpPort | None = None,
    sms: SmsPort | None = None,
    extractor: ExtractionPort | None = None,
    sink: ActionSink | None = None,
    now: datetime | None = None,
    builtins: Mapping[str, Any] | None = None,
    timezone: str = "UTC",
) -> TurnContext:
    """A context whose current node is ``raw_node`` (graph: ``start``, the node, ``after``, extras)."""
    nodes = [start(), raw_node, node("after"), *(extra_nodes or [])]
    loaded = graph(nodes, [], global_config)
    current = loaded.node(raw_node["id"])
    assert current is not None
    state = EngineState(
        runner="pathway",
        pathway_id=None,
        pathway_version=None,
        current_node_id=current.id,
        variables=dict(variables or {}),
        turn_index=turn_index,
    )
    return TurnContext(
        state=state,
        graph=loaded,
        node=current,
        channel=channel,
        dialogue=dialogue or FakeDialogue(),
        lines=lines or FakeLines(),
        now_ms=lambda: NOW_MS,
        request_data={"account": "A-7"},
        builtins=dict(builtins) if builtins is not None else {"from": "+14155550100"},
        timezone=timezone,
        transfer_list=dict(transfer_list or {}),
        http=http,
        sms=sms,
        extractor=extractor,
        sink=sink,
        clock_now=(lambda: now) if now is not None else None,
    )


async def spoken(actions: tuple[Action, ...]) -> list[tuple[Any, ...]]:
    """``actions`` as tuples; streams are read to the end."""
    rendered: list[tuple[Any, ...]] = []
    for action in actions:
        match action:
            case Say():
                rendered.append(("say", action.text, action.mark, action.interruptible))
            case SayStream():
                text = "".join([token async for token in action.tokens])
                rendered.append(("stream", text, action.mark, action.interruptible))
            case Wait():
                rendered.append(("wait", action.seconds))
            case TransferAction():
                rendered.append(
                    ("transfer", action.mode, action.target, action.caller_id_mode, action.after_mark)
                )
            case EndCallAction():
                rendered.append(("end", action.reason, action.ended_by, action.after_mark))
            case HumanTakeover():
                rendered.append(("takeover", action.after_mark))
            case SendDtmfAction():
                rendered.append(("dtmf", action.digits, action.duration_ms, action.after_mark))
            case SwitchGraph():
                rendered.append(
                    (
                        "switch",
                        action.pathway_id,
                        action.version,
                        action.node_id,
                        action.carry_variables,
                        action.return_on_end,
                    )
                )
    return rendered
