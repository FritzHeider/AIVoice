"""Send Call input loading against Postgres: pathway, persona, and agent runners and their versions."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from call_input_rows import START_NODE, Seed, graph
from identity_db import IdentityDb

from va_domain.call_resolution import AgentRunner, PathwayRunner, PersonaRunner, TaskRunner

pytestmark = pytest.mark.integration


@pytest.fixture
async def seed(identity: IdentityDb) -> Seed:
    seeded = await Seed.create(identity.database)
    await seeded.voice("Karen")
    await seeded.number()
    return seeded


def _warning_codes(inputs: Any) -> list[str]:
    return [warning.code for warning in inputs.warnings]


async def _pathway_run(seed: Seed, pathway_id: uuid.UUID, **body: Any) -> tuple[int, list[str], Any]:
    inputs = await seed.load({"pathway_id": str(pathway_id), **body})
    assert isinstance(inputs.runner, PathwayRunner)
    return inputs.runner.pathway.version, _warning_codes(inputs), inputs.runner.pathway


async def test_task_runner(seed: Seed) -> None:
    inputs = await seed.load({})
    assert inputs.runner == TaskRunner()
    assert inputs.warnings == ()


async def test_pathway_production_is_the_default(seed: Seed) -> None:
    config = {"defaultVoice": "karen", "errorPhrase": "Sorry!", "call_defaults": {"record": True}}
    pathway_id = await seed.pathway({1: {}, 2: {"global_config": config}, 3: {}}, production=2, draft=3)
    for selector in (None, 0, "production"):
        body = {} if selector is None else {"pathway_version": selector}
        version, warnings, ref = await _pathway_run(seed, pathway_id, **body)
        assert (version, warnings) == (2, [])
    assert ref.call_defaults == {"voice": "karen", "record": True}
    assert (ref.error_phrase, ref.start_node_id) == ("Sorry!", START_NODE)


async def test_unpublished_pathway_uses_the_draft(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}, 2: {}}, production=None, draft=2)
    assert (await _pathway_run(seed, pathway_id))[:2] == (2, ["PATHWAY_UNPUBLISHED"])
    staging = await _pathway_run(seed, pathway_id, pathway_version="staging")
    assert staging[:2] == (2, ["PATHWAY_UNPUBLISHED"])


async def test_integer_pathway_versions(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}, 2: {}, 3: {}}, production=2, draft=3)
    assert (await _pathway_run(seed, pathway_id, pathway_version=1))[:2] == (1, [])
    assert (await _pathway_run(seed, pathway_id, pathway_version=9))[:2] == (2, ["PATHWAY_VERSION_FALLBACK"])
    unpublished = await seed.pathway({1: {}}, production=None)
    assert await seed.fails({"pathway_id": str(unpublished), "pathway_version": 9}) == (
        "PATHWAY_VERSION_NOT_FOUND",
        "pathway_version",
        "Pathway version 9 not found.",
    )


async def test_staging_and_draft_pathway_versions(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}, 2: {}, 3: {}}, production=1, staging=2, draft=3)
    assert (await _pathway_run(seed, pathway_id, pathway_version="staging"))[:2] == (2, [])
    no_staging = await seed.pathway({1: {}, 2: {}}, production=1, draft=2)
    assert (await _pathway_run(seed, no_staging, pathway_version="staging"))[:2] == (1, [])
    draft = {"pathway_id": str(pathway_id), "pathway_version": "draft"}
    assert await seed.fails(draft) == (
        "INVALID_INPUT",
        "pathway_version",
        'pathway_version "draft" requires test_call.',
    )
    assert (await _pathway_run(seed, pathway_id, pathway_version="draft", test_call=True))[:2] == (3, [])


async def test_missing_or_deleted_pathway(seed: Seed) -> None:
    deleted = await seed.pathway(deleted=True)
    for pathway_id in (uuid.uuid4(), deleted):
        assert await seed.fails({"pathway_id": str(pathway_id)}) == (
            "NOT_FOUND",
            "pathway_id",
            "Pathway not found.",
        )


async def test_pathway_pin_without_a_version_row(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}}, production=4)
    assert await seed.fails({"pathway_id": str(pathway_id)}) == (
        "PATHWAY_VERSION_NOT_FOUND",
        "pathway_version",
        "Pathway version 4 not found.",
    )


async def test_start_node_must_exist_in_the_version(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {"nodes": graph("ask")}})
    assert (await _pathway_run(seed, pathway_id, start_node_id="ask"))[2].start_node_id == "ask"
    assert await seed.fails({"pathway_id": str(pathway_id), "start_node_id": "nope"}) == (
        "INVALID_INPUT",
        "start_node_id",
        "start_node_id does not exist in pathway version 1.",
    )


async def test_non_object_pathway_call_defaults(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {"global_config": {"call_defaults": [1]}}})
    assert (await seed.fails({"pathway_id": str(pathway_id)}))[:2] == (
        "INVALID_INPUT",
        "pathway.call_defaults",
    )


async def test_another_orgs_pathway_reads_as_absent(seed: Seed, identity: IdentityDb) -> None:
    other = await Seed.create(identity.database)
    foreign = await other.pathway()
    assert (await seed.fails({"pathway_id": str(foreign)}))[:2] == ("NOT_FOUND", "pathway_id")


async def _persona_run(seed: Seed, persona_id: uuid.UUID, **body: Any) -> tuple[PersonaRunner, list[str]]:
    inputs = await seed.load({"persona_id": str(persona_id), **body})
    assert isinstance(inputs.runner, PersonaRunner)
    return inputs.runner, _warning_codes(inputs)


async def test_persona_versions(seed: Seed) -> None:
    persona_id = await seed.persona(
        {1: {"prompt": "one"}, 2: {"prompt": "two", "legacy": True}, 3: {"prompt": "three"}},
        production=2,
        draft=3,
    )
    runner, warnings = await _persona_run(seed, persona_id)
    assert (runner.version, runner.legacy, runner.config["prompt"], warnings) == (2, True, "two", [])
    assert (await _persona_run(seed, persona_id, persona_version=1))[0].legacy is False
    fallback = await _persona_run(seed, persona_id, persona_version=7)
    assert (fallback[0].version, fallback[1]) == (2, ["PERSONA_VERSION_FALLBACK"])
    assert (await _persona_run(seed, persona_id, persona_version="draft", test_call=True))[0].version == 3
    assert await seed.fails({"persona_id": str(persona_id), "persona_version": "draft"}) == (
        "INVALID_INPUT",
        "persona_version",
        'persona_version "draft" requires test_call.',
    )


async def test_unpublished_persona(seed: Seed) -> None:
    persona_id = await seed.persona({1: {"prompt": "one"}}, production=None)
    runner, warnings = await _persona_run(seed, persona_id)
    assert (runner.version, warnings) == (1, ["PERSONA_UNPUBLISHED"])
    assert await seed.fails({"persona_id": str(persona_id), "persona_version": 5}) == (
        "NOT_FOUND",
        "persona_version",
        "Persona version 5 not found.",
    )
    assert (await seed.fails({"persona_id": str(uuid.uuid4())}))[:2] == ("NOT_FOUND", "persona_id")


async def test_persona_pin_without_a_version_row(seed: Seed) -> None:
    persona_id = await seed.persona({1: {"prompt": "one"}}, production=3)
    assert (await seed.fails({"persona_id": str(persona_id)}))[:2] == ("NOT_FOUND", "persona_version")


async def test_persona_config_pathway_and_request_override(seed: Seed) -> None:
    configured = await seed.pathway({1: {"nodes": graph("ask")}, 2: {}}, production=1, staging=2)
    override = await seed.pathway({1: {}}, production=1)
    config = {"runner": "pathway", "pathway_id": str(configured), "pathway_version": "staging"}
    persona_id = await seed.persona({1: config})
    runner, _ = await _persona_run(seed, persona_id)
    assert runner.pathway is not None
    assert (runner.pathway.pathway_id, runner.pathway.version) == (configured, 2)
    pinned = await seed.persona({1: {**config, "pathway_version": 1, "start_node_id": "ask"}})
    runner, _ = await _persona_run(seed, pinned)
    assert runner.pathway is not None
    assert (runner.pathway.version, runner.pathway.start_node_id) == (1, "ask")
    runner, _ = await _persona_run(seed, persona_id, pathway_id=str(override))
    assert runner.pathway is not None
    assert runner.pathway.pathway_id == override


async def test_persona_config_pathway_errors(seed: Seed) -> None:
    bad_id = await seed.persona({1: {"runner": "pathway", "pathway_id": "nope"}})
    assert (await seed.fails({"persona_id": str(bad_id)}))[:2] == (
        "INVALID_INPUT",
        "persona.config.pathway_id",
    )
    missing = await seed.persona({1: {"runner": "pathway", "pathway_id": str(uuid.uuid4())}})
    assert (await seed.fails({"persona_id": str(missing)}))[:2] == (
        "NOT_FOUND",
        "persona.config.pathway_id",
    )
    pathway_id = await seed.pathway()
    bad_version = await seed.persona(
        {1: {"runner": "pathway", "pathway_id": str(pathway_id), "pathway_version": "x"}}
    )
    assert (await seed.fails({"persona_id": str(bad_version)}))[:2] == (
        "INVALID_INPUT",
        "persona.config.pathway_version",
    )
    bad_start = await seed.persona(
        {1: {"runner": "pathway", "pathway_id": str(pathway_id), "start_node_id": "x"}}
    )
    assert (await seed.fails({"persona_id": str(bad_start)}))[:2] == (
        "INVALID_INPUT",
        "persona.config.start_node_id",
    )
    assert (await seed.fails({"persona_id": str(bad_start), "start_node_id": "y"}))[:2] == (
        "INVALID_INPUT",
        "start_node_id",
    )
    prompt_persona = await seed.persona({1: {"runner": "prompt", "pathway_id": str(pathway_id)}})
    runner, _ = await _persona_run(seed, prompt_persona)
    assert runner.pathway is None


async def _agent_version(seed: Seed, agent_id: uuid.UUID, selector: str | None = None) -> uuid.UUID:
    body: dict[str, Any] = {"agent_id": str(agent_id), "task": None}
    if selector is not None:
        body["agent_version"] = selector
    inputs = await seed.load(body)
    assert isinstance(inputs.runner, AgentRunner)
    assert (inputs.runner.experiment_id, inputs.runner.experiment_arm_id) == (None, None)
    return inputs.runner.agent_version_id


async def test_agent_version_selectors(seed: Seed) -> None:
    agent_id, (v100, v110, v120, v200b) = await seed.agent(
        [
            {"semver": "1.0.0", "call_settings": {"record": True}},
            {"semver": "1.1.0"},
            {"semver": "1.2.0"},
            {"semver": "2.0.0-beta", "branch": "beta"},
        ],
        env_pins={"production": "1.1.0", "staging": "1.0.0"},
    )
    assert await _agent_version(seed, agent_id) == v110
    assert await _agent_version(seed, agent_id, "production") == v110
    assert await _agent_version(seed, agent_id, "staging") == v100
    assert await _agent_version(seed, agent_id, "dev") == v120
    assert await _agent_version(seed, agent_id, "latest") == v120
    assert await _agent_version(seed, agent_id, "1.0.0") == v100
    assert await _agent_version(seed, agent_id, "branch:beta") == v200b


async def test_agent_without_a_production_pin_uses_the_newest_main_version(seed: Seed) -> None:
    agent_id, (_, newest, _) = await seed.agent(
        [{"semver": "1.0.0"}, {"semver": "1.1.0"}, {"semver": "3.0.0", "branch": "exp"}]
    )
    assert await _agent_version(seed, agent_id) == newest


async def test_missing_agent_and_versions(seed: Seed) -> None:
    agent_id, _ = await seed.agent([{"semver": "1.0.0"}], env_pins={"production": "9.9.9"})
    base = {"agent_id": str(agent_id), "task": None}
    assert await seed.fails(base) == ("NOT_FOUND", "agent_version", "Agent version production not found.")
    assert (await seed.fails({**base, "agent_version": "4.0.0"}))[:2] == ("NOT_FOUND", "agent_version")
    assert (await seed.fails({**base, "agent_version": "branch:none"}))[:2] == (
        "NOT_FOUND",
        "agent_version",
    )
    empty, _ = await seed.agent([])
    assert (await seed.fails({"agent_id": str(empty), "task": None}))[:2] == ("NOT_FOUND", "agent_version")
    assert await seed.fails({"agent_id": str(uuid.uuid4()), "task": None}) == (
        "NOT_FOUND",
        "agent_id",
        "Agent not found.",
    )


async def test_agent_pathway_is_frozen_to_its_version(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}, 2: {}}, production=2)
    agent_id, _ = await seed.agent([{"semver": "1.0.0", "pathway_id": pathway_id, "pathway_version": 1}])
    inputs = await seed.load({"agent_id": str(agent_id), "task": None})
    assert isinstance(inputs.runner, AgentRunner)
    assert inputs.runner.pathway is not None
    assert inputs.runner.pathway.version == 1
    missing, _ = await seed.agent([{"semver": "1.0.0", "pathway_id": pathway_id, "pathway_version": 5}])
    assert await seed.fails({"agent_id": str(missing), "task": None}) == (
        "PATHWAY_VERSION_NOT_FOUND",
        "agent_version",
        "Pathway version 5 not found.",
    )
    plain, _ = await seed.agent([{"semver": "1.0.0", "call_settings": {"task": "Sell"}}])
    inputs = await seed.load({"agent_id": str(plain), "task": None})
    assert isinstance(inputs.runner, AgentRunner)
    assert (inputs.runner.pathway, inputs.merged.task) == (None, "Sell")
