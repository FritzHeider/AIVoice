"""The tools of a pathway call, resolved per node (specs/13 §4; Decisions P08-FIX-17 and P08-T02c2).

:class:`PathwayCallExecutor` is the :class:`va_engine.tool_executor.PathwayToolExecutor` of a call. It knows
the stored tools the call's graphs reference (:meth:`PathwayCallExecutor.add`; the media worker loads them
when a graph is loaded) and hands every node a :class:`~va_tools.executor.CallToolExecutor` over exactly the
tools that node's refs name (:meth:`PathwayCallExecutor.for_refs`):

* a string ref names a stored tool (``TL-<uuid>`` or a bare uuid, :func:`tool_ref_id`); an id the call does
  not know (deleted, another org's, not a tool id at all) resolves to nothing;
* a mapping ref is an inline tool, validated as a Send Call inline tool
  (:class:`va_domain.calls.tools.InlineTool`); one that does not validate resolves to nothing (logged
  ``tools.inline_tool_invalid`` once per call, with the number of errors only), and so does one that cannot be
  written as JSON (an integer beyond 64 bits; logged once per call for all such refs, ``errors=1``);
* tools named like a system tool are dropped, and of several refs with one tool name the first wins (logged
  ``tools.tool_skipped`` when another definition is shadowed).

Every node executor shares the call's :class:`~va_tools.executor.SystemRuns`: the executor-backed system
tools run wherever the conversation is, a run one node left behind is known to the next node's executor, and
:meth:`PathwayCallExecutor.aclose` drains them once at call end. The call's tool speech (quoted by the tool
usage note) is the first non-blank ``speech`` among the tools the refs given to
:meth:`~PathwayCallExecutor.add` resolve to, in ref order; once set it stays. The engine reads it before
every reply pass, so a speech a graph loaded later brought reaches the note from that graph's first reply on.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Final

from pydantic import ValidationError

from va_common.ids import PublicPrefix, from_public, is_public
from va_common.json import dumps
from va_common.logging import get_logger
from va_domain.calls.tools import InlineTool
from va_engine.models.graph import PathwayGraph
from va_engine.system_tools import is_reserved
from va_engine.tool_executor import ToolExecutor, ToolRef
from va_tools.executor import SYSTEM_DRAIN_S, CallToolExecutor, SystemRuns, SystemTool, ToolEvents
from va_tools.models import ToolSpec
from va_tools.runner import ToolRunner

__all__ = ["PathwayCallExecutor", "graph_tool_refs", "tool_ref_id"]

_BARE_UUID: Final = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

_log = get_logger(__name__)


def tool_ref_id(ref: str) -> uuid.UUID | None:
    """The stored tool a string ref names: ``TL-<uuid>`` or a bare uuid (``None`` otherwise)."""
    if is_public(ref, PublicPrefix.TOOL):
        return from_public(ref, PublicPrefix.TOOL)
    if _BARE_UUID.fullmatch(ref):
        return uuid.UUID(ref)
    return None


def graph_tool_refs(graph: PathwayGraph) -> list[ToolRef]:
    """Every node's ``tools`` in node order, then ``globalConfig.globalTools``."""
    refs: list[ToolRef] = [ref for node in graph.nodes for ref in node.data.tools]
    refs.extend(graph.global_config.global_tools)
    return refs


def _speech(spec: ToolSpec | None) -> str | None:
    if spec is None or not spec.speech or not spec.speech.strip():
        return None
    return spec.speech


class PathwayCallExecutor:
    """The custom and executor-backed system tools of one pathway call (see the module docstring)."""

    def __init__(
        self,
        *,
        runner: ToolRunner,
        events: ToolEvents | None = None,
        system: Sequence[SystemTool] = (),
    ) -> None:
        self._runner = runner
        self._events = events
        self._runs = SystemRuns(system, events=events)
        self._stored: dict[uuid.UUID, ToolSpec] = {}
        self._inline: dict[bytes, ToolSpec | None] = {}
        self._unwritable_logged = False
        self._tool_speech: str | None = None

    def add(self, stored: Iterable[ToolSpec], refs: Sequence[ToolRef] = ()) -> None:
        """Make ``stored`` tools resolvable (keyed by their id); while the call has no tool speech it becomes
        the first non-blank ``speech`` among the tools ``refs`` resolve to, in ref order."""
        for spec in stored:
            tool_id = None if spec.tool_id is None else tool_ref_id(spec.tool_id)
            if tool_id is not None:
                self._stored.setdefault(tool_id, spec)
        if self._tool_speech is None:
            speeches = (_speech(self._resolve(ref)) for ref in refs)
            self._tool_speech = next((speech for speech in speeches if speech is not None), None)

    def knows(self, tool_id: uuid.UUID) -> bool:
        """Whether the stored tool ``tool_id`` was added."""
        return tool_id in self._stored

    @property
    def tool_speech(self) -> str | None:
        return self._tool_speech

    def handles(self, name: str) -> bool:
        return self._runs.handles(name)

    def for_refs(self, refs: Sequence[ToolRef]) -> ToolExecutor:
        kept: dict[str, ToolSpec] = {}
        for ref in refs:
            spec = self._resolve(ref)
            if spec is None:
                continue
            first = kept.get(spec.name)
            if first is None:
                kept[spec.name] = spec
            elif first is not spec:  # the same tool listed twice (node and global) is not worth a line
                _log.info("tools.tool_skipped", tool=spec.name, reason="duplicate", inline=spec.inline)
        return CallToolExecutor(
            list(kept.values()), runner=self._runner, events=self._events, system=self._runs
        )

    async def aclose(self, timeout_s: float = SYSTEM_DRAIN_S) -> None:
        """Wait up to ``timeout_s`` for the call's unfinished system tool runs, then cancel the rest
        (:meth:`~va_tools.executor.SystemRuns.aclose`); never raises."""
        await self._runs.aclose(timeout_s)

    def _resolve(self, ref: ToolRef) -> ToolSpec | None:
        """The tool ``ref`` names (``None``: unknown, invalid, or named like a system tool)."""
        if isinstance(ref, str):
            tool_id = tool_ref_id(ref)
            spec = None if tool_id is None else self._stored.get(tool_id)
        else:
            spec = self._inline_spec(ref)
        if spec is not None and is_reserved(spec.name):
            return None
        return spec

    def _inline_spec(self, ref: ToolRef) -> ToolSpec | None:
        """The inline tool of a mapping ref, parsed once per call (``None`` when it does not validate, or
        cannot be written as JSON: such a ref has no cache key and could not be sent either)."""
        try:
            key = dumps(ref, sort_keys=True)
        except TypeError:
            if not self._unwritable_logged:
                self._unwritable_logged = True
                _log.warning("tools.inline_tool_invalid", errors=1)
            return None
        if key in self._inline:
            return self._inline[key]
        spec: ToolSpec | None
        try:
            spec = ToolSpec.from_inline(InlineTool.model_validate(ref))
        except ValidationError as exc:
            _log.warning("tools.inline_tool_invalid", errors=exc.error_count())
            spec = None
        if spec is not None and is_reserved(spec.name):
            _log.info("tools.tool_skipped", tool=spec.name, reason="reserved", inline=True)
        self._inline[key] = spec
        return spec
