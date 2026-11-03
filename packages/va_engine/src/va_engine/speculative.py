"""Speculative dialogue generation (specs/11 §5.8; Decision P07-T08a rules 5-6).

While the turn's decision calls run, :meth:`Speculation.start` opens a speculative dialogue stream on the
current node (prompt assembled as if staying); its deltas stay buffered in the
:class:`~va_llm.dialogue.DialogueStream`. :meth:`Speculation.resolve` then commits it (the
:class:`~va_engine.dialogue.GatewayDialogue` relays it on the next ``stream``) when the turn stays on the
node, no loop-condition suffix was added and no variable the prompt references changed; otherwise it is
discarded (the HTTP stream is closed) and the node's handler opens a fresh request. Hit/miss counting is
the gateway's ``va_llm_speculation_total``.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Final, Literal

from va_common.logging import get_logger
from va_engine.dialogue import GatewayDialogue
from va_engine.handlers.base import TurnContext, has_content
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayNode
from va_engine.prompt import instruction_templates
from va_engine.templating import TemplateSyntaxError, cached_template
from va_llm.dialogue import DialogueStream

__all__ = [
    "PROMPTED_STREAM_TYPES",
    "STREAM_TYPES",
    "Speculation",
    "SpeculationOutcome",
    "referenced_variables",
    "speculation_allowed",
]

type SpeculationOutcome = Literal["committed", "discarded"]
type DiscardReason = Literal["route_changed", "variables_changed", "prompt_changed", "closed"]

STREAM_TYPES: Final[frozenset[str]] = frozenset({"Default", "Scheduling"})
"""Node types whose reply always streams from the dialogue model (when they have no static ``text``)."""
PROMPTED_STREAM_TYPES: Final[frozenset[str]] = frozenset({"Wait for Response", "Webhook", "SMS"})
"""Node types whose reply streams only when they have a non-blank ``prompt``."""

_ABSENT: Final = object()
_log = get_logger(__name__)


def referenced_variables(template_texts: Iterable[str]) -> frozenset[str] | None:
    """The first path segment of every expression in ``template_texts``; ``None`` when one does not parse
    (callers then assume it references everything)."""
    names: set[str] = set()
    for text in template_texts:
        try:
            template = cached_template(text)
        except TemplateSyntaxError:
            return None
        names.update(str(expression.path[0]) for expression in template.expressions)
    return frozenset(names)


def _streams_reply(node: PathwayNode) -> bool:
    if node.type in STREAM_TYPES:
        return True
    return node.type in PROMPTED_STREAM_TYPES and has_content(node.data.prompt)


def speculation_allowed(node: PathwayNode, graph: PathwayGraph, *, enabled: bool) -> bool:
    """Whether a speculative reply may run at ``node`` (rule 5): the org allows it, the node streams a
    dialogue reply without static ``text``, and no ``before_response`` extraction variable is referenced by
    the instructions (background extraction never blocks)."""
    if not enabled or has_content(node.data.text) or not _streams_reply(node):
        return False
    data = node.data
    if not data.extract_vars or data.extract_vars_mode != "before_response":
        return True
    referenced = referenced_variables(instruction_templates(node, graph))
    if referenced is None:
        return False
    return not referenced & {variable.name for variable in data.extract_vars}


class Speculation:
    """One speculative reply; build it with :meth:`start`."""

    def __init__(
        self,
        dialogue: GatewayDialogue,
        stream: DialogueStream,
        *,
        node_id: str,
        referenced: frozenset[str] | None,
        snapshot: Mapping[str, object],
    ) -> None:
        self._dialogue = dialogue
        self._stream = stream
        self._node_id = node_id
        self._referenced = referenced
        self._snapshot = snapshot
        self._outcome: SpeculationOutcome | None = None
        self._reason: DiscardReason | None = None

    @classmethod
    async def start(cls, dialogue: GatewayDialogue, ctx: TurnContext) -> Speculation:
        """Open the speculative stream at the context's node and remember the referenced variables."""
        referenced = referenced_variables(instruction_templates(ctx.node, ctx.graph))
        stream = await dialogue.open(ctx, speculative=True)
        return cls(
            dialogue,
            stream,
            node_id=ctx.node.id,
            referenced=referenced,
            snapshot=_snapshot(ctx.state.variables, referenced),
        )

    @property
    def outcome(self) -> SpeculationOutcome | None:
        """``None`` until resolved."""
        return self._outcome

    @property
    def reason(self) -> str | None:
        """Why it was discarded (``None`` when committed or unresolved)."""
        return self._reason

    @property
    def stream(self) -> DialogueStream:
        return self._stream

    async def resolve(
        self, *, stays: bool, variables: Mapping[str, object], prompt_changed: bool = False
    ) -> DialogueStream | None:
        """Commit (and hand the stream to the dialogue port) or discard; a second call does nothing."""
        if self._outcome is not None:
            return self._stream if self._outcome == "committed" else None
        reason = self._discard_reason(stays=stays, variables=variables, prompt_changed=prompt_changed)
        if reason is not None:
            await self._discard(reason)
            return None
        self._outcome = "committed"
        self._stream.commit()
        self._dialogue.adopt(self._stream)
        _log.info("engine.speculation", node_id=self._node_id, outcome="committed", reason=None)
        return self._stream

    async def aclose(self) -> None:
        """Discard the stream when it was never resolved."""
        if self._outcome is None:
            await self._discard("closed")

    def _discard_reason(
        self, *, stays: bool, variables: Mapping[str, object], prompt_changed: bool
    ) -> DiscardReason | None:
        if not stays:
            return "route_changed"
        if prompt_changed:
            return "prompt_changed"
        if _snapshot(variables, self._referenced) != self._snapshot:
            return "variables_changed"
        return None

    async def _discard(self, reason: DiscardReason) -> None:
        self._outcome = "discarded"
        self._reason = reason
        await self._stream.discard()
        await self._stream.aclose()
        _log.info("engine.speculation", node_id=self._node_id, outcome="discarded", reason=reason)


def _snapshot(variables: Mapping[str, object], referenced: frozenset[str] | None) -> dict[str, object]:
    """A deep copy of the referenced variables (every variable when ``referenced`` is ``None``)."""
    if referenced is None:
        return copy.deepcopy(dict(variables))
    return {name: copy.deepcopy(variables[name]) if name in variables else _ABSENT for name in referenced}
