"""Property tests for the templating engine (specs/11 §8 Decisions P05-T09a and P07-T02)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from va_engine.templating import (
    MissingVariables,
    TemplateScope,
    TemplateSyntaxError,
    parse_template,
    render,
    render_json,
    render_url,
)

_LITERAL = st.text(alphabet=st.characters(blacklist_characters="{\\"), max_size=200)
_IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,8}", fullmatch=True)
_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_IDENT, children, max_size=3),
    max_leaves=8,
)


@given(_LITERAL)
def test_literal_text_round_trips(text: str) -> None:
    assert render(parse_template(text), TemplateScope()) == text


@given(_LITERAL, _LITERAL)
def test_escaped_braces_are_literal(before: str, after: str) -> None:
    source = f"{before}\\{{{{{after}"

    assert render(parse_template(source), TemplateScope()) == f"{before}{{{{{after}"


@given(st.text(max_size=120), st.dictionaries(_IDENT, _VALUES, max_size=5))
def test_parsed_templates_always_render(source: str, variables: dict[str, object]) -> None:
    try:
        template = parse_template(source)
    except TemplateSyntaxError as error:
        assert 0 <= error.position <= len(source)
        return
    values = TemplateScope(variables=variables, missing=MissingVariables())

    assert isinstance(render(template, values), str)
    assert isinstance(render_url(template, values), str)


@given(_IDENT, st.text(max_size=40))
def test_string_values_render_verbatim(name: str, value: str) -> None:
    assert (
        render(parse_template(f"<{{{{ {name} }}}}>"), TemplateScope(variables={name: value})) == f"<{value}>"
    )


# ------------------------------------------------------------------------------------------------
# Grammar-driven fuzzing and hostile values (specs/11 §8 Decision P07-T02)

_PLAIN = st.text(alphabet=st.characters(blacklist_characters="{\\"), max_size=20)
_WS = st.text(alphabet=" \t\r\n", max_size=2)
_QUOTE_BODY = st.text(max_size=12)
_TIMEZONES = st.sampled_from(["UTC", "America/New_York", "Asia/Tokyo", "Pacific/Kiritimati", "Nope/Zone"])


def _quoted(body: str, quote: str) -> str:
    escaped = body.replace("\\", "\\\\").replace(quote, "\\" + quote)
    return f"{quote}{escaped}{quote}"


@st.composite
def _paths(draw: st.DrawFn) -> str:
    step = st.one_of(_IDENT.map(lambda n: f".{n}"), st.integers(0, 12).map(lambda i: f"[{i}]"))
    return draw(_IDENT) + "".join(draw(st.lists(step, max_size=3)))


@st.composite
def _filters(draw: st.DrawFn) -> str:
    name = draw(
        st.sampled_from(["upper", "lower", "title", "json", "phone_spoken", "trim", "default", "date"])
    )
    if name in {"default", "date"}:
        arg = _quoted(draw(_QUOTE_BODY), draw(st.sampled_from(['"', "'"])))
        return f"{name}:{draw(_WS)}{arg}"
    return name


@st.composite
def _expressions(draw: st.DrawFn) -> str:
    filters = "".join(f"{draw(_WS)}|{draw(_WS)}{draw(_filters())}" for _ in range(draw(st.integers(0, 3))))
    return f"{{{{{draw(_WS)}{draw(_paths())}{filters}{draw(_WS)}}}}}"


_SEGMENTS = st.one_of(_PLAIN, st.just("\\{{"), st.just("}}"), _expressions())
_TEMPLATES = st.lists(_SEGMENTS, max_size=6).map("".join)

_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=2**63, max_value=2**200),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=20),
    st.datetimes(timezones=st.none() | st.timezones()).map(lambda moment: moment.isoformat()),
    st.sampled_from(["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59-05:00", "+14155550100", "+44"]),
)
_KEYS = st.one_of(_IDENT, st.integers(-5, 5), st.booleans(), st.none())
_HOSTILE = st.recursive(
    _SCALARS,
    lambda children: (
        st.lists(children, max_size=3)
        | st.tuples(children, children)
        | st.dictionaries(_KEYS, children, max_size=3)
    ),
    max_leaves=10,
)
_NAMES = st.sampled_from(["a", "b", "x", "name", "items", "when"])


@given(_TEMPLATES)
def test_grammar_templates_always_parse(source: str) -> None:
    assert parse_template(source).source == source


@settings(max_examples=200, deadline=None)
@given(_TEMPLATES, st.dictionaries(_NAMES, _HOSTILE, max_size=4), _TIMEZONES)
def test_rendering_never_raises_for_hostile_values(
    source: str, variables: dict[str, object], timezone: str
) -> None:
    values = TemplateScope(
        variables=variables,
        request_data={"x": variables.get("a")},
        timezone=timezone,
        missing=MissingVariables(),
    )

    assert isinstance(render(source, values), str)
    assert isinstance(render_url(source, values), str)
    rendered = render_json({"k": [source]}, values)
    assert isinstance(rendered, dict)


@given(
    st.text(alphabet=st.sampled_from("{}|\\:'\"[]. a1_\n"), max_size=60),
    st.dictionaries(_NAMES, _HOSTILE, max_size=3),
)
def test_arbitrary_text_raises_only_syntax_errors(source: str, variables: dict[str, object]) -> None:
    try:
        rendered = render(source, TemplateScope(variables=variables))
    except TemplateSyntaxError as error:
        assert 0 <= error.position <= len(source)
    else:
        assert isinstance(rendered, str)


@given(st.lists(_paths(), min_size=1, max_size=6))
def test_missing_paths_are_reported_once(paths: list[str]) -> None:
    reported: list[str] = []
    source = "".join(f"{{{{{path}}}}}" for path in paths)
    values = TemplateScope(missing=MissingVariables(reported.append))

    render(source, values)
    render(source, values)

    assert len(reported) == len(set(reported))
    assert set(reported) == {parse_template(f"{{{{{path}}}}}").expressions[0].path_text for path in paths}
