"""Runner loading for Send Call: pathway, persona, agent, or task (specs/05-calls-api.md §3.1, §4.1 step 5,
§4.2; Decision P05-T03c).

Experiment routing (§4.2 step 1) belongs to P13-T07: an omitted `agent_version` uses the production pin here,
and `experiment_*` stay `None`. Persona `routing` rules are evaluated by P13 as well.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_db.call_inputs.pathways import (
    AGENT_PATHWAY_FIELDS,
    PERSONA_CONFIG_FIELDS,
    REQUEST_PATHWAY_FIELDS,
    LoadedPathway,
    PathwayFields,
    PathwaySelector,
    draft_requires_test_call,
    load_pathway,
)
from va_db.models import Agent, AgentVersion, Persona, PersonaVersion
from va_domain.call_resolution import (
    AgentRunner,
    PathwayRunner,
    PersonaRunner,
    ResolutionWarning,
    RunnerInputs,
    TaskRunner,
)
from va_domain.calls import SendCallRequest

__all__ = ["LoadedRunner", "load_runner"]

_ENVIRONMENTS: Final = frozenset({"production", "staging", "dev"})
_BRANCH_PREFIX: Final = "branch:"
_MAIN_BRANCH: Final = "main"
# A persona-config pathway whose start node the request overrides: the request field is reported.
_PERSONA_REQUEST_START: Final = PathwayFields(
    PERSONA_CONFIG_FIELDS.pathway_id, PERSONA_CONFIG_FIELDS.version, REQUEST_PATHWAY_FIELDS.start_node_id
)


@dataclass(frozen=True, slots=True)
class LoadedRunner:
    runner: RunnerInputs
    warnings: tuple[ResolutionWarning, ...] = ()


async def _pathway_runner(
    session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest, pathway_id: uuid.UUID
) -> LoadedRunner:
    loaded = await load_pathway(
        session,
        org_id,
        pathway_id,
        request.pathway_version,
        fields=REQUEST_PATHWAY_FIELDS,
        test_call=request.test_call,
        start_node_id=request.start_node_id,
    )
    return LoadedRunner(PathwayRunner(loaded.ref), loaded.warnings)


async def _persona_version_row(session: AsyncSession, persona: Persona, number: int) -> PersonaVersion | None:
    return await session.scalar(
        select(PersonaVersion).where(
            PersonaVersion.org_id == persona.org_id,
            PersonaVersion.persona_id == persona.id,
            PersonaVersion.version_number == number,
        )
    )


def _persona_version_missing(number: int) -> AppError:
    return AppError("NOT_FOUND", f"Persona version {number} not found.", field="persona_version")


def _persona_production(persona: Persona) -> tuple[int, ResolutionWarning | None]:
    if persona.production_version is not None:
        return persona.production_version, None
    message = "The persona has no production version; its draft is used."
    return persona.draft_version, ResolutionWarning("PERSONA_UNPUBLISHED", message, "persona_version")


async def _persona_version(
    session: AsyncSession, persona: Persona, request: SendCallRequest
) -> tuple[PersonaVersion, ResolutionWarning | None]:
    selector = request.persona_version
    warning: ResolutionWarning | None = None
    if selector == "draft":
        if not request.test_call:
            raise draft_requires_test_call("persona_version")
        number = persona.draft_version
    elif isinstance(selector, int) and selector > 0:
        row = await _persona_version_row(session, persona, selector)
        if row is not None:
            return row, None
        if persona.production_version is None:
            raise _persona_version_missing(selector)
        number = persona.production_version
        message = f"Persona version {selector} does not exist; the production version is used."
        warning = ResolutionWarning("PERSONA_VERSION_FALLBACK", message, "persona_version")
    else:
        number, warning = _persona_production(persona)
    row = await _persona_version_row(session, persona, number)
    if row is None:
        raise _persona_version_missing(number)
    return row, warning


def _config_selector(config: Mapping[str, Any]) -> PathwaySelector:
    value = config.get("pathway_version")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if value is None or value == "production":
        return "production"
    if value == "staging":
        return "staging"
    message = 'pathway_version must be a non-negative integer, "production", or "staging".'
    raise AppError("INVALID_INPUT", message, field=PERSONA_CONFIG_FIELDS.version)


def _config_pathway_id(config: Mapping[str, Any]) -> uuid.UUID | None:
    if config.get("runner") != "pathway" or config.get("pathway_id") is None:
        return None
    try:
        return uuid.UUID(str(config["pathway_id"]))
    except ValueError:
        message = "pathway_id must be a UUID."
        raise AppError("INVALID_INPUT", message, field=PERSONA_CONFIG_FIELDS.pathway_id) from None


async def _persona_pathway(
    session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest, config: Mapping[str, Any]
) -> LoadedPathway | None:
    """The request's pathway override when sent, else the pathway of a `runner: "pathway"` persona config."""
    if request.pathway_id is not None:
        return await load_pathway(
            session,
            org_id,
            request.pathway_id,
            request.pathway_version,
            fields=REQUEST_PATHWAY_FIELDS,
            test_call=request.test_call,
            start_node_id=request.start_node_id,
        )
    pathway_id = _config_pathway_id(config)
    if pathway_id is None:
        return None
    configured_start = config.get("start_node_id")
    start: str | None = request.start_node_id
    fields = _PERSONA_REQUEST_START
    if start is None:
        start = configured_start if isinstance(configured_start, str) else None
        fields = PERSONA_CONFIG_FIELDS
    return await load_pathway(
        session, org_id, pathway_id, _config_selector(config), fields=fields, start_node_id=start
    )


async def _persona_runner(session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest) -> LoadedRunner:
    persona = await session.scalar(
        select(Persona).where(
            Persona.id == request.persona_id, Persona.org_id == org_id, Persona.deleted_at.is_(None)
        )
    )
    if persona is None:
        raise AppError("NOT_FOUND", "Persona not found.", field="persona_id")
    version, warning = await _persona_version(session, persona, request)
    config = version.config
    pathway = await _persona_pathway(session, org_id, request, config)
    warnings = ([] if warning is None else [warning]) + ([] if pathway is None else list(pathway.warnings))
    runner = PersonaRunner(
        persona_id=persona.id,
        version=version.version_number,
        legacy=config.get("legacy") is True,
        config=config,
        pathway=None if pathway is None else pathway.ref,
    )
    return LoadedRunner(runner, tuple(warnings))


async def _newest_on(session: AsyncSession, agent: Agent, branch: str) -> AgentVersion | None:
    return await session.scalar(
        select(AgentVersion)
        .where(
            AgentVersion.org_id == agent.org_id,
            AgentVersion.agent_id == agent.id,
            AgentVersion.branch == branch,
        )
        .order_by(AgentVersion.created_at.desc(), AgentVersion.id.desc())
        .limit(1)
    )


async def _by_semver(session: AsyncSession, agent: Agent, semver: str) -> AgentVersion | None:
    return await session.scalar(
        select(AgentVersion).where(
            AgentVersion.org_id == agent.org_id,
            AgentVersion.agent_id == agent.id,
            AgentVersion.semver == semver,
        )
    )


async def _agent_version(session: AsyncSession, agent: Agent, selector: str | None) -> AgentVersion:
    """§4.2 steps 2-3: environment pins (missing pin → newest on `main`), `latest`, `branch:x`, semver."""
    if selector is None or selector in _ENVIRONMENTS:
        pin = agent.env_pins.get(selector or "production")
        if isinstance(pin, str):
            row = await _by_semver(session, agent, pin)
        else:
            row = await _newest_on(session, agent, _MAIN_BRANCH)
    elif selector == "latest":
        row = await _newest_on(session, agent, _MAIN_BRANCH)
    elif selector.startswith(_BRANCH_PREFIX):
        row = await _newest_on(session, agent, selector.removeprefix(_BRANCH_PREFIX))
    else:
        row = await _by_semver(session, agent, selector)
    if row is None:
        shown = selector or "production"
        raise AppError("NOT_FOUND", f"Agent version {shown} not found.", field="agent_version")
    return row


async def _agent_runner(session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest) -> LoadedRunner:
    agent = await session.scalar(
        select(Agent).where(Agent.id == request.agent_id, Agent.org_id == org_id, Agent.deleted_at.is_(None))
    )
    if agent is None:
        raise AppError("NOT_FOUND", "Agent not found.", field="agent_id")
    version = await _agent_version(session, agent, request.agent_version)
    pathway: LoadedPathway | None = None
    if version.pathway_id is not None:
        pathway = await load_pathway(
            session,
            org_id,
            version.pathway_id,
            version.pathway_version,
            fields=AGENT_PATHWAY_FIELDS,
            exact=True,
        )
    runner = AgentRunner(
        agent_id=agent.id,
        agent_version_id=version.id,
        call_settings=version.call_settings,
        pathway=None if pathway is None else pathway.ref,
    )
    return LoadedRunner(runner, () if pathway is None else pathway.warnings)


async def load_runner(session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest) -> LoadedRunner:
    """Load the runner inputs for `request.runner_type` (agent, persona, pathway, task)."""
    runner_type = request.runner_type
    if runner_type == "agent":
        return await _agent_runner(session, org_id, request)
    if runner_type == "persona":
        return await _persona_runner(session, org_id, request)
    if request.pathway_id is not None:
        return await _pathway_runner(session, org_id, request, request.pathway_id)
    return LoadedRunner(TaskRunner())
