from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Column, MetaData, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from va_db.base import NAMING_CONVENTION
from va_db.models._checks import check_in

_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def test_check_in_renders_in_list_and_column_name() -> None:
    constraint = check_in("status", ("active", "suspended", "closed"))
    assert isinstance(constraint, CheckConstraint)
    assert constraint.name == "status_check"
    assert str(constraint.sqltext) == "status IN ('active', 'suspended', 'closed')"


def test_check_in_name_follows_postgres_inline_check_naming() -> None:
    table = Table(
        "things",
        MetaData(naming_convention=NAMING_CONVENTION),
        Column("kind", Text),
        check_in("kind", ("a",)),
    )
    ddl = " ".join(str(CreateTable(table).compile(dialect=_DIALECT)).split())
    assert "CONSTRAINT things_kind_check CHECK (kind IN ('a'))" in ddl


def test_check_in_rejects_empty_values() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        check_in("status", ())


def test_check_in_rejects_quotes() -> None:
    with pytest.raises(ValueError, match="single quotes"):
        check_in("status", ("ok", "it's"))
