"""Stripe bracket-notation form decoding and the typed parameter readers (Decision P03-T07a rule 2)."""

from __future__ import annotations

from urllib.parse import parse_qsl

import pytest
from stripe_helpers import encode

from va_sim.stripe.errors import StripeError
from va_sim.stripe.form import FormError, decode_form, split_key
from va_sim.stripe.params import (
    MetadataPatch,
    boolean,
    check_known,
    integer,
    limit,
    mapping,
    metadata,
    string,
)


def decoded(params: dict[str, object]) -> dict[str, object]:
    return dict(decode_form(parse_qsl(encode(params), keep_blank_values=True)))


def test_nested_hashes_and_indexed_lists_round_trip() -> None:
    params: dict[str, object] = {
        "metadata": {"org_id": "o1", "purpose": "topup"},
        "line_items": [{"price_data": {"currency": "usd", "unit_amount": "1000"}, "quantity": "1"}],
        "email": "a@b.test",
    }
    assert decoded(params) == params


def test_empty_bracket_appends_and_empty_values_are_kept() -> None:
    pairs = [("expand[]", "customer"), ("expand[]", "latest_charge"), ("description", "")]
    assert decode_form(pairs) == {"expand": ["customer", "latest_charge"], "description": ""}


def test_out_of_order_indexes_become_an_ordered_list_but_gaps_stay_a_hash() -> None:
    assert decode_form([("a[1]", "y"), ("a[0]", "x")]) == {"a": ["x", "y"]}
    assert decode_form([("a[0]", "x"), ("a[2]", "z")]) == {"a": {"0": "x", "2": "z"}}


def test_a_repeated_key_keeps_the_last_value() -> None:
    assert decode_form([("name", "a"), ("name", "b")]) == {"name": "b"}


@pytest.mark.parametrize(
    "pairs",
    [
        [("a", "1"), ("a[b]", "2")],
        [("a[b]", "2"), ("a", "1")],
        [("a[][b]", "1")],
        [("a[b", "1")],
        [("[a]", "1")],
    ],
)
def test_conflicting_or_malformed_keys_are_rejected(pairs: list[tuple[str, str]]) -> None:
    with pytest.raises(FormError) as caught:
        decode_form(pairs)
    assert caught.value.param


def test_split_key() -> None:
    assert split_key("a[b][0][]") == ["a", "b", "0", ""]


def test_readers() -> None:
    params = decode_form([("amount", "1500"), ("confirm", "true"), ("off", "false"), ("m[k]", "v")])
    assert integer(params, "amount") == 1500
    assert boolean(params, "confirm") is True
    assert boolean(params, "off") is False
    assert boolean(params, "missing") is None
    assert mapping(params, "m") == {"k": "v"}
    assert mapping(decode_form([("m[0]", "v")]), "m") == {"0": "v"}
    assert string(params, "missing") is None
    assert limit({}) == 10


@pytest.mark.parametrize(
    ("pairs", "reader", "code", "param"),
    [
        ([("amount", "12.5")], lambda p: integer(p, "amount"), "parameter_invalid_integer", "amount"),
        ([("confirm", "yes")], lambda p: boolean(p, "confirm"), "parameter_invalid_boolean", "confirm"),
        ([], lambda p: string(p, "currency", required=True), "parameter_missing", "currency"),
        ([("x", "1")], lambda p: check_known(p, frozenset({"y"})), "parameter_unknown", "x"),
        ([("limit", "101")], limit, "parameter_invalid_integer", "limit"),
        ([("limit", "0")], limit, "parameter_invalid_integer", "limit"),
    ],
)
def test_reader_errors(pairs: list[tuple[str, str]], reader: object, code: str, param: str) -> None:
    assert callable(reader)
    with pytest.raises(StripeError) as caught:
        reader(decode_form(pairs))
    assert caught.value.status == 400
    assert caught.value.error["code"] == code
    assert caught.value.error["param"] == param


def test_type_errors_name_the_parameter() -> None:
    with pytest.raises(StripeError, match="must be a string"):
        string(decode_form([("email[x]", "1")]), "email")
    with pytest.raises(StripeError, match="must be a hash"):
        mapping(decode_form([("metadata", "x")]), "metadata")
    with pytest.raises(StripeError, match="values must be strings"):
        metadata(decode_form([("metadata[a][b]", "x")]))
    with pytest.raises(StripeError, match="at most 50"):
        metadata(decode_form([(f"metadata[k{i}]", "v") for i in range(51)]))


def test_metadata_patch_semantics() -> None:
    assert metadata({}) is None
    clear = metadata(decode_form([("metadata", "")]))
    assert clear == MetadataPatch(clear=True)
    assert clear.apply({"a": "1"}) == {}
    patch = metadata(decode_form([("metadata[a]", ""), ("metadata[b]", "2")]))
    assert patch is not None
    assert patch.apply({"a": "1", "c": "3"}) == {"c": "3", "b": "2"}
