"""CHECK constraint builders that keep Python ``Literal`` types and SQL enumerations in lockstep."""

from __future__ import annotations

from sqlalchemy import CheckConstraint

__all__ = ["check_in"]


def check_in(column: str, values: tuple[str, ...]) -> CheckConstraint:
    """Return ``CHECK (<column> IN ('a', 'b', ...))``, named ``<table>_<column>_check`` by the convention.

    Callers pass ``typing.get_args(SomeLiteral)`` so the ``Literal`` alias is the single source of truth.
    """
    if not values:
        msg = f"check_in({column!r}) needs at least one value"
        raise ValueError(msg)
    if any("'" in value for value in values):
        msg = f"check_in({column!r}) values must not contain single quotes: {values!r}"
        raise ValueError(msg)
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=f"{column}_check")
