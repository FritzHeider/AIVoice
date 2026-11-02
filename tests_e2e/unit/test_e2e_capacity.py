"""e2e_capacity: the call count of the opt-in capacity flow."""

from __future__ import annotations

import pytest
from e2e_capacity import CONCURRENT_CALLS_ENV, DEFAULT_CONCURRENT_CALLS, concurrent_calls


def test_default_when_unset_or_blank() -> None:
    assert concurrent_calls({}) == DEFAULT_CONCURRENT_CALLS == 50
    assert concurrent_calls({CONCURRENT_CALLS_ENV: "  "}) == DEFAULT_CONCURRENT_CALLS


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("10", 10), (" 50 ", 50)])
def test_valid_counts(raw: str, expected: int) -> None:
    assert concurrent_calls({CONCURRENT_CALLS_ENV: raw}) == expected


@pytest.mark.parametrize("raw", ["0", "51", "-3", "x", "2.5"])
def test_out_of_range_or_not_an_integer_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=CONCURRENT_CALLS_ENV) as caught:
        concurrent_calls({CONCURRENT_CALLS_ENV: raw})
    assert repr(raw) in str(caught.value)
