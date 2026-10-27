"""Column builders shared by the model modules, so the spec's repetitive column blocks stay one-liners."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, TextClause, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import MappedColumn, mapped_column

__all__ = [
    "bool_column",
    "fk_column",
    "int_column",
    "jsonb_column",
    "jsonb_default",
    "org_fk",
    "org_id_column",
    "text_array_column",
    "text_column",
    "tz",
]

type OnDelete = Literal["CASCADE", "SET NULL", "RESTRICT"]


def tz() -> DateTime:
    """``timestamptz``: the spec has no naive timestamps."""
    return DateTime(timezone=True)


def org_fk() -> ForeignKey:
    """``REFERENCES orgs(id) ON DELETE CASCADE``, carried by every tenant-owned table."""
    return ForeignKey("orgs.id", ondelete="CASCADE")


def org_id_column(*, nullable: bool = False) -> MappedColumn[Any]:
    """``org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE`` (``nullable`` drops the NOT NULL)."""
    return mapped_column(Uuid(as_uuid=True), org_fk(), nullable=nullable)


def fk_column(target: str, ondelete: OnDelete, *, nullable: bool = True) -> MappedColumn[Any]:
    """``uuid REFERENCES <target> ON DELETE <ondelete>``; ``target`` is ``"<table>.id"``."""
    return mapped_column(Uuid(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable)


def bool_column(*, default: bool) -> MappedColumn[Any]:
    """``boolean NOT NULL DEFAULT <default>``."""
    return mapped_column(Boolean, nullable=False, server_default=text("true" if default else "false"))


def int_column(default: int) -> MappedColumn[Any]:
    """``integer NOT NULL DEFAULT <default>``."""
    return mapped_column(Integer, nullable=False, server_default=text(str(default)))


def text_column(default: str) -> MappedColumn[Any]:
    """``text NOT NULL DEFAULT '<default>'``."""
    if "'" in default:
        msg = f"text_column default must not contain single quotes: {default!r}"
        raise ValueError(msg)
    return mapped_column(Text, nullable=False, server_default=text(f"'{default}'"))


def text_array_column(default: str) -> MappedColumn[Any]:
    """``text[] NOT NULL DEFAULT '<default>'``; ``default`` is the array literal, e.g. ``{}`` or ``{en}``."""
    return mapped_column(ARRAY(Text), nullable=False, server_default=text(f"'{default}'"))


def jsonb_default(literal: str) -> TextClause:
    """``DEFAULT '<literal>'::jsonb``; ``literal`` is the JSON text exactly as written in the spec.

    Colons are escaped because :func:`text` would otherwise read ``"style":0`` as a ``:0`` bind parameter.
    """
    escaped = literal.replace(":", "\\:")
    return text(f"'{escaped}'::jsonb")


def jsonb_column(default: str | None = None) -> MappedColumn[Any]:
    """``jsonb NOT NULL``, with ``DEFAULT '<default>'::jsonb`` when ``default`` is given."""
    if default is None:
        return mapped_column(JSONB, nullable=False)
    return mapped_column(JSONB, nullable=False, server_default=jsonb_default(default))
