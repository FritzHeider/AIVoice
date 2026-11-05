"""Tool request rendering (specs/13 §5 step 3, Decision P08-T02a)."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from va_engine.templating import TemplateScope
from va_tools.models import ToolSpec
from va_tools.render import (
    EXCERPT_LIMIT,
    ToolRenderError,
    excerpt,
    merge_query,
    render_request,
    shown_headers,
)


def _spec(**fields: Any) -> ToolSpec:
    values: dict[str, Any] = {
        "tool_id": "TL-0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71",
        "name": "check",
        "description": "Checks.",
        "speech": None,
        "url": "https://api.acme.test/check",
        "method": "POST",
    }
    return ToolSpec(**{**values, **fields})


def _scope(**args: Any) -> TemplateScope:
    return TemplateScope(
        variables={"city": "São Paulo", "qty": 3},
        builtins={"from": "+14155550100"},
        input={"input": args},
    )


def test_body_keeps_types_of_lone_expressions_and_renders_text() -> None:
    spec = _spec(
        body={
            "party": "{{input.party_size}}",
            "label": "Party of {{input.party_size}}",
            "caller": "{{from}}",
            "items": ["{{qty}}", {"vip": "{{input.vip}}"}],
            "missing": "{{input.nope}}",
        }
    )
    call = render_request(spec, _scope(party_size=4, vip=True)).call
    assert call.body == {
        "party": 4,
        "label": "Party of 4",
        "caller": "+14155550100",
        "items": [3, {"vip": True}],
        "missing": "",
    }


def test_url_values_are_percent_encoded_and_literal_text_kept() -> None:
    spec = _spec(url="https://api.acme.test/cities/{{city}}/search?q={{input.q}}", method="GET")
    call = render_request(spec, _scope(q="a&b=c")).call
    assert call.url == "https://api.acme.test/cities/S%C3%A3o%20Paulo/search?q=a%26b%3Dc"


def test_query_is_merged_and_overrides_url_pairs() -> None:
    spec = _spec(
        url="https://api.acme.test/s?page=1&tag=a&tag=b&keep=",
        query={"tag": "{{input.tag}}", "lang": "en us"},
    )
    call = render_request(spec, _scope(tag="x/y")).call
    assert call.url == "https://api.acme.test/s?page=1&keep=&tag=x%2Fy&lang=en+us"


def test_merge_query_without_query_or_with_an_unparseable_url_keeps_the_url() -> None:
    assert merge_query("https://a.test/p?x=1#frag", {}) == "https://a.test/p?x=1#frag"
    assert merge_query("https://a.test/p#frag", {"y": "2"}) == "https://a.test/p?y=2#frag"
    assert merge_query("https://[::1/p", {"y": "2"}) == "https://[::1/p"


def test_secret_headers_are_sent_verbatim_win_collisions_and_are_masked() -> None:
    secret = f"Bearer {secrets.token_hex(16)}"
    spec = _spec(
        headers={"X-Org": "{{city}}", "authorization": "{{input.auth}}", "X-Api-Key": "plain"},
        secret_headers={"Authorization": secret, "X-Raw": "{{not_a_template"},
    )
    rendered = render_request(spec, _scope(auth="leak"))
    assert rendered.call.headers == {
        "X-Org": "São Paulo",
        "X-Api-Key": "plain",
        "Authorization": secret,
        "X-Raw": "{{not_a_template",
    }
    assert rendered.shown_headers == {
        "X-Org": "São Paulo",
        "X-Api-Key": "••••",
        "Authorization": "••••",
        "X-Raw": "••••",
    }


def test_inline_tool_headers_are_all_masked() -> None:
    spec = _spec(tool_id=None, headers={"X-Org": "acme", "Accept": "application/json"})
    rendered = render_request(spec, _scope())
    assert rendered.call.headers == {"X-Org": "acme", "Accept": "application/json"}
    assert rendered.shown_headers == {"X-Org": "••••", "Accept": "••••"}
    assert rendered.call.source_id == "check"


def test_call_carries_the_tool_settings() -> None:
    spec = _spec(timeout_ms=2500, retries=2, response_map={"a": "$.a", "b": "$.b[*]"}, method="PUT")
    call = render_request(spec, _scope()).call
    assert (call.method, call.timeout_ms, call.retries) == ("PUT", 2500, 2)
    assert call.extract == (("a", "$.a"), ("b", "$.b[*]"))
    assert call.body is None
    assert call.source_id == spec.tool_id


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"url": "https://a.test/{{"}, "url"),
        ({"query": {"page": "{{ | }}"}}, "query.page"),
        ({"headers": {"X-A": "{{a|nope}}"}}, "headers.X-A"),
        ({"body": {"a": ["{{"]}}, "body"),
    ],
)
def test_unparseable_templates_name_the_field(fields: dict[str, Any], field: str) -> None:
    with pytest.raises(ToolRenderError) as caught:
        render_request(_spec(**fields), _scope())
    assert caught.value.field == field


def test_shown_headers_masks_secret_named_keys() -> None:
    assert shown_headers({"Cookie": "c", "Accept": "a", "X-Org": "o"}, frozenset({"x-org"})) == {
        "Cookie": "••••",
        "Accept": "a",
        "X-Org": "••••",
    }


def test_excerpt_cuts_with_an_ellipsis() -> None:
    assert excerpt(None) is None
    assert excerpt({"a": 1}) == '{"a":1}'
    long = "x" * (EXCERPT_LIMIT + 5)
    cut = excerpt(long)
    assert cut is not None
    assert len(cut) == EXCERPT_LIMIT
    assert cut.endswith("…")
    assert excerpt("x" * EXCERPT_LIMIT) == "x" * EXCERPT_LIMIT
