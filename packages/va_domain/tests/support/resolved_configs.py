"""Resolved Send Call configurations from the api's Bland fixtures, as stored in ``calls.config``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

__all__ = ["RESOLVED_NAMES", "resolved_config"]

_SNAPSHOTS: Final = Path(__file__).resolve().parents[4] / "apps/api/tests/fixtures/bland_send_call_resolved"
RESOLVED_NAMES: Final = tuple(sorted(path.name for path in _SNAPSHOTS.glob("*.json")))


def resolved_config(name: str = "01_task_basic.json") -> dict[str, Any]:
    """The ``config`` of snapshot ``name`` (a fresh dict on every call)."""
    config: dict[str, Any] = json.loads((_SNAPSHOTS / name).read_text(encoding="utf-8"))["config"]
    return config
