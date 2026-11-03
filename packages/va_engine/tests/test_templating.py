"""Templating grammar, resolution, filters, and rendering modes (specs/11 §8 Decision P05-T09a)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from va_engine.templating import (
    MAX_EXPRESSIONS,
    MAX_TEMPLATE_CHARS,
    Expression,
    Filter,
    MissingVariables,
    TemplateLimitError,
    TemplateScope,
    TemplateSyntaxError,
    TextSegment,
    parse_template,
    render,
    render_json,
    render_text,
    render_url,
    stringify,
    zone_named,
)


def scope(**over: Any) -> TemplateScope:
    return TemplateScope(**over)


def text(template: str, **over: Any) -> str:
    return render_text(template, scope(**over))


# ------------------------------------------------------------------------------------------------
# Grammar


@pytest.mark.parametrize("source", ["{{name}}", "{{ name }}", "{{\tname\n}}", "{{  name|upper  }}"])
def test_whitespace_is_allowed_around_tokens(source: str) -> None:
    template = parse_template(source)

    assert template.expressions[0].path == ("name",)
    assert template.single_expression is not None


def test_paths_mix_keys_and_indexes() -> None:
    expression = parse_template("{{ a.b[0].c[12] }}").expressions[0]

    assert expression.path == ("a", "b", 0, "c", 12)
    assert expression.path_text == "a.b[0].c[12]"


def test_filters_chain_with_arguments() -> None:
    expression = parse_template('{{ x | default:"a\\"b" | trim|upper | date:\'%Y\' }}').expressions[0]

    assert expression.filters == (
        Filter("default", 'a"b'),
        Filter("trim"),
        Filter("upper"),
        Filter("date", "%Y"),
    )


def test_segments_keep_literal_text_and_order() -> None:
    template = parse_template("Hi {{name}}, bye")

    assert template.segments == (TextSegment("Hi "), Expression(("name",)), TextSegment(", bye"))
    assert template.single_expression is None


def test_escaped_braces_render_literally() -> None:
    assert text("\\{{name}} {{name}}", variables={"name": "Al"}) == "{{name}} Al"
    assert text("a\\\\{{x}}") == "a\\{{x}}"


def test_closing_braces_and_backslashes_outside_expressions_are_literal() -> None:
    assert text("}} a\\b {single}") == "}} a\\b {single}"


def test_quoted_argument_escapes() -> None:
    assert text("{{ x | default:'it\\'s \\\\ ok' }}") == "it's \\ ok"


@pytest.mark.parametrize(
    ("source", "message", "position"),
    [
        ("ab {{ name", "unterminated expression", 3),
        ("{{", "unterminated expression", 0),
        ("x {{ }}", "empty expression", 2),
        ("{{ 1abc }}", "expected variable name", 3),
        ("{{ a. }}", "expected name after '.'", 5),
        ("{{ a[x] }}", "expected a list index", 5),
        ("{{ a[-1] }}", "expected a list index", 5),
        ("{{ a[1 }}", "unclosed '['", 4),
        ("{{ a | shout }}", "unknown filter 'shout'", 7),
        ("{{ a | }}", "expected filter name", 7),
        ("{{ a | default }}", "filter 'default' needs a quoted argument", 14),
        ("{{ a | date:%Y }}", "expected a quoted string", 12),
        ('{{ a | default:"open }}', "unterminated string", 15),
        ("{{ a b }}", "expected '}}'", 5),
        ("{{ a }", "expected '}}'", 5),
    ],
)
def test_syntax_errors_carry_a_position(source: str, message: str, position: int) -> None:
    with pytest.raises(TemplateSyntaxError) as caught:
        parse_template(source)

    assert caught.value.message == message
    assert caught.value.position == position
    assert str(caught.value) == f"{message} at position {position}"
    assert not isinstance(caught.value, TemplateLimitError)


def test_template_length_limit() -> None:
    parse_template("a" * MAX_TEMPLATE_CHARS)

    with pytest.raises(TemplateLimitError):
        parse_template("a" * (MAX_TEMPLATE_CHARS + 1))


def test_expression_count_limit() -> None:
    assert len(parse_template("{{a}}" * MAX_EXPRESSIONS).expressions) == MAX_EXPRESSIONS

    with pytest.raises(TemplateLimitError) as caught:
        parse_template("{{a}}" * (MAX_EXPRESSIONS + 1))

    assert caught.value.position == MAX_EXPRESSIONS * 5


# ------------------------------------------------------------------------------------------------
# Resolution


def test_resolution_order() -> None:
    layers = {
        "input": {"v": "input"},
        "node_vars": {"v": "node", "w": "node"},
        "variables": {"v": "var", "w": "var", "x": "var"},
        "request_data": {"v": "rd", "w": "rd", "x": "rd", "y": "rd"},
        "builtins": {"v": "bi", "w": "bi", "x": "bi", "y": "bi", "z": "bi"},
    }

    assert text("{{v}} {{w}} {{x}} {{y}} {{z}}", **layers) == "input node var rd bi"


def test_input_and_node_layers_apply_only_when_given() -> None:
    assert text("{{v}}", variables={"v": "var"}, builtins={"input": "bi"}) == "var"
    assert text("{{input}}", builtins={"input": "bi"}) == "bi"


def test_request_data_is_addressable_by_name() -> None:
    values = {"request_data": {"city": "Austin", "zip": 78701}}

    assert text("{{city}} {{request_data.city}} {{request_data.zip}}", **values) == "Austin Austin 78701"
    assert text("{{request_data}}", **values) == '{"city":"Austin","zip":78701}'


def test_metadata_is_a_builtin_mapping() -> None:
    assert text("{{metadata.k}}", builtins={"metadata": {"k": "v"}}) == "v"


def test_stored_null_is_found_and_renders_empty() -> None:
    missing = MissingVariables()

    assert text("[{{a}}]", variables={"a": None}, builtins={"a": "bi"}, missing=missing) == "[]"
    assert text("{{a|default:'d'}}", variables={"a": None}) == "d"
    assert missing.paths == frozenset()


@pytest.mark.parametrize(
    "path",
    ["{{d.a[5]}}", "{{d.a.b}}", "{{d.k[0]}}", "{{d.k.z}}", "{{d.n.x}}", "{{nothing.x}}", "{{d.a[0][0]}}"],
)
def test_misses_inside_paths_render_empty_and_report(path: str) -> None:
    missing = MissingVariables()
    data = {"d": {"a": [1, 2], "k": "text", "n": None}}

    assert text(path, variables=data, missing=missing) == ""
    assert missing.paths == frozenset({parse_template(path).expressions[0].path_text})


def test_nested_lookup() -> None:
    assert text("{{d.a[1].b}}", variables={"d": {"a": [0, {"b": "deep"}]}}) == "deep"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("s", "s"),
        (None, ""),
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (2.5, "2.5"),
        ([1, "a"], '[1,"a"]'),
        ({"a": {"b": None}}, '{"a":{"b":null}}'),
        (date(2026, 11, 2), '"2026-11-02"'),
    ],
)
def test_stringify(value: object, expected: str) -> None:
    assert stringify(value) == expected


class _Opaque:
    def __str__(self) -> str:
        return "opaque"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2**70, str(2**70)),
        (-(2**65), str(-(2**65))),
        ({1: 2}, '{"1":2}'),
        ({True: 1, None: 2}, '{"true":1,"null":2}'),
        (float("nan"), "null"),
        ([float("inf"), 1.5], "[null,1.5]"),
        ({"n": 2**70, 3: [float("-inf")], False: _Opaque()}, f'{{"n":{2**70},"3":[null],"false":"opaque"}}'),
        ((2**70, None, {None: True}), f'[{2**70},null,{{"null":true}}]'),
        ({2.5: {"s"}}, '{"2.5":"{\'s\'}"}'),
        (_Opaque(), '"opaque"'),
    ],
)
def test_stringify_never_raises(value: object, expected: str) -> None:
    assert stringify(value) == expected


# ------------------------------------------------------------------------------------------------
# Filters


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{s|upper}}", "  MIXED CASE  "),
        ("{{s|lower}}", "  mixed case  "),
        ("{{s|title}}", "  Mixed Case  "),
        ("{{s|trim}}", "MiXed case"),
        ("{{s|trim|upper}}", "MIXED CASE"),
        ("{{n|upper}}", "42"),
    ],
)
def test_text_filters(source: str, expected: str) -> None:
    assert text(source, variables={"s": "  MiXed case  ", "n": 42}) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "d"), ("", "d"), ("x", "x"), (0, "0"), (False, "false")],
)
def test_default_replaces_missing_none_and_empty(value: object, expected: str) -> None:
    assert text("{{v|default:'d'}}", variables={"v": value}) == expected


def test_default_suppresses_the_missing_report() -> None:
    missing = MissingVariables()

    assert text("{{gone|upper|default:'fallback'}}", missing=missing) == "fallback"
    assert text("{{gone|default:'x'|upper}}", missing=missing) == "X"
    assert missing.paths == frozenset()


def test_filters_on_missing_values_still_report() -> None:
    missing = MissingVariables()

    assert text("{{gone|json}}{{gone|upper}}{{other|date:'%Y'}}", missing=missing) == ""
    assert missing.paths == frozenset({"gone", "other"})


def test_json_filter() -> None:
    values = {"variables": {"d": {"a": [1, True]}, "s": 'say "hi"'}}

    assert text("{{d|json}}", **values) == '{"a":[1,true]}'
    assert text("{{s|json}}", **values) == '"say \\"hi\\""'


def test_json_filter_handles_big_integers_and_non_string_keys() -> None:
    values = {"variables": {"n": 2**70, "d": {1: [2**64]}}}

    assert text("{{n|json}} {{d|json}}", **values) == f'{2**70} {{"1":[{2**64}]}}'


@pytest.mark.parametrize(
    ("number", "spoken"),
    [
        ("+14155550100", "4 1 5, 5 5 5, 0 1 0 0"),
        ("4155550100", "4 1 5, 5 5 5, 0 1 0 0"),
        ("+442071838750", "0 2 0, 7 1 8 3, 8 7 5 0"),
        ("12-34", "1 2 3 4"),
        ("ext", "ext"),
        ("+1 41", "1 4 1"),
    ],
)
def test_phone_spoken(number: str, spoken: str) -> None:
    assert text("{{p|phone_spoken}}", variables={"p": number}) == spoken


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        ("2026-11-02T15:30:00+00:00", "%B %d %H:%M", "November 02 10:30"),
        ("2026-07-04T12:00:00Z", "%H:%M %Z", "08:00 EDT"),
        ("2026-11-02T15:30:00", "%H:%M", "15:30"),
        ("2026-11-02", "%A", "Monday"),
        ("not a date", "%Y", "not a date"),
        (20261102, "%Y", "20261102"),
    ],
)
def test_date_filter_uses_the_scope_timezone(value: object, fmt: str, expected: str) -> None:
    assert text(f"{{{{v|date:'{fmt}'}}}}", variables={"v": value}, timezone="America/New_York") == expected


def test_date_filter_accepts_date_objects_and_unknown_zones() -> None:
    values: dict[str, object] = {"d": date(2026, 1, 2), "t": datetime(2026, 1, 2, 3, 4, tzinfo=UTC)}

    assert (
        text("{{d|date:'%d/%m'}} {{t|date:'%H:%M'}}", variables=values, timezone="Nowhere/City")
        == "02/01 03:04"
    )
    assert zone_named("Nowhere/City").key == "UTC"
    assert zone_named("../etc").key == "UTC"


class _Unformattable(date):
    def strftime(self, fmt: str) -> str:
        raise ValueError(fmt)


@pytest.mark.parametrize(
    ("value", "timezone"),
    [
        ("0001-01-01T00:00:00+00:00", "America/New_York"),
        ("9999-12-31T23:59:59-05:00", "Asia/Tokyo"),
        (datetime(1, 1, 1, tzinfo=UTC), "Pacific/Honolulu"),
    ],
)
def test_date_filter_overflow_keeps_the_value(value: object, timezone: str) -> None:
    rendered = text("{{v|date:'%Y'}}", variables={"v": value}, timezone=timezone)

    assert rendered == stringify(value)


def test_date_filter_keeps_the_value_when_strftime_fails() -> None:
    assert text("{{v|date:'%Y'}}", variables={"v": _Unformattable(2026, 11, 2)}) == '"2026-11-02"'


# ------------------------------------------------------------------------------------------------
# Missing variables


def test_missing_reported_once_per_distinct_path_across_renders() -> None:
    heard: list[str] = []
    shared = scope(missing=MissingVariables(heard.append))
    template = parse_template("{{a}} {{b.c}} {{a}}")

    assert render(template, shared) == "  "
    assert render(template, shared) == "  "
    assert render_text("{{ b.c }} {{ b }}", shared) == " "
    assert heard == ["a", "b.c", "b"]


def test_missing_without_a_tracker_renders_empty() -> None:
    assert text("[{{a}}]") == "[]"


# ------------------------------------------------------------------------------------------------
# Modes


def test_render_url_encodes_values_only() -> None:
    template = parse_template("https://h.test/a b?q={{q}}&n={{n}}")

    assert (
        render_url(template, scope(variables={"q": "a b&c/", "n": 5}))
        == "https://h.test/a b?q=a%20b%26c%2F&n=5"
    )


def test_render_json_preserves_types_of_single_expressions() -> None:
    values = scope(
        variables={"qty": 3, "flag": True, "items": [1, 2], "obj": {"k": "v"}, "none": None, "name": "Al"},
        missing=MissingVariables(),
    )
    body = {
        "qty": "{{qty}}",
        "flag": "{{ flag }}",
        "items": "{{items}}",
        "obj": "{{obj}}",
        "none": "{{none}}",
        "text": "x {{qty}}",
        "padded": " {{qty}}",
        "filtered": "{{qty|upper}}",
        "gone": "{{gone}}",
        "{{name}}": ["{{name}}", {"deep": "{{qty}}"}, 7, None, False],
    }

    assert render_json(body, values) == {
        "qty": 3,
        "flag": True,
        "items": [1, 2],
        "obj": {"k": "v"},
        "none": None,
        "text": "x 3",
        "padded": " 3",
        "filtered": "3",
        "gone": "",
        "{{name}}": ["Al", {"deep": 3}, 7, None, False],
    }


def test_render_json_turns_tuples_into_lists() -> None:
    assert render_json(("{{a}}", 1), scope(variables={"a": "x"})) == ["x", 1]


def test_render_json_raises_for_invalid_templates() -> None:
    with pytest.raises(TemplateSyntaxError):
        render_json({"a": "{{"}, scope())
