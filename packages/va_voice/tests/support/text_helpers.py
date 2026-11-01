"""Shared helpers for the va_voice.text tests."""

from __future__ import annotations


def split_at(text: str, cuts: list[int]) -> list[str]:
    """``text`` cut into consecutive deltas at the given offsets (bad offsets ignored)."""
    points = sorted({cut for cut in cuts if cut <= len(text)})
    parts: list[str] = []
    start = 0
    for point in points:
        parts.append(text[start:point])
        start = point
    parts.append(text[start:])
    return parts
