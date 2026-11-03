"""Node handler test helpers (specs/11 §4, Decision P07-T06).

:func:`handler_ctx` builds a :class:`TurnContext` around one raw node (in a small graph with a start node
and an ``after`` node) with a :class:`FakeDialogue` and :class:`FakeLines`; :func:`spoken` drains the actions
into comparable tuples.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from graph_helpers import graph, node, start

from va_engine.handlers import Channel, LineUnavailableError, TurnContext
from va_engine.models import (
    Action,
    EndCallAction,
    EngineState,
    HumanTakeover,
    Say,
    SayStream,
    TransferAction,
    Wait,
)

__all__ = [
    "CHANNELS",
    "NOW_MS",
    "FakeDialogue",
    "FakeLines",
    "handler_ctx",
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
        builtins={"from": "+14155550100"},
        transfer_list=dict(transfer_list or {}),
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
    return rendered
