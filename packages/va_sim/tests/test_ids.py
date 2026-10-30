"""SimIds: seeded, reproducible identifiers."""

from __future__ import annotations

import random
import uuid

import pytest

from va_sim.ids import SimIds


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - deterministic ids, not cryptography


def test_same_seed_same_ids() -> None:
    one, two = SimIds(_rng(4)), SimIds(_rng(4))
    assert [one.uuid(), one.token(), one.digits()] == [two.uuid(), two.token(), two.digits()]


def test_id_formats() -> None:
    ids = SimIds(_rng(0))
    assert uuid.UUID(ids.uuid()).version == 4
    token = ids.token(24)
    assert len(token) == 32
    assert "=" not in token
    numeric = ids.digits()
    assert len(numeric) == 19
    assert numeric.isdigit()
    assert numeric[0] != "0"
    assert len(ids.digits(1)) == 1


def test_digits_rejects_empty_ids() -> None:
    with pytest.raises(ValueError, match="at least one digit"):
        SimIds(_rng(0)).digits(0)
