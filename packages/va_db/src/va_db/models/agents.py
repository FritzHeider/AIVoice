"""Personas, agents, experiments, dispositions, citations, evals, widgets (specs/03-data-model.md §5).

Follows the conventions of :mod:`va_db.models.tenancy`. Per the §1 decision, every tenant table has an
``org_id`` index (or a UNIQUE constraint that starts with ``org_id``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

# jscpd:ignore-start  (import lists of the model modules overlap by nature)
from sqlalchemy import CheckConstraint, Index, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    int_column,
    jsonb_column,
    org_id_column,
    text_array_column,
    text_column,
    tz,
)
from va_db.models.pathways import VersionStatus

# jscpd:ignore-end  (end of the shared import lists)

__all__ = [
    "Agent",
    "AgentVersion",
    "CitationSchema",
    "Disposition",
    "EvalSetup",
    "EvalSetupVersion",
    "Experiment",
    "ExperimentArm",
    "ExperimentStatus",
    "Persona",
    "PersonaVersion",
    "Widget",
]

ExperimentStatus = Literal["draft", "running", "stopped"]


class Persona(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``personas``: a versioned identity (voice, tone, rules) reused across agents."""

    __tablename__ = "personas"
    __table_args__ = (Index("personas_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    production_version: Mapped[int | None] = mapped_column(Integer)
    draft_version: Mapped[int] = int_column(1)
    latest_version: Mapped[int] = int_column(1)


class PersonaVersion(IdMixin, TimestampMixin, Base):
    """``persona_versions``: ``config`` is a ``PersonaConfig`` (specs/20 §3)."""

    __tablename__ = "persona_versions"
    __table_args__ = (
        check_in("status", get_args(VersionStatus)),
        UniqueConstraint("persona_id", "version_number"),
        Index("persona_versions_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    persona_id: Mapped[uuid.UUID] = fk_column("personas.id", "CASCADE", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = jsonb_column()
    published_at: Mapped[datetime | None] = mapped_column(tz())


class Agent(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``agents``: ``env_pins`` maps environments to semvers, e.g. ``{"production": "1.2.0"}``."""

    __tablename__ = "agents"
    __table_args__ = (Index("agents_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    env_pins: Mapped[dict[str, Any]] = jsonb_column("{}")


class AgentVersion(IdMixin, TimestampMixin, Base):
    """``agent_versions``: an immutable semver release; ``call_settings`` holds Send Call parameter defaults.

    A pathway referenced by a version cannot be deleted (``ON DELETE RESTRICT``).
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint(r"semver ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'", name="semver_check"),
        CheckConstraint("char_length(branch) BETWEEN 1 AND 64", name="branch_check"),
        UniqueConstraint("agent_id", "semver"),
        Index("agent_versions_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    agent_id: Mapped[uuid.UUID] = fk_column("agents.id", "CASCADE", nullable=False)
    semver: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = text_column("main")
    pathway_id: Mapped[uuid.UUID | None] = fk_column("pathways.id", "RESTRICT")
    pathway_version: Mapped[int | None] = mapped_column(Integer)
    call_settings: Mapped[dict[str, Any]] = jsonb_column("{}")
    changelog: Mapped[str] = text_column("")
    created_by: Mapped[uuid.UUID | None] = fk_column("users.id", "SET NULL")


Index(
    "agent_versions_agent_branch_idx",
    AgentVersion.agent_id,
    AgentVersion.branch,
    AgentVersion.created_at.desc(),
)


class Experiment(IdMixin, TimestampMixin, Base):
    """``experiments``: an A/B test across agent versions; at most one runs per agent."""

    __tablename__ = "experiments"
    __table_args__ = (
        check_in("status", get_args(ExperimentStatus)),
        Index(
            "experiments_one_running_per_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("experiments_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    agent_id: Mapped[uuid.UUID] = fk_column("agents.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ExperimentStatus] = text_column("draft")
    # disposition_rate | avg_eval_score | transfer_rate | avg_duration (the spec has no CHECK).
    primary_metric: Mapped[str] = text_column("disposition_rate")
    primary_metric_config: Mapped[dict[str, Any]] = jsonb_column("{}")
    started_at: Mapped[datetime | None] = mapped_column(tz())
    stopped_at: Mapped[datetime | None] = mapped_column(tz())


class ExperimentArm(IdMixin, TimestampMixin, Base):
    """``experiment_arms``: a weighted agent version; a version used by an arm cannot be deleted."""

    __tablename__ = "experiment_arms"
    __table_args__ = (
        CheckConstraint("weight BETWEEN 0 AND 100", name="weight_check"),
        Index("experiment_arms_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    experiment_id: Mapped[uuid.UUID] = fk_column("experiments.id", "CASCADE", nullable=False)
    agent_version_id: Mapped[uuid.UUID] = fk_column("agent_versions.id", "RESTRICT", nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class Disposition(IdMixin, TimestampMixin, Base):
    """``dispositions``: an org's call outcome tag, e.g. ``APPOINTMENT_BOOKED``."""

    __tablename__ = "dispositions"
    __table_args__ = (
        CheckConstraint("tag ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="tag_check"),
        UniqueConstraint("org_id", "tag"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    tag: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")


class CitationSchema(IdMixin, TimestampMixin, Base):
    """``citation_schemas``: ``variables`` is ``[{name, type, description, required, conditions}]``."""

    __tablename__ = "citation_schemas"
    __table_args__ = (Index("citation_schemas_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    variables: Mapped[list[Any]] = jsonb_column()


class EvalSetup(IdMixin, TimestampMixin, Base):
    """``eval_setups``: a "workbench setup" grading calls against versioned criteria."""

    __tablename__ = "eval_setups"
    __table_args__ = (Index("eval_setups_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    latest_version: Mapped[int] = int_column(1)


class EvalSetupVersion(IdMixin, TimestampMixin, Base):
    """``eval_setup_versions``: ``criteria`` is ``[{id, name, description, type, weight, instructions}]`` with
    ``type`` one of ``pass_fail``, ``score_1_5``, ``score_0_100``.
    """

    __tablename__ = "eval_setup_versions"
    __table_args__ = (
        UniqueConstraint("eval_setup_id", "version_number"),
        Index("eval_setup_versions_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    eval_setup_id: Mapped[uuid.UUID] = fk_column("eval_setups.id", "CASCADE", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    criteria: Mapped[list[Any]] = jsonb_column()
    pass_threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("70"))


class Widget(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``widgets``: an embeddable chat/voice widget; ``config`` is a ``WidgetConfig`` (specs/22 §3) and
    ``public_key`` (``wgt_<random>``) is embedded in the script tag.
    """

    __tablename__ = "widgets"
    __table_args__ = (
        CheckConstraint("messages_per_minute BETWEEN 1 AND 600", name="messages_per_minute_check"),
        Index("widgets_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    pathway_id: Mapped[uuid.UUID | None] = fk_column("pathways.id", "SET NULL")
    pathway_version: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[uuid.UUID | None] = fk_column("agents.id", "SET NULL")
    persona_id: Mapped[uuid.UUID | None] = fk_column("personas.id", "SET NULL")
    agent_prompt: Mapped[str | None] = mapped_column(Text)
    allowed_domains: Mapped[list[str]] = text_array_column("{}")
    messages_per_minute: Mapped[int] = int_column(20)
    voice_enabled: Mapped[bool] = bool_column(default=False)
    voice_id: Mapped[uuid.UUID | None] = fk_column("voices.id", "SET NULL")
    config: Mapped[dict[str, Any]] = jsonb_column("{}")
    webhook_url: Mapped[str | None] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
