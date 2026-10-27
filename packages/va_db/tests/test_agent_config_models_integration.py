"""Agent configuration models against Postgres + pgvector, on the rolled-back ``scratch_session`` schema."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from model_factories import (
    ModelBuilder,
    assert_check_enforced,
    assert_enum_check_enforced,
    assert_rejected,
    flushed,
    make_org,
    make_user,
)
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import (
    EMBEDDING_DIMENSIONS,
    Agent,
    AgentVersion,
    Disposition,
    EvalSetup,
    EvalSetupVersion,
    Experiment,
    ExperimentArm,
    ExperimentStatus,
    KbChunk,
    KbDocument,
    KbDocumentType,
    KnowledgeBase,
    KnowledgeBaseStatus,
    Pathway,
    PathwayFolder,
    PathwayTestRun,
    PathwayTestRunKind,
    PathwayTestRunStatus,
    PathwayUnitTest,
    PathwayVersion,
    Persona,
    PersonaVersion,
    Tool,
    ToolHttpMethod,
    VersionStatus,
    Voice,
    VoiceCategory,
    VoiceCloneStatus,
    VoiceProvider,
    VoiceRating,
    VoiceService,
    Widget,
)

pytestmark = pytest.mark.integration

_VOICE_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0,
    "use_speaker_boost": True,
    "speed": 1.0,
}


def _voice(org_id: uuid.UUID | None, **values: Any) -> Voice:
    values.setdefault("name", f"voice-{secrets.token_hex(4)}")
    values.setdefault("provider_voice_id", secrets.token_hex(10))
    return Voice(org_id=org_id, **values)


async def _org_id(session: AsyncSession) -> uuid.UUID:
    return (await make_org(session)).id


async def _pathway(session: AsyncSession, org_id: uuid.UUID, **values: Any) -> Pathway:
    return await flushed(session, Pathway(org_id=org_id, name="Booking", **values))


async def _agent(session: AsyncSession) -> Agent:
    return await flushed(session, Agent(org_id=await _org_id(session), name="Receptionist"))


def _kb(org_id: uuid.UUID) -> KnowledgeBase:
    return KnowledgeBase(
        org_id=org_id,
        name="FAQ",
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_dimensions=EMBEDDING_DIMENSIONS,
    )


async def _document(session: AsyncSession, kb: KnowledgeBase) -> KbDocument:
    return await flushed(session, KbDocument(org_id=kb.org_id, kb_id=kb.id, type="TEXT", name="hours.txt"))


async def _build_voice(session: AsyncSession) -> Base:
    return _voice(await _org_id(session))


async def _build_tool(session: AsyncSession) -> Base:
    return Tool(org_id=await _org_id(session), name="lookup_order", description="d", url="https://x.test")


async def _build_knowledge_base(session: AsyncSession) -> Base:
    return _kb(await _org_id(session))


async def _build_kb_document(session: AsyncSession) -> Base:
    kb = await flushed(session, _kb(await _org_id(session)))
    return KbDocument(org_id=kb.org_id, kb_id=kb.id, type="FILE", name="menu.pdf")


async def _build_pathway_version(session: AsyncSession) -> Base:
    pathway = await _pathway(session, await _org_id(session))
    return PathwayVersion(
        org_id=pathway.org_id, pathway_id=pathway.id, version_number=1, status="draft", graph_hash="0" * 64
    )


async def _build_persona_version(session: AsyncSession) -> Base:
    persona = await flushed(session, Persona(org_id=await _org_id(session), name="Ava"))
    return PersonaVersion(
        org_id=persona.org_id, persona_id=persona.id, version_number=1, status="draft", config={}
    )


async def _build_pathway_test_run(session: AsyncSession) -> Base:
    pathway = await _pathway(session, await _org_id(session))
    return PathwayTestRun(
        org_id=pathway.org_id, pathway_id=pathway.id, version_number=1, kind="unit", status="queued"
    )


async def _build_experiment(session: AsyncSession) -> Base:
    agent = await _agent(session)
    return Experiment(org_id=agent.org_id, agent_id=agent.id, name="greeting A/B")


async def _build_voice_rating(session: AsyncSession) -> Base:
    org_id = await _org_id(session)
    voice = await flushed(session, _voice(org_id))
    user = await make_user(session)
    return VoiceRating(voice_id=voice.id, org_id=org_id, user_id=user.id, rating=3)


async def _build_pathway_unit_test(session: AsyncSession) -> Base:
    pathway = await _pathway(session, await _org_id(session))
    return PathwayUnitTest(
        org_id=pathway.org_id,
        pathway_id=pathway.id,
        name="routes to booking",
        node_id="start",
        history=[],
        user_message="I want to book",
        expected={"route_to_node_id": "book"},
    )


async def _build_agent_version(session: AsyncSession) -> Base:
    agent = await _agent(session)
    return AgentVersion(org_id=agent.org_id, agent_id=agent.id, semver="1.0.0")


async def _build_experiment_arm(session: AsyncSession) -> Base:
    agent = await _agent(session)
    version = await flushed(session, AgentVersion(org_id=agent.org_id, agent_id=agent.id, semver="1.0.0"))
    experiment = await flushed(session, Experiment(org_id=agent.org_id, agent_id=agent.id, name="e"))
    return ExperimentArm(
        org_id=agent.org_id, experiment_id=experiment.id, agent_version_id=version.id, weight=50, label="A"
    )


async def _build_disposition(session: AsyncSession) -> Base:
    return Disposition(org_id=await _org_id(session), tag="BOOKED")


async def _build_widget(session: AsyncSession) -> Base:
    return Widget(org_id=await _org_id(session), name="Site chat", public_key=f"wgt_{secrets.token_hex(12)}")


async def test_voice_defaults(scratch_session: AsyncSession) -> None:
    voice = await flushed(scratch_session, _voice(await _org_id(scratch_session)))
    assert voice.settings == _VOICE_SETTINGS
    assert (voice.language_codes, voice.tags) == (["en"], [])
    assert (voice.provider, voice.service, voice.category) == ("elevenlabs", "ELEVEN_FLASH", "premade")
    assert (voice.is_public, voice.rating_sum, voice.rating_count, voice.description) == (False, 0, 0, "")
    assert (voice.clone_status, voice.consent, voice.deleted_at) == (None, None, None)

    platform = await flushed(scratch_session, _voice(None, is_public=True))
    assert platform.org_id is None


@pytest.mark.parametrize(
    ("column", "first", "duplicate", "index"),
    [
        ("name", "Ava", "ava", "voices_name_scope_uq"),
        ("provider_voice_id", "pv_ava", "pv_ava", "voices_provider_uq"),
    ],
)
async def test_voice_unique_per_scope(
    scratch_session: AsyncSession, column: str, first: str, duplicate: str, index: str
) -> None:
    org_id, other_org_id = await _org_id(scratch_session), await _org_id(scratch_session)
    held = await flushed(scratch_session, _voice(org_id, **{column: first}))
    await assert_rejected(scratch_session, _voice(org_id, **{column: duplicate}), index)
    await flushed(scratch_session, _voice(other_org_id, **{column: duplicate}))
    await flushed(scratch_session, _voice(None, **{column: first}))
    await assert_rejected(scratch_session, _voice(None, **{column: duplicate}), index)

    held.deleted_at = datetime(2026, 10, 27, tzinfo=UTC)
    await scratch_session.flush()
    await flushed(scratch_session, _voice(org_id, **{column: duplicate}))


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_voice, "provider", VoiceProvider),
        (_build_voice, "service", VoiceService),
        (_build_voice, "category", VoiceCategory),
        (_build_voice, "clone_status", VoiceCloneStatus),
        (_build_tool, "method", ToolHttpMethod),
        (_build_knowledge_base, "status", KnowledgeBaseStatus),
        (_build_kb_document, "type", KbDocumentType),
        (_build_kb_document, "status", KnowledgeBaseStatus),
        (_build_pathway_version, "status", VersionStatus),
        (_build_persona_version, "status", VersionStatus),
        (_build_pathway_test_run, "kind", PathwayTestRunKind),
        (_build_pathway_test_run, "status", PathwayTestRunStatus),
        (_build_experiment, "status", ExperimentStatus),
    ],
)
async def test_enum_values_accepted_and_rejected(
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


@pytest.mark.parametrize(
    ("builder", "column", "accepted", "rejected"),
    [
        (_build_voice_rating, "rating", (1, 5), (0, 6)),
        (_build_tool, "name", ("a", "Lookup_order_2", "T" + "_" * 63), ("1bad", "has-dash", "x" * 65)),
        (_build_tool, "timeout_ms", (100, 60000), (99, 60001)),
        (_build_tool, "retries", (0, 3), (-1, 4)),
        (_build_pathway_unit_test, "simulated_variations", (0, 20), (-1, 21)),
        (_build_agent_version, "semver", ("1.2.3", "10.0.0-rc.1"), ("1.2", "v1.2.3", "1.2.3-")),
        (_build_agent_version, "branch", ("x", "b" * 64), ("", "b" * 65)),
        (_build_experiment_arm, "weight", (0, 100), (-1, 101)),
        (_build_disposition, "tag", ("A", "APPOINTMENT_BOOKED"), ("lower", "1ABC", "A" * 65)),
        (_build_widget, "messages_per_minute", (1, 600), (0, 601)),
    ],
)
async def test_range_and_pattern_checks(
    scratch_session: AsyncSession,
    builder: ModelBuilder,
    column: str,
    accepted: tuple[Any, ...],
    rejected: tuple[Any, ...],
) -> None:
    await assert_check_enforced(scratch_session, builder, column, accepted, rejected)


async def test_kb_chunk_embedding_and_tsv(scratch_session: AsyncSession) -> None:
    kb = await flushed(scratch_session, _kb(await _org_id(scratch_session)))
    document = await _document(scratch_session, kb)
    embedding = [0.5] * (EMBEDDING_DIMENSIONS - 1) + [-0.25]
    chunk = await flushed(
        scratch_session,
        KbChunk(
            org_id=kb.org_id,
            kb_id=kb.id,
            document_id=document.id,
            ordinal=0,
            content="Refunds are processed within five business days",
            token_count=8,
            embedding=embedding,
        ),
    )
    assert chunk.embedding == embedding
    assert chunk.heading_path == []
    assert chunk.tsv is not None
    assert "'refunds'" in chunk.tsv
    matching = select(KbChunk.id).where(KbChunk.tsv.op("@@")(func.to_tsquery("simple", "refunds & business")))
    assert (await scratch_session.execute(matching)).scalars().all() == [chunk.id]
    nearest = select(KbChunk.id).order_by(KbChunk.embedding.cosine_distance(embedding)).limit(1)
    assert (await scratch_session.execute(nearest)).scalar_one() == chunk.id

    short = KbChunk(
        org_id=kb.org_id, kb_id=kb.id, document_id=document.id, ordinal=1, content="x", token_count=1
    )
    short.embedding = [1.0, 2.0, 3.0]
    with pytest.raises(DBAPIError, match="expected 1024 dimensions, not 3"):
        async with scratch_session.begin_nested():
            await flushed(scratch_session, short)


async def test_kb_chunk_indexes_exist(scratch_session: AsyncSession) -> None:
    query = text(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE indexname IN ('kb_chunks_embedding_hnsw', 'kb_chunks_tsv_idx')"
    )
    definitions = {str(row.indexname): str(row.indexdef) for row in await scratch_session.execute(query)}
    assert (
        "USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')"
        in (definitions["kb_chunks_embedding_hnsw"])
    )
    assert "USING gin (tsv)" in definitions["kb_chunks_tsv_idx"]


async def test_experiment_one_running_per_agent(scratch_session: AsyncSession) -> None:
    agent, other = await _agent(scratch_session), await _agent(scratch_session)

    def experiment(owner: Agent, status: ExperimentStatus) -> Experiment:
        return Experiment(org_id=owner.org_id, agent_id=owner.id, name=status, status=status)

    await flushed(scratch_session, experiment(agent, "running"))
    await assert_rejected(scratch_session, experiment(agent, "running"), "experiments_one_running_per_agent")
    for status in ("draft", "draft", "stopped", "stopped"):
        await flushed(scratch_session, experiment(agent, status))
    await flushed(scratch_session, experiment(other, "running"))


async def test_restrict_and_set_null_paths(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    parent = await flushed(scratch_session, PathwayFolder(org_id=org_id, name="Sales"))
    child = await flushed(scratch_session, PathwayFolder(org_id=org_id, name="Inbound", parent_id=parent.id))
    pathway = await _pathway(scratch_session, org_id, folder_id=child.id)
    agent = await flushed(scratch_session, Agent(org_id=org_id, name="Receptionist"))
    version = await flushed(
        scratch_session,
        AgentVersion(
            org_id=org_id, agent_id=agent.id, semver="1.0.0", pathway_id=pathway.id, pathway_version=1
        ),
    )
    experiment = await flushed(scratch_session, Experiment(org_id=org_id, agent_id=agent.id, name="e"))
    await flushed(
        scratch_session,
        ExperimentArm(
            org_id=org_id, experiment_id=experiment.id, agent_version_id=version.id, weight=100, label="A"
        ),
    )
    voice = await flushed(scratch_session, _voice(org_id))
    widget = await flushed(
        scratch_session, Widget(org_id=org_id, name="w", public_key="wgt_restrict", voice_id=voice.id)
    )
    scratch_session.expunge_all()

    for statement, constraint in (
        (delete(Pathway).where(Pathway.id == pathway.id), "agent_versions_pathway_id_fkey"),
        (delete(AgentVersion).where(AgentVersion.id == version.id), "experiment_arms_agent_version_id_fkey"),
    ):
        with pytest.raises(DBAPIError, match=constraint):
            async with scratch_session.begin_nested():
                await scratch_session.execute(statement)

    await scratch_session.execute(delete(Voice).where(Voice.id == voice.id))
    widget_voice = select(Widget.voice_id).where(Widget.id == widget.id)
    assert (await scratch_session.execute(widget_voice)).scalar_one() is None

    await scratch_session.execute(delete(PathwayFolder).where(PathwayFolder.id == parent.id))
    remaining = select(func.count()).select_from(PathwayFolder).where(PathwayFolder.org_id == org_id)
    assert (await scratch_session.execute(remaining)).scalar_one() == 0
    folder = select(Pathway.folder_id).where(Pathway.id == pathway.id)
    assert (await scratch_session.execute(folder)).scalar_one() is None


async def test_version_uniques(scratch_session: AsyncSession) -> None:
    pathway_version = await _build_pathway_version(scratch_session)
    persona_version = await _build_persona_version(scratch_session)
    agent_version = await _build_agent_version(scratch_session)
    eval_setup = await flushed(scratch_session, EvalSetup(org_id=await _org_id(scratch_session), name="QA"))
    eval_version = EvalSetupVersion(
        org_id=eval_setup.org_id, eval_setup_id=eval_setup.id, version_number=1, criteria=[]
    )
    for model in (pathway_version, persona_version, agent_version, eval_version):
        await flushed(scratch_session, model)

    duplicates: list[tuple[Base, str]] = [
        (
            PathwayVersion(
                **_copy(pathway_version, "org_id", "pathway_id", "version_number", "status", "graph_hash")
            ),
            "pathway_versions_pathway_id_version_number_key",
        ),
        (
            PersonaVersion(
                **_copy(persona_version, "org_id", "persona_id", "version_number", "status", "config")
            ),
            "persona_versions_persona_id_version_number_key",
        ),
        (
            AgentVersion(**_copy(agent_version, "org_id", "agent_id", "semver")),
            "agent_versions_agent_id_semver_key",
        ),
        (
            EvalSetupVersion(**_copy(eval_version, "org_id", "eval_setup_id", "version_number", "criteria")),
            "eval_setup_versions_eval_setup_id_version_number_key",
        ),
    ]
    for duplicate, constraint in duplicates:
        await assert_rejected(scratch_session, duplicate, constraint)


def _copy(model: Base, *names: str) -> dict[str, Any]:
    return {name: getattr(model, name) for name in names}


async def test_defaults_of_versioned_resources(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    eval_setup = await flushed(scratch_session, EvalSetup(org_id=org_id, name="QA"))
    eval_version = await flushed(
        scratch_session,
        EvalSetupVersion(org_id=org_id, eval_setup_id=eval_setup.id, version_number=1, criteria=[]),
    )
    assert eval_version.pass_threshold == Decimal("70.00")
    assert str(eval_version.pass_threshold) == "70.00"
    assert eval_setup.latest_version == 1

    pathway = await _pathway(scratch_session, org_id)
    assert (pathway.draft_version, pathway.latest_version, pathway.production_version) == (1, 1, None)
    version = await flushed(scratch_session, await _build_pathway_version(scratch_session))
    assert isinstance(version, PathwayVersion)
    assert (version.nodes, version.edges, version.global_config) == ([], [], {})

    agent_version = await flushed(scratch_session, await _build_agent_version(scratch_session))
    assert isinstance(agent_version, AgentVersion)
    assert (agent_version.branch, agent_version.call_settings, agent_version.changelog) == ("main", {}, "")

    tool = await flushed(scratch_session, await _build_tool(scratch_session))
    assert isinstance(tool, Tool)
    assert (tool.method, tool.timeout_ms, tool.retries, tool.body) == ("POST", 10000, 0, None)
    assert tool.input_schema == {"type": "object", "properties": {}}

    widget = await flushed(scratch_session, await _build_widget(scratch_session))
    assert isinstance(widget, Widget)
    assert (widget.messages_per_minute, widget.voice_enabled, widget.allowed_domains) == (20, False, [])
    await assert_rejected(
        scratch_session,
        Widget(org_id=org_id, name="dup", public_key=widget.public_key),
        "widgets_public_key_key",
    )

    experiment = await flushed(scratch_session, await _build_experiment(scratch_session))
    assert isinstance(experiment, Experiment)
    assert (experiment.status, experiment.primary_metric) == ("draft", "disposition_rate")
    # The spec lists the metrics in a comment only, so other values are stored as given.
    await scratch_session.execute(
        update(Experiment).where(Experiment.id == experiment.id).values(primary_metric="custom_metric")
    )
    stored = select(Experiment.primary_metric).where(Experiment.id == experiment.id)
    assert (await scratch_session.execute(stored)).scalar_one() == "custom_metric"
