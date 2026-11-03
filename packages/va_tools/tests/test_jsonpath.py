"""JSONPath extraction (specs/13 §5 step 6, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

import pytest
from service_helpers import json_log_stream, log_events

from va_tools.jsonpath import InvalidJsonPathError, compile_path, extract, extract_all

DOCUMENT: dict[str, Any] = {
    "account": {"status": "active", "tier": None},
    "items": [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 3}, {"sku": "C", "qty": 5}],
    "tags": ["x"],
}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("$.account.status", "active"),
        ("$.account.tier", None),
        ("$.items[0].sku", "A"),
        ("$.items[*].sku", ["A", "B", "C"]),
        ("$.items[?(@.qty > 1)].sku", ["B", "C"]),
        ("$.items[?(@.qty > 4)].sku", "C"),
        ("$.tags", ["x"]),
        ("$.missing", None),
        ("$.items[9].sku", None),
        ("  $.account.status  ", "active"),
    ],
)
def test_extract(path: str, value: Any) -> None:
    assert extract(DOCUMENT, path) == value


@pytest.mark.parametrize("path", ["", "   ", "$.[[", "$..?("])
def test_invalid_paths_give_none_and_are_logged_without_the_path(path: str) -> None:
    stream = json_log_stream()

    assert extract(DOCUMENT, path) is None

    events = [event for event in log_events(stream) if event["event"] == "tools.jsonpath_invalid"]
    assert events == [{**events[0], "length": len(path)}]
    with pytest.raises(InvalidJsonPathError):
        compile_path(path)


def test_scalar_documents_and_failing_filters() -> None:
    assert extract("text", "$.a") is None
    assert extract(None, "$.a") is None
    assert extract({"items": [{"qty": "x"}]}, "$.items[?(@.qty > 1)]") is None


def test_evaluation_errors_give_none_and_are_logged() -> None:
    stream = json_log_stream()

    assert extract({"a": ["x"]}, '$.a[?(@ =~ "[")]') is None

    failed = [event for event in log_events(stream) if event["event"] == "tools.jsonpath_failed"]
    assert failed[0]["error"] == "error"


def test_extract_all_maps_every_name() -> None:
    pairs = [("status", "$.account.status"), ("skus", "$.items[*].sku"), ("none", "$.nope")]
    assert extract_all(DOCUMENT, pairs) == {"status": "active", "skus": ["A", "B", "C"], "none": None}


def test_compiled_paths_are_cached() -> None:
    compile_path.cache_clear()
    extract(DOCUMENT, "$.account.status")
    extract(DOCUMENT, "$.account.status")
    info = compile_path.cache_info()
    assert (info.hits, info.misses) == (1, 1)
