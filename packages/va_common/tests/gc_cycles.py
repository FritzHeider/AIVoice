"""Reference-cycle checks for tests (specs/07-media-pipeline.md §9 Decision P08-FIX-3).

A per-call object that sits on a reference cycle is freed only by the cyclic collector, and a collection
runs synchronously on the event loop. :func:`unreachable_after` records everything a full collection finds
unreachable while the body runs (``gc.DEBUG_SAVEALL``, collector disabled meanwhile), and
:func:`cycles_through` reports, for each object of the named classes, one reference cycle it lies on (a path
of type names). An object that is merely *reachable* from someone else's cycle is not reported: only the
cycles that would keep it alive on their own. :func:`live_cycles_through` asks the same of objects that are
still referenced (a finished component inside a test harness), following object references but never
modules, classes, or module globals.
"""

from __future__ import annotations

import gc
import sys
import types
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

__all__ = ["cycles_through", "live_cycles_through", "unreachable_after"]


@contextmanager
def unreachable_after() -> Iterator[list[object]]:
    """Collect, then disable the collector for the body; on exit the yielded list holds what a full collection
    found unreachable (the objects are kept alive in that list until it is cleared or dropped)."""
    gc.collect()
    found: list[object] = []
    flags = gc.get_debug()
    gc.disable()
    try:
        yield found
        gc.set_debug(flags | gc.DEBUG_SAVEALL)
        gc.collect()
        found.extend(gc.garbage)
    finally:
        gc.garbage.clear()
        gc.set_debug(flags)
        gc.enable()


def cycles_through(garbage: Iterable[object], class_names: Iterable[str]) -> dict[str, list[str]]:
    """``{class name: cycle}`` for the first object of each named class on a cycle in ``garbage``."""
    items = list(garbage)
    members = {id(item) for item in items}
    wanted = set(class_names)
    found: dict[str, list[str]] = {}
    for item in items:
        name = type(item).__name__
        if name in wanted and name not in found:
            cycle = _cycle_from(item, members)
            if cycle is not None:
                found[name] = cycle
    return found


def live_cycles_through(
    root: object, class_names: Iterable[str], *, limit: int = 200_000
) -> dict[str, list[str]]:
    """``{class name: cycle}`` for the objects of the named classes reachable from ``root`` (itself included)
    that lie on a cycle of instance references (modules, classes, and module globals are not followed)."""
    wanted = set(class_names)
    graph = _instance_graph(root, limit)
    members = set(graph)
    found: dict[str, list[str]] = {}
    for item in graph.values():
        name = type(item).__name__
        if name in wanted and name not in found:
            cycle = _cycle_from(item, members)
            if cycle is not None:
                found[name] = cycle
    return found


def _instance_graph(root: object, limit: int) -> dict[int, object]:
    """Every object reachable from ``root`` through instance references (at most ``limit`` objects)."""
    module_dicts = {id(vars(module)) for module in list(sys.modules.values()) if module is not None}
    graph: dict[int, object] = {id(root): root}
    queue: deque[object] = deque([root])
    while queue and len(graph) < limit:
        for referent in gc.get_referents(queue.popleft()):
            if id(referent) in graph or _opaque(referent, module_dicts):
                continue
            graph[id(referent)] = referent
            queue.append(referent)
    return graph


def _opaque(item: object, module_dicts: set[int]) -> bool:
    return isinstance(item, types.ModuleType | type) or id(item) in module_dicts


def _cycle_from(start: object, members: set[int]) -> list[str] | None:
    """The shortest path ``start -> ... -> start`` through ``members`` (breadth first), as type names."""
    parents: dict[int, object] = {}
    queue: deque[object] = deque([start])
    seen = {id(start)}
    while queue:
        node = queue.popleft()
        for referent in gc.get_referents(node):
            if referent is start:
                path = [type(node).__name__]
                current = node
                while current is not start:
                    current = parents[id(current)]
                    path.append(type(current).__name__)
                path.reverse()
                return [*path, type(start).__name__]
            if id(referent) in members and id(referent) not in seen:
                seen.add(id(referent))
                parents[id(referent)] = node
                queue.append(referent)
    return None
