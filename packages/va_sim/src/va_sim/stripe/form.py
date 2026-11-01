"""Stripe bracket-notation form decoding (specs/30 §4.1 Decision P03-T07a rule 2).

Stripe request bodies (and GET query strings) are ``application/x-www-form-urlencoded`` pairs whose keys nest
with brackets: ``metadata[org_id]=o1`` is a mapping, ``line_items[0][quantity]=1`` an indexed list, and
``expand[]=x`` appends to a list. Values stay strings; typed readers live in :mod:`va_sim.stripe.params`.
A mapping whose keys are exactly ``0..n-1`` becomes a list.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

__all__ = ["FormError", "FormValue", "decode_form", "split_key"]

type FormValue = str | list[FormValue] | dict[str, FormValue]
type _Node = str | dict[str, _Node]

_KEY: Final = re.compile(r"([^\[\]]+)((?:\[[^\[\]]*\])*)")
_SEGMENT: Final = re.compile(r"\[([^\[\]]*)\]")


class FormError(ValueError):
    """A key that cannot be decoded, or that conflicts with another key (``param`` is the outer name)."""

    def __init__(self, param: str, message: str) -> None:
        super().__init__(message)
        self.param = param
        self.message = message


def split_key(key: str) -> list[str]:
    """``a[b][0][]`` → ``["a", "b", "0", ""]``; raises :class:`FormError` for malformed brackets."""
    match = _KEY.fullmatch(key)
    if match is None:
        raise FormError(key, f"Invalid parameter name: {key}")
    return [match.group(1), *_SEGMENT.findall(match.group(2))]


def _place(root: dict[str, _Node], key: str, value: str) -> None:
    segments = split_key(key)
    if "" in segments[1:-1]:
        raise FormError(segments[0], f"Invalid parameter name: {key} ([] is allowed only at the end)")
    node = root
    for segment in segments[:-1]:
        child = node.setdefault(segment, {})
        if isinstance(child, str):
            raise FormError(segments[0], f"Invalid parameter: {key} conflicts with a scalar value")
        node = child
    last = segments[-1] if segments[-1] else str(len(node))
    existing = node.get(last)
    if isinstance(existing, dict):
        raise FormError(segments[0], f"Invalid parameter: {key} conflicts with a nested value")
    node[last] = value


def _list_order(node: dict[str, _Node]) -> list[str] | None:
    """The keys in list order when they are exactly ``0..n-1``, else None."""
    if not node or not all(key.isdecimal() for key in node):
        return None
    ordered = sorted(node, key=int)
    return ordered if ordered == [str(index) for index in range(len(node))] else None


def _finish(node: _Node) -> FormValue:
    if isinstance(node, str):
        return node
    order = _list_order(node)
    if order is not None:
        return [_finish(node[key]) for key in order]
    return {key: _finish(child) for key, child in node.items()}


def decode_form(pairs: Sequence[tuple[str, str]]) -> dict[str, FormValue]:
    """Nest decoded ``(key, value)`` pairs; a repeated plain key keeps its last value (as Stripe does)."""
    root: dict[str, _Node] = {}
    for key, value in pairs:
        _place(root, key, value)
    return {key: _finish(child) for key, child in root.items()}
