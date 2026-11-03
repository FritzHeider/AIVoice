"""Shared rows of the pathway repository tests (specs/12-pathways-api.md)."""

from __future__ import annotations

from typing import Final

from va_db.repositories import PathwayGraphJson

__all__ = ["GRAPH"]

GRAPH: Final = PathwayGraphJson(
    nodes=[{"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "prompt": "Hi"}}],
    edges=[],
    global_config={},
    graph_hash="0" * 64,
)
