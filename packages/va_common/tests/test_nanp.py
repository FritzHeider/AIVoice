"""`va_common.nanp`: area codes, the committed NPA -> state table, and same-state checks."""

from __future__ import annotations

import json
import re
from importlib import resources

import pytest

from va_common.nanp import npa, same_state, state_for_npa


@pytest.mark.parametrize(
    ("area_code", "state"),
    [("415", "CA"), ("212", "NY"), ("416", "ON"), ("787", "PR"), ("202", "DC"), ("800", None), ("199", None)],
)
def test_state_for_npa(area_code: str, state: str | None) -> None:
    assert state_for_npa(area_code) == state


@pytest.mark.parametrize(
    ("e164", "expected"),
    [
        ("+14155550100", "415"),
        ("+442071234567", None),
        ("+1415555010", None),
        ("+141555501000", None),
        ("+1415555010a", None),
        ("14155550100", None),
    ],
)
def test_npa(e164: str, expected: str | None) -> None:
    assert npa(e164) == expected


def test_same_state() -> None:
    assert same_state("+14155550100", "+16505550100")
    assert not same_state("+14155550100", "+12125550100")
    assert not same_state("+18005550100", "+18885550100")
    assert not same_state("+442071234567", "+442071234568")


def test_table_shape() -> None:
    raw = resources.files("va_common").joinpath("data", "npa_states.json").read_text(encoding="utf-8")
    table = json.loads(raw)
    assert raw.endswith("}\n")
    assert list(table) == sorted(table)
    assert all(re.fullmatch(r"[2-9]\d\d", key) for key in table)
    assert all(re.fullmatch(r"[A-Z]{2}", value) for value in table.values())
