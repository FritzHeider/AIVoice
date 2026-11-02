"""Property tests for the templating engine (specs/11 §8 Decision P05-T09a)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from va_engine.templating import (
    MissingVariables,
    TemplateScope,
    TemplateSyntaxError,
    parse_template,
    render,
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
