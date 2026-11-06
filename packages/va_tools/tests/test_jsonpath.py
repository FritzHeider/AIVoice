"""JSONPath extraction (specs/13 §5 step 6, Decision P07-T07) and the inline read rule (specs/13 §9
Decision P08-FIX-25)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from jsonpath_ng import JSONPath
from jsonpath_ng.ext.filter import Expression, Filter
from jsonpath_ng.ext.parser import parse
from jsonpath_ng.jsonpath import Child, Fields, Index, Root, Slice, This
from service_helpers import json_log_stream, log_events

import va_tools.jsonpath
from va_tools.jsonpath import (
    INLINE_SCAN_BYTES,
    InvalidJsonPathError,
    compile_path,
    extract,
    extract_all,
    inline_read,
)

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


# The inline read rule.

MIB = 1_048_576
DIRECT = ["$", "$.a.b[0]", "$['a b']", '$["x-y"][2]', "  $.account.status  ", "$._private[12].name"]
DESCENDANT = ["$..x", "$.a[?(@..b)]", "$..*..x", " $.a..b "]
DESCENDING = [
    ("$.a[*]", 1),
    ("$.*", 1),
    ("$['*']", 1),
    ('$["a*"]', 1),
    ("$.a[0:2]", 1),
    ("$.a[::2].b[*].c", 1),
    ("$.a[-1]", 1),
    ("$.a.'b c'", 1),
    (" $ .a ", 1),
    ("$[?(@)]", 2),
    ("$[?(@ >= 0)]", 2),
    ("$.a[?(@.x > 1)]", 3),
    ("$[?(!@.a)]", 3),
    ("$[?(@.a == 'x')].b", 3),
    ("$[?(@.a != null)]", 3),
    ("$[?(@.a == 1 && @.b[0].c <= 2.5)]", 7),
    ("$.a[?(@.b)][?(@.c == true)].d[*]", 5),
    ("$[?(@" + ".a" * 100 + ")]", 102),
]
"""Paths that only go down, with their weight: 1, plus per filter expression 1 and its relative steps."""
REVIEWED = [
    "$[*].$[*]",
    "$.*.$.*",
    "$[*].$[*].$[0]",
    "$[*].($[*])",
    "$[?($[*])]",
    "$[*].`parent`[*]",
    "$[*].`parent`[*].`parent`[*]",
    "$" + "['a','a']" * 20,
]
"""The paths of the P08-FIX-25 review that took 0.5 to 7 s on answers of 121 to 2 001 bytes."""
MULTIPLYING = [
    "$[0,0]",
    "$['a','b']",
    "$.a.`len`",
    "$.a.`sorted`",
    "$.a[/b]",
    "$.a.`this`",
    "$[*].@",
    "@.a",
    "a.b",
    "`this`",
    "$.a + 1",
    "$.a where b",
    "$[?(@.a =~ 'x')]",
    "$[?(@.a == @.b)]",
    "$[?(@.a == $.b)]",
    "$[?(@.a || @.b)]",
    "$[?(@.*)]",
    "$[?(@[*] == 1)]",
    "$[?(@[0:2])]",
    "$[?(@['a','b'])]",
    "$[?(@.a + 1 == 2)]",
    "$[?(@.`len` > 1)]",
    "$[?(1 == 1)]",
    "$[?(@[?(@.a)])]",
]
INVALID = ["", "   ", "$.a b", "$.[[", "$[*,*]"]
UNIONS = ["$" + ".(a|a)" * 20, "$" + ".(*|*)" * 20, "$.a|$.b"]


def test_an_answer_without_paths_is_always_read_inline() -> None:
    assert inline_read(MIB, [])
    assert inline_read(MIB, iter(()))


@pytest.mark.parametrize("path", DIRECT)
def test_a_direct_path_never_counts(path: str) -> None:
    assert inline_read(MIB, [path])
    assert inline_read(MIB, DIRECT)
    assert inline_read(INLINE_SCAN_BYTES, [path, "$.a[*]", path])
    assert not inline_read(INLINE_SCAN_BYTES + 1, [path, "$.a[*]", path])


@pytest.mark.parametrize("path", DESCENDANT)
def test_a_descendant_path_is_never_read_inline(path: str) -> None:
    assert not inline_read(1, [path])
    assert not inline_read(0, [path])
    assert not inline_read(1, ["$.a", path])
    assert not inline_read(1, [path, "$.a"])


@pytest.mark.parametrize(("path", "weight"), DESCENDING)
def test_a_descending_path_counts_the_answer_s_bytes_times_its_weight(path: str, weight: int) -> None:
    at = INLINE_SCAN_BYTES // weight
    assert inline_read(at, [path])
    assert not inline_read(at + 1, [path])
    assert inline_read(at // 2, [path, "$.a", path])
    assert not inline_read(at // 2 + 1, [path, "$.a", path])
    assert inline_read(0, [path] * 50)


def _never_inline(path: str) -> None:
    assert not inline_read(1, [path])
    assert not inline_read(0, [path])
    assert not inline_read(1, ["$.a", path, "$.b[*]"])
    assert not inline_read(MIB, [path])


@pytest.mark.parametrize("path", REVIEWED + MULTIPLYING)
def test_a_path_that_is_not_descending_is_never_read_inline(path: str) -> None:
    compile_path(path)
    _never_inline(path)


@pytest.mark.filterwarnings("ignore:The `|` .union. operator is deprecated:DeprecationWarning")
@pytest.mark.parametrize("path", UNIONS)
def test_a_union_is_never_read_inline(path: str) -> None:
    compile_path(path)
    _never_inline(path)


@pytest.mark.parametrize("path", INVALID)
def test_a_path_that_does_not_compile_is_never_read_inline(path: str) -> None:
    with pytest.raises(InvalidJsonPathError):
        compile_path(path)
    _never_inline(path)


def _parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The texts handed to jsonpath-ng's parser from here on."""
    parsed: list[str] = []

    def recording(text: str) -> JSONPath:
        parsed.append(text)
        return parse(text)

    monkeypatch.setattr(va_tools.jsonpath, "parse", recording)
    return parsed


def test_an_invalid_path_is_parsed_once_for_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """``compile_path`` does not cache a failure; the rule does, so the loop pays one parse per path."""
    parsed = _parses(monkeypatch)
    path = "$.rule.parsed.once[["

    assert [inline_read(1, [path]) for _ in range(3)] == [False] * 3
    assert parsed == [path]


def test_no_path_is_compiled_for_an_answer_too_large_for_any_of_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the threshold the answer goes to the reader whatever the tree says, and the reader compiles."""
    parsed = _parses(monkeypatch)
    paths = ["$.rule.not.compiled[*]", "$.rule.not.compiled[*].$[*]", "$.rule.not.compiled.direct"]

    assert not inline_read(INLINE_SCAN_BYTES + 1, paths[:1])
    assert not inline_read(INLINE_SCAN_BYTES // 2 + 1, paths)
    assert parsed == []
    assert not inline_read(INLINE_SCAN_BYTES // 2, paths)
    assert parsed == paths[:2]


def _names_and_indexes_only(node: JSONPath) -> bool:
    if isinstance(node, Child):
        return _names_and_indexes_only(node.left) and _names_and_indexes_only(node.right)
    if isinstance(node, Fields):
        return len(node.fields) == 1 and node.fields[0] != "*"
    return isinstance(node, Root | Index)


_QUOTED = st.characters(exclude_characters="[]\\*", exclude_categories=["Cs"])
_STEPS = st.one_of(
    st.from_regex(r"\.[A-Za-z_][A-Za-z0-9_]*", fullmatch=True),
    st.integers(min_value=0, max_value=10**9).map(lambda index: f"[{index}]"),
    st.text(_QUOTED.filter(lambda char: char != "'")).map(lambda name: f"['{name}']"),
    st.text(_QUOTED.filter(lambda char: char != '"')).map(lambda name: f'["{name}"]'),
)


@given(st.lists(_STEPS, max_size=6).map(lambda steps: "$" + "".join(steps)))
def test_every_direct_path_compiles_to_names_and_indexes_only(path: str) -> None:
    """What keeps the rule honest against the library: a path the rule calls direct is, to jsonpath-ng,
    one name or index per step (or no path at all), so its cost does not grow with the answer."""
    assume(".." not in path)
    assert inline_read(MIB, [path])
    try:
        tree = compile_path(path)
    except InvalidJsonPathError:
        return
    assert _names_and_indexes_only(tree), repr(tree)


# The descending class against the library: grammar pieces inside and outside the class, in any order.

_PIECES = st.sampled_from(
    [
        ".a",
        ".b",
        ".*",
        "[*]",
        "[0]",
        "[-1]",
        "[1:]",
        "[::2]",
        "['a']",
        "['a','b']",
        "[0,0]",
        "[0,1]",
        ".$",
        ".@",
        ".`parent`",
        ".`len`",
        ".`this`",
        ".`sorted`",
        "[?(@)]",
        "[?(@.a)]",
        "[?(!@.a)]",
        "[?(@.a == 1)]",
        "[?(@ > 0)]",
        "[?(@.a[0].b != null)]",
        "[?(@.a == 'x' && @.b)]",
        "[?(@.a =~ 'x')]",
        "[?(@.a == @.b)]",
        "[?($.a)]",
        "[?($[*])]",
        "[?(@.*)]",
        "[?(@[*] == 1)]",
        "[?(@.a || @.b)]",
        "[?(@.a + 1 == 2)]",
        "[?(@['a','b'])]",
    ]
)
_PATHS = st.builds(
    lambda origin, pieces: origin + "".join(pieces),
    st.sampled_from(["$", "$", "$", "@", "a"]),
    st.lists(_PIECES, max_size=5),
)
_DOCUMENTS = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-2, 2), st.sampled_from(["x", "y"])),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.sampled_from(["a", "b", "c"]), children, max_size=3),
    ),
    max_leaves=25,
)
_TEST_OPERATORS = {None, "!", "==", "=", "!=", "<", "<=", ">", ">="}


def _relative_names_and_indexes(node: JSONPath) -> bool:
    if type(node) is not Child:
        return type(node) is This
    step = node.right
    one = (type(step) is Fields and step.fields not in {(), ("*",)} and len(step.fields) == 1) or (
        type(step) is Index and len(step.indices) == 1
    )
    return one and _relative_names_and_indexes(node.left)


def _plain_test(expression: object) -> bool:
    return (
        type(expression) is Expression
        and expression.op in _TEST_OPERATORS
        and not isinstance(expression.value, JSONPath)
        and _relative_names_and_indexes(expression.target)
    )


def _only_goes_down(node: JSONPath) -> bool:
    """``$`` at the far left, then steps that each select distinct children of what they are given."""
    if type(node) is Root:
        return True
    if type(node) is not Child or not _only_goes_down(node.left):
        return False
    step = node.right
    if type(step) is Fields:
        return len(step.fields) == 1
    if type(step) is Index:
        return len(step.indices) == 1
    if type(step) is Filter:
        return all(_plain_test(expression) for expression in step.expressions)
    return type(step) is Slice


def _values(document: Any) -> int:
    """How many values ``document`` holds (itself included)."""
    if isinstance(document, dict):
        return 1 + sum(_values(value) for value in document.values())
    if isinstance(document, list):
        return 1 + sum(_values(value) for value in document)
    return 1


@given(_PATHS, _DOCUMENTS)
def test_a_path_the_rule_keeps_on_the_loop_only_goes_down(path: str, document: Any) -> None:
    """What keeps the allowlist honest against the library: the tree of a path that may be read on the
    loop holds only the judged nodes, and on any document it selects each value at most once (a path
    that multiplies selects some value twice)."""
    if not inline_read(1, [path]):
        return
    tree = compile_path(path)
    assert _only_goes_down(tree), repr(tree)
    matches = tree.find(document)
    assert len({str(match.full_path) for match in matches}) == len(matches) <= _values(document)


@given(st.lists(_PIECES, min_size=1, max_size=5).map(lambda pieces: "$" + "".join(pieces)))
def test_a_tree_that_only_goes_down_is_what_the_rule_keeps_on_the_loop(path: str) -> None:
    """The other direction, so the class does not shrink unnoticed: a compiled path that only goes down
    is read on the loop at one byte."""
    try:
        tree = compile_path(path)
    except InvalidJsonPathError:
        assert not inline_read(1, [path])
        return
    assert inline_read(1, [path]) is _only_goes_down(tree), repr(tree)
