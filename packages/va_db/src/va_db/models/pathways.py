"""Conversational Pathways: folders, pathways, versions, unit tests, test runs (specs/03-data-model.md §5).

Follows the conventions of :mod:`va_db.models.tenancy`. Per the §1 decision, every tenant table has an
``org_id`` index.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy import CheckConstraint, Index, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import fk_column, int_column, jsonb_column, org_id_column, text_column, tz

__all__ = [
    "Pathway",
    "PathwayFolder",
    "PathwayTestRun",
    "PathwayTestRunKind",
    "PathwayTestRunStatus",
    "PathwayUnitTest",
    "PathwayVersion",
    "VersionStatus",
]

# Shared by ``pathway_versions.status`` and ``persona_versions.status``.
VersionStatus = Literal["draft", "published", "archived"]
PathwayTestRunKind = Literal["unit", "simulation", "regression"]
PathwayTestRunStatus = Literal["queued", "running", "passed", "failed", "error"]


class PathwayFolder(IdMixin, TimestampMixin, Base):
    """``pathway_folders``: a dashboard folder; deleting one deletes its sub-folders."""

    __tablename__ = "pathway_folders"
    __table_args__ = (Index("pathway_folders_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = fk_column("pathway_folders.id", "CASCADE")


class Pathway(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``pathways``: a conversation graph; the ``*_version`` columns are ``version_number`` pins."""

    __tablename__ = "pathways"
    __table_args__ = (Index("pathways_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    folder_id: Mapped[uuid.UUID | None] = fk_column("pathway_folders.id", "SET NULL")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    # version_number pinned to production.
    production_version: Mapped[int | None] = mapped_column(Integer)
    staging_version: Mapped[int | None] = mapped_column(Integer)
    # The single editable draft.
    draft_version: Mapped[int] = int_column(1)
    latest_version: Mapped[int] = int_column(1)


class PathwayVersion(IdMixin, TimestampMixin, Base):
    """``pathway_versions``: ``nodes`` is ``PathwayNode[]``, ``edges`` is ``PathwayEdge[]``, and
    ``global_config`` is a ``GlobalConfig`` (specs/11 §3); ``graph_hash`` is the sha256 of the canonical JSON.
    """

    __tablename__ = "pathway_versions"
    __table_args__ = (
        check_in("status", get_args(VersionStatus)),
        UniqueConstraint("pathway_id", "version_number"),
        Index("pathway_versions_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    pathway_id: Mapped[uuid.UUID] = fk_column("pathways.id", "CASCADE", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional label, e.g. "v3 - new greeting".
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[VersionStatus] = mapped_column(Text, nullable=False)
    nodes: Mapped[list[Any]] = jsonb_column("[]")
    edges: Mapped[list[Any]] = jsonb_column("[]")
    global_config: Mapped[dict[str, Any]] = jsonb_column("{}")
    graph_hash: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(tz())
    published_by: Mapped[uuid.UUID | None] = fk_column("users.id", "SET NULL")


class PathwayUnitTest(IdMixin, TimestampMixin, Base):
    """``pathway_unit_tests``: ``history`` is ``[{role, content}]`` before ``user_message``; ``expected`` is
    ``{"route_to_node_id"?, "variables"?, "grading_goal"?}``.

    ``source_call_id`` names the call the test was captured from; like the spec, it has no foreign key.
    """

    __tablename__ = "pathway_unit_tests"
    __table_args__ = (
        CheckConstraint("simulated_variations BETWEEN 0 AND 20", name="simulated_variations_check"),
        Index("pathway_unit_tests_pathway_idx", "pathway_id"),
        Index("pathway_unit_tests_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    pathway_id: Mapped[uuid.UUID] = fk_column("pathways.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    history: Mapped[list[Any]] = jsonb_column()
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[dict[str, Any]] = jsonb_column()
    simulated_variations: Mapped[int] = int_column(5)


class PathwayTestRun(IdMixin, TimestampMixin, Base):
    """``pathway_test_runs``: one unit, simulation, or regression run of a pathway version."""

    __tablename__ = "pathway_test_runs"
    __table_args__ = (
        check_in("kind", get_args(PathwayTestRunKind)),
        check_in("status", get_args(PathwayTestRunStatus)),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    pathway_id: Mapped[uuid.UUID] = fk_column("pathways.id", "CASCADE", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[PathwayTestRunKind] = mapped_column(Text, nullable=False)
    status: Mapped[PathwayTestRunStatus] = mapped_column(Text, nullable=False)
    results: Mapped[list[Any]] = jsonb_column("[]")
    started_at: Mapped[datetime | None] = mapped_column(tz())
    finished_at: Mapped[datetime | None] = mapped_column(tz())


Index("pathway_test_runs_pathway_idx", PathwayTestRun.pathway_id, PathwayTestRun.created_at.desc())
Index("pathway_test_runs_org_idx", PathwayTestRun.org_id)
