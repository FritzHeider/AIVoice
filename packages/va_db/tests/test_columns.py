from __future__ import annotations

from typing import Any

import pytest
from model_introspection import DIALECT
from sqlalchemy import BigInteger, Boolean, Column, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from va_db.models._columns import (
    bool_column,
    int_column,
    jsonb_column,
    jsonb_default,
    micros_column,
    text_array_column,
    text_column,
)


def _default_sql(column: Column[Any]) -> str:
    return str(column.server_default.arg.compile(dialect=DIALECT))  # type: ignore[union-attr]


def test_jsonb_default_escapes_colons_so_no_bind_parameters_appear() -> None:
    clause = jsonb_default('{"style":0,"use_speaker_boost":true}')
    assert clause.compile(dialect=DIALECT).params == {}
    assert str(clause.compile(dialect=DIALECT)) == '\'{"style":0,"use_speaker_boost":true}\'::jsonb'


def test_builders_produce_not_null_columns_with_spec_defaults() -> None:
    columns = {
        "on": bool_column(default=True).column,
        "off": bool_column(default=False).column,
        "count": int_column(7).column,
        "label": text_column("main").column,
        "langs": text_array_column("{en}").column,
        "payload": jsonb_column().column,
        "settings": jsonb_column("[]").column,
        "price": micros_column(0).column,
        "amount": micros_column().column,
    }
    assert all(column.nullable is False for column in columns.values())
    assert [type(column.type) for column in columns.values()] == [
        Boolean,
        Boolean,
        Integer,
        Text,
        ARRAY,
        JSONB,
        JSONB,
        BigInteger,
        BigInteger,
    ]
    without_default = {"payload", "amount"}
    defaults = {name: _default_sql(column) for name, column in columns.items() if name not in without_default}
    assert defaults == {
        "on": "true",
        "off": "false",
        "count": "7",
        "label": "'main'",
        "langs": "'{en}'",
        "settings": "'[]'::jsonb",
        "price": "0",
    }
    assert all(columns[name].server_default is None for name in without_default)


def test_text_column_rejects_quotes() -> None:
    with pytest.raises(ValueError, match="single quotes"):
        text_column("it's")
