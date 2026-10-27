from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, MetaData, Table, Text, UniqueConstraint
from sqlalchemy import Uuid as UuidType
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateIndex, CreateTable

from va_db.base import NAMING_CONVENTION, Base, IdMixin, SoftDeleteMixin, TimestampMixin

_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


class _ProbeBase(DeclarativeBase):
    """A separate registry so probe models never join ``Base.metadata``."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Probe(IdMixin, TimestampMixin, SoftDeleteMixin, _ProbeBase):
    __tablename__ = "probes"

    name: Mapped[str] = mapped_column(Text)


def _probe_table() -> Table:
    table = _Probe.__table__
    assert isinstance(table, Table)
    return table


_TABLE = _probe_table()


def test_base_uses_naming_convention_and_async_attrs() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert issubclass(Base, AsyncAttrs)


def test_id_is_uuid_primary_key_defaulting_to_uuid7() -> None:
    column = _TABLE.c.id
    assert column.primary_key
    assert isinstance(column.type, UuidType)
    assert column.server_default is None
    assert column.default is not None
    generated = column.default.arg(None)  # type: ignore[attr-defined]
    assert isinstance(generated, uuid.UUID)
    assert generated.version == 7


def test_timestamps_are_not_null_timestamptz_defaulting_to_now() -> None:
    for name in ("created_at", "updated_at"):
        column = _TABLE.c[name]
        assert column.type.timezone is True  # type: ignore[attr-defined]
        assert column.nullable is False
        assert str(column.server_default.arg.compile(dialect=_DIALECT)) == "now()"  # type: ignore[union-attr]
    assert _TABLE.c.updated_at.server_onupdate is not None
    assert _TABLE.c.created_at.server_onupdate is None


def test_deleted_at_is_nullable_timestamptz() -> None:
    column = _TABLE.c.deleted_at
    assert column.nullable is True
    assert column.type.timezone is True  # type: ignore[attr-defined]
    assert column.server_default is None


def test_probe_ddl_matches_spec_conventions() -> None:
    ddl = " ".join(str(CreateTable(_TABLE).compile(dialect=_DIALECT)).split())
    assert "id UUID NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "deleted_at TIMESTAMP WITH TIME ZONE," in ddl
    assert "CONSTRAINT probes_pkey PRIMARY KEY (id)" in ddl


def test_naming_convention_matches_postgres_defaults() -> None:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table("orgs", metadata, Column("id", UuidType(), primary_key=True))
    table = Table(
        "agents",
        metadata,
        Column("id", UuidType(), primary_key=True),
        Column("org_id", UuidType(), ForeignKey("orgs.id")),
        Column("slug", Text),
        Column("status", Text),
        UniqueConstraint("org_id", "slug"),
        CheckConstraint("status IN ('a', 'b')", name="status_check"),
    )
    index = Index(None, table.c.org_id, table.c.slug)
    ddl = " ".join(str(CreateTable(table).compile(dialect=_DIALECT)).split())
    assert "CONSTRAINT agents_pkey PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT agents_org_id_fkey FOREIGN KEY(org_id)" in ddl
    assert "CONSTRAINT agents_org_id_slug_key UNIQUE (org_id, slug)" in ddl
    assert "CONSTRAINT agents_status_check CHECK" in ddl
    assert str(CreateIndex(index).compile(dialect=_DIALECT)).startswith(
        "CREATE INDEX agents_org_id_slug_idx ON agents"
    )
