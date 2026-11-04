"""Pathway regression suite settings: ``GET``/``PUT /v1/pathway/{id}/regression`` (specs/12 §9; Decision
P07-T11e1). Roles live in ``test_pathways_permissions.py``."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
import pytest
from graph_helpers import start
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, star_api
from session_helpers import SessionContainer, audit_rows, error_of

from va_db.repositories import PathwayGraphJson, PathwayRepo
from va_engine.models.graph import dump_graph, graph_hash, load_graph

pytestmark = pytest.mark.integration

DEFAULTS: Final[dict[str, Any]] = {
    "run_on_publish": False,
    "block_publish_on_failure": True,
    "include_simulations": False,
    "scenarios": [],
}
SCENARIO: Final[dict[str, Any]] = {
    "name": "Impatient caller",
    "persona": "You want a large pizza fast.",
    "goal": "Order placed",
}


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


async def get_settings(api: Api, pathway_id: str) -> httpx.Response:
    return await api.client.get(f"{BASE}/{pathway_id}/regression", headers=api.headers)


async def put_settings(api: Api, pathway_id: str, body: dict[str, Any]) -> httpx.Response:
    return await api.client.put(f"{BASE}/{pathway_id}/regression", json=body, headers=api.headers)


async def test_new_pathway_reads_the_defaults(api: Api) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])

    response = await get_settings(api, pathway_id)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "regression": DEFAULTS}


async def test_put_round_trips_through_the_draft(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(
        nodes=[start(text="Hello")], edges=[], global_config={"globalPrompt": "Hi"}
    )
    before = (await api.get(pathway_id)).json()
    body = {"run_on_publish": True, "include_simulations": True, "scenarios": [SCENARIO]}

    saved = await put_settings(api, pathway_id, body)

    assert saved.status_code == 200, saved.text
    stored = {**DEFAULTS, **body, "scenarios": [{**SCENARIO, "max_turns": 20, "request_data": {}}]}
    detail = (await api.get(pathway_id)).json()
    assert saved.json() == {
        "status": "success",
        "regression": stored,
        "draft_version": before["version_number"],
        "graph_hash": detail["graph_hash"],
    }
    assert detail["graph_hash"] != before["graph_hash"]
    assert detail["global_config"] == {"globalPrompt": "Hi", "regression": stored}
    # The hash is the one any graph write (update, import) computes for the same graph.
    parsed = load_graph(detail["nodes"], detail["edges"], detail["global_config"])
    assert detail["graph_hash"] == graph_hash(dump_graph(parsed))
    assert (await get_settings(api, pathway_id)).json()["regression"] == stored

    rows = await audit_rows(identity, api.org_id, "pathway.regression_update")
    assert [row.changes for row in rows] == [
        {
            "before": {
                "run_on_publish": False,
                "block_publish_on_failure": True,
                "include_simulations": False,
                "scenarios": 0,
                "graph_hash": before["graph_hash"],
            },
            "after": {
                "run_on_publish": True,
                "block_publish_on_failure": True,
                "include_simulations": True,
                "scenarios": 1,
                "graph_hash": detail["graph_hash"],
            },
        }
    ]


async def test_storing_the_same_settings_writes_nothing(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])
    first = await put_settings(api, pathway_id, {"run_on_publish": True})
    second = await put_settings(api, pathway_id, {"run_on_publish": True, "block_publish_on_failure": True})

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert len(await audit_rows(identity, api.org_id, "pathway.regression_update")) == 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"run_on_publish": True, "unknown": 1}, "unknown"),
        ({"scenarios": [SCENARIO] * 21}, "scenarios"),
        ({"scenarios": [{**SCENARIO, "max_turns": 0}]}, "scenarios[0].max_turns"),
        ({"run_on_publish": "maybe"}, "run_on_publish"),
    ],
)
async def test_put_rejects_invalid_settings(api: Api, body: dict[str, Any], field: str) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])

    response = await put_settings(api, pathway_id, body)

    assert error_of(response) == (400, "INVALID_INPUT", field)
    assert (await get_settings(api, pathway_id)).json()["regression"] == DEFAULTS


async def test_saving_a_version_carries_the_settings_into_the_next_draft(api: Api) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])
    await put_settings(api, pathway_id, {"run_on_publish": True})

    saved = await api.save(pathway_id)

    assert saved.json()["draft_version"] == 2
    assert (await get_settings(api, pathway_id)).json()["regression"]["run_on_publish"] is True


async def test_invalid_stored_value_reads_as_defaults_and_put_replaces_it(
    api: Api, identity: IdentityDb
) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])
    async with identity.database.tenant_session(api.org_id) as session:
        repo = PathwayRepo(session, api.org_id)
        pathway = await repo.get(uuid.UUID(pathway_id), lock=True)
        draft = await repo.draft(pathway)
        broken = PathwayGraphJson.of(draft)
        await repo.update_draft(
            pathway,
            draft,
            values={},
            graph=PathwayGraphJson(broken.nodes, broken.edges, {"regression": "x"}, broken.graph_hash),
        )

    assert (await get_settings(api, pathway_id)).json()["regression"] == DEFAULTS
    saved = await put_settings(api, pathway_id, {})
    assert saved.status_code == 200, saved.text
    assert (await api.get(pathway_id)).json()["global_config"] == {"regression": DEFAULTS}


async def test_other_orgs_pathway_is_not_found(
    api: Api, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])
    other = await star_api(client, identity, container)

    assert error_of(await get_settings(other, pathway_id))[:2] == (404, "NOT_FOUND")
    assert error_of(await put_settings(other, pathway_id, {"run_on_publish": True}))[:2] == (404, "NOT_FOUND")
    assert (await get_settings(api, pathway_id)).json()["regression"] == DEFAULTS
