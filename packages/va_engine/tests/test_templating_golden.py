"""Golden templating cases and spec drift checks (specs/11 §8, Decisions P05-T09a and P07-T02)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from va_engine.templating import (
    MissingVariables,
    Template,
    TemplateScope,
    TemplateSyntaxError,
    cached_template,
    render,
    render_json,
    render_url,
)

_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = Path(__file__).parent / "fixtures" / "templating" / "golden.json"
_SPEC = _ROOT / "specs" / "11-pathways-engine.md"
_CASES: list[dict[str, Any]] = json.loads(_GOLDEN.read_text(encoding="utf-8"))
_SCOPE_KEYS = frozenset({"variables", "request_data", "builtins", "node_vars", "input", "timezone"})


def _scope(raw: dict[str, Any], missing: MissingVariables) -> TemplateScope:
    unknown = set(raw) - _SCOPE_KEYS
    assert not unknown, f"unknown scope keys {unknown}"
    return TemplateScope(**raw, missing=missing)


def _render_case(case: dict[str, Any], scope: TemplateScope) -> object:
    mode = case["mode"]
    if mode == "json":
        return render_json(case["template"], scope)
    if mode == "url":
        return render_url(case["template"], scope)
    assert mode == "text"
    return render(case["template"], scope)


def test_golden_file_is_well_formed() -> None:
    names = [case["name"] for case in _CASES]

    assert len(names) == len(set(names))
    assert len(names) >= 30
    for case in _CASES:
        assert ("expected" in case) != ("error" in case), case["name"]


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_golden_case(case: dict[str, Any]) -> None:
    reported: list[str] = []
    scope = _scope(case["scope"], MissingVariables(reported.append))

    if "error" in case:
        with pytest.raises(TemplateSyntaxError) as caught:
            _render_case(case, scope)
        assert caught.value.message == case["error"]["message"]
        assert caught.value.position == case["error"]["position"]
        return

    assert _render_case(case, scope) == case["expected"]
    assert reported == case.get("missing", [])


def _section_8() -> str:
    text = _SPEC.read_text(encoding="utf-8")
    start = text.index("## 8. Templating engine")
    return text[start : text.index("## 9.", start)]


_PHONE_EXAMPLE = re.compile(r"`(\+\d+)` → `?\"?([\d ,]+?)\"?`")


def test_spec_phone_examples_are_golden() -> None:
    examples = _PHONE_EXAMPLE.findall(_section_8())
    golden = {
        case["scope"]["variables"]["p"]: case["expected"]
        for case in _CASES
        if case["template"] == "{{p|phone_spoken}}"
    }

    assert len(examples) >= 2
    for number, spoken in examples:
        assert golden.get(number) == spoken, number
        assert render("{{p|phone_spoken}}", TemplateScope(variables={"p": number})) == spoken


def test_spec_json_example_is_golden() -> None:
    assert '(`"{{qty}}"` with qty=3 → `3`)' in _section_8()
    assert render_json("{{qty}}", TemplateScope(variables={"qty": 3})) == 3


def test_render_accepts_text_and_caches_the_parse() -> None:
    cached_template.cache_clear()
    values = TemplateScope(variables={"a": "x y"})

    assert render("{{a}}!", values) == "x y!"
    assert render_url("{{a}}!", values) == "x%20y!"
    assert cached_template.cache_info().hits == 1
    assert cached_template("{{a}}!") is cached_template("{{a}}!")
    assert isinstance(cached_template("{{a}}!"), Template)


def test_render_text_syntax_errors_raise_and_are_not_cached() -> None:
    cached_template.cache_clear()

    for _ in range(2):
        with pytest.raises(TemplateSyntaxError):
            render("{{ broken", TemplateScope())

    assert cached_template.cache_info().currsize == 0
